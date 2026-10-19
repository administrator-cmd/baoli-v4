"""任务材料上传、移除与输入变更失效处理。"""

import logging
from pathlib import Path
from typing import Any
from fastapi import UploadFile
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session
from ....config import get_settings
from ....common.exceptions import BusinessError
from ....entity import (
    AuditTask,
    BackgroundOperation,
    DocumentUpload,
    ManualReview,
    NodeExecution,
    RuleResult,
    StoredArtifact,
    TaskDocument,
    TaskFact,
    TaskFieldValue,
)
from ...system.background import assert_no_active_operation
from ....domain.identity import public_no
from ....domain.rule_library import execution_container_of
from ....integrations.http_services import list_material_types
from ...system.operation_types import OBJECT_AUDIT_TASK
from .lookup import task_or_404
from ..execution_history import invalidate_executions
from ...shared.storage import DOCUMENT_EXTENSIONS, resolve_storage_path, save_upload_sync


logger = logging.getLogger(__name__)


def _verified_material_type(declared: str) -> str:
    """核对上传时声明的材料类型，返回可落库的取值。

    8018 的 doc_type 是严格枚举，目录是外部服务的事实：本函数不缓存、不内置默认清单、
    不按材料名称猜类型。空值与目录外的取值都在这里显式拒绝——问题要在上传时就暴露，
    不能等到材料检查节点执行时才由 8018 拒绝，那时用户已经以为文件传好了。
    """
    material_type = (declared or "").strip()
    if not material_type:
        raise BusinessError(
            "MATERIAL_TYPE_MISSING",
            "未选择材料类型，无法上传：请从 8018 支持的材料类型目录中选择",
            status_code=409,
        )
    supported = {str(item["name"]) for item in list_material_types()}
    if material_type not in supported:
        raise BusinessError(
            "MATERIAL_TYPE_UNSUPPORTED",
            "选择的材料类型不在 8018 支持目录中，无法上传",
            status_code=409,
            details={"material_type": material_type, "supported": sorted(supported)},
        )
    return material_type


def _invalidate_for_input_change(
    db: Session,
    task: AuditTask,
    document: TaskDocument,
    previous_upload_id: int | None,
    *,
    action_label: str,
) -> dict[str, Any]:
    """任务输入变化（替换 / 首次补传 / 移除当前文件）的统一失效流程。

    受影响的处理产物、字段事实、后续结果与报告一起失效；旧版本事实保留可追溯。
    失效通过“节点执行批次 + current_upload_id 绑定 + 统一事实当前标记”表达，旧版本事实本身保留，
    因此不覆盖原始字段值和原始规则结论，历史仍能查到当时抽到了什么、判定了什么。
    调用方负责更新材料位指针，并在完成后重新建立审核等待点。
    """
    manifest = task.manifest_json
    if not isinstance(manifest, dict) or not manifest:
        raise BusinessError("TASK_MANIFEST_MISSING", "任务缺少已冻结的执行配置", status_code=500)
    affected_artifacts = db.scalar(
        select(func.count())
        .select_from(StoredArtifact)
        .where(
            StoredArtifact.document_upload_id == previous_upload_id,
            StoredArtifact.status.in_(["READY", "PENDING", "SUBMITTING"]),
        )
    ) or 0
    superseded_field_values = db.scalar(
        select(func.count())
        .select_from(TaskFieldValue)
        .where(
            TaskFieldValue.audit_task_id == task.id,
            TaskFieldValue.is_current.is_(True),
        )
    ) or 0
    affected_rule_results = db.scalar(
        select(func.count())
        .select_from(RuleResult)
        .join(NodeExecution, NodeExecution.id == RuleResult.node_execution_id)
        .where(NodeExecution.audit_task_id == task.id, NodeExecution.status != "INVALIDATED")
    ) or 0
    affected_executions = db.scalar(
        select(func.count())
        .select_from(NodeExecution)
        .where(NodeExecution.audit_task_id == task.id, NodeExecution.status != "INVALIDATED")
    ) or 0
    invalidated_task_facts = db.scalar(
        select(func.count())
        .select_from(TaskFact)
        .where(TaskFact.audit_task_id == task.id, TaskFact.is_current.is_(True))
    ) or 0
    db.execute(
        update(StoredArtifact)
        .where(
            StoredArtifact.document_upload_id == previous_upload_id,
            StoredArtifact.status.in_(["READY", "PENDING", "SUBMITTING"]),
        )
        .values(status="INVALIDATED")
    )
    invalidated_reviews = db.execute(
        update(ManualReview)
        .where(ManualReview.audit_task_id == task.id, ManualReview.status == "PENDING")
        .values(status="INVALIDATED")
    ).rowcount

    invalidate_executions(db, NodeExecution.audit_task_id == task.id, reason="任务材料输入已变更")
    # 当前实现会让全部节点和全部统一事实失效并从 DAG 起点重跑，因此候选层也必须
    # 按同一范围整体退出当前位。只撤销被替换材料会留下“候选仍当前、TaskFact 已失效”
    # 的半套事实，任务详情与规则输入配对时会明确报错。
    db.execute(
        update(TaskFieldValue)
        .where(
            TaskFieldValue.audit_task_id == task.id,
            TaskFieldValue.is_current.is_(True),
        )
        .values(is_current=False)
    )
    # 所有节点执行都已失效，抽取与派生事实也不能继续作为后续节点的当前输入。
    db.execute(
        update(TaskFact)
        .where(TaskFact.audit_task_id == task.id, TaskFact.is_current.is_(True))
        .values(is_current=False)
    )
    first_node_code = execution_container_of(manifest)["node_code"]
    first_node_index = 0
    task.current_node = first_node_code
    task.status = "READY"
    task.checkpoint_json = {
        "node_index": first_node_index,
        "current_node": first_node_code,
        "phase": "AWAITING_CONFIRMATION",
    }
    # 当前有效事实与结果已失效：必须递增输入修订号，使携带旧修订的命令被明确拒绝。
    task.input_revision = (task.input_revision or 0) + 1
    logger.info(
        "材料位%s使影响范围内的结果失效 task_no=%s material_name=%s 受影响产物=%s 旧版本字段事实=%s "
        "失效统一事实=%s 失效规则结果=%s 失效节点执行=%s 失效待复核项=%s 新输入修订=%s",
        action_label,
        task.task_no,
        document.material_name,
        affected_artifacts,
        superseded_field_values,
        invalidated_task_facts,
        affected_rule_results,
        affected_executions,
        invalidated_reviews,
        task.input_revision,
    )
    return {
        "affected_artifacts": int(affected_artifacts),
        "stale_field_values": int(superseded_field_values),
        "invalidated_rule_results": int(affected_rule_results),
        "invalidated_node_executions": int(affected_executions),
        "invalidated_reviews": int(invalidated_reviews or 0),
        "reset_to_node": first_node_code,
        "description": (
            f"{action_label}「{document.material_name}」的当前文件：{affected_artifacts} 个处理产物、"
            f"{superseded_field_values} 条旧版本字段事实不再进入当前结果，"
            f"{invalidated_task_facts} 条统一事实已失效，"
            f"{affected_rule_results} 条规则结果与 {affected_executions} 个节点执行已失效，"
            f"任务回到起始节点等待确认。历史事实仍可追溯。"
        ),
    }


def upload_document(
    db: Session,
    task_no: str,
    document_no: str,
    upload: UploadFile,
    *,
    material_type: str,
    replace: bool = False,
) -> tuple[TaskDocument, DocumentUpload, dict[str, Any] | None, BackgroundOperation]:
    """上传或替换材料文件：落库、失效下游结果，并返回本次输入修订的真实影响范围。

    `material_type` 是用户本次上传时从 8018 支持目录中选择的材料类型。它随本次上传
    保存（见 DocumentUpload.declared_material_type），不来自场景配置：材料位本身不声明
    类型，替换文件时可以改选。空值或不在 8018 目录内都显式拒绝，不代填、不跳过校验。
    """
    settings = get_settings()
    task = task_or_404(db, task_no, lock=True)
    # 与后台执行互斥必须在锁定对象行之后：先取锁再查活跃操作，消除检查与修改之间的 TOCTOU。
    assert_no_active_operation(db, OBJECT_AUDIT_TASK, task.task_no)
    document = db.scalar(
        select(TaskDocument).where(
            TaskDocument.audit_task_id == task.id,
            TaskDocument.document_no == document_no,
        )
    )
    if not document:
        raise BusinessError("TASK_DOCUMENT_NOT_FOUND", "任务材料位不存在", status_code=404)
    if task.status in {"COMPLETED", "FAILED"}:
        raise BusinessError("TASK_DOCUMENT_LOCKED", "任务已结束，不能替换材料")
    if task.status == "RUNNING":
        raise BusinessError("AUDIT_TASK_RUNNING", "任务正在执行当前节点，不能替换材料", status_code=409)
    previous_upload_id = document.current_upload_id
    if previous_upload_id and not replace:
        raise BusinessError(
            "TASK_DOCUMENT_REPLACE_CONFIRMATION_REQUIRED",
            "该材料位已有当前文件，请明确确认替换后再上传",
            status_code=409,
        )
    execution_count = db.scalar(
        select(func.count()).select_from(NodeExecution).where(NodeExecution.audit_task_id == task.id)
    ) or 0
    # 输入变化不止“替换”：首次补传此前缺失的当前文件，同样改变任务已经依据的结论。
    # 只要任务已越过起始节点（移动过节点或执行过任何节点），补传就必须与替换走同一套失效流程。
    task_manifest = task.manifest_json
    first_node_code = execution_container_of(task_manifest)["node_code"]
    input_changed = (
        bool(previous_upload_id)
        or task.current_node != first_node_code
        or execution_count > 0
    )
    # 类型核对放在所有任务侧校验之后、写文件之前：请求本身不合法时不必打扰 8018，
    # 而类型不合法时必须在落盘前失败，不留下一份没有类型的孤儿文件。
    declared_material_type = _verified_material_type(material_type)
    upload_no = public_no("UPL")
    suffix = Path(upload.filename or "").suffix.lower()
    relative_path = f"tasks/{task.task_no}/{document.document_no}/{upload_no}{suffix}"
    size, sha256 = save_upload_sync(
        upload,
        relative_path=relative_path,
        allowed_extensions=DOCUMENT_EXTENSIONS,
        max_bytes=settings.max_document_bytes,
    )
    task_id = task.id
    try:
        db.execute(
            update(DocumentUpload)
            .where(DocumentUpload.task_document_id == document.id, DocumentUpload.is_current.is_(True))
            .values(is_current=False, status="SUPERSEDED")
        )
        record = DocumentUpload(
            upload_no=upload_no,
            task_document_id=document.id,
            audit_task_id=task.id,
            original_filename=upload.filename or f"材料{suffix}",
            relative_path=relative_path,
            content_type=upload.content_type or "application/octet-stream",
            declared_material_type=declared_material_type,
            size_bytes=size,
            sha256=sha256,
            status="UPLOADED",
            is_current=True,
        )
        db.add(record)
        db.flush()
        # 首次上传且任务尚未越过起始节点时没有失效范围；其余情况给出真实影响范围供页面展示。
        impact: dict[str, Any] | None = None
        if input_changed:
            # 补传/替换文件是明确的新版本输入：受影响的处理产物、字段事实、后续结果与报告一起失效。
            impact = _invalidate_for_input_change(
                db,
                task,
                document,
                previous_upload_id,
                action_label="已替换" if previous_upload_id else "已补传此前缺失的",
            )
        document.current_upload_id = record.id
        document.status = "UPLOADED"

        # 文件保存、输入失效和分类命令一起提交；活跃命令占位消除提交后的替换/执行竞态。
        from ..operations import submit_document_classification
        try:
            operation, _ = submit_document_classification(db, task, record)
        except BusinessError as exc:
            if exc.code != "OPERATION_DISPATCH_FAILED":
                raise
            # 调度失败时文件已提交：响应明确携带 FAILED 分类状态，不能把整个上传报成失败。
            operation = db.scalar(select(BackgroundOperation).where(
                BackgroundOperation.object_type == OBJECT_AUDIT_TASK,
                BackgroundOperation.object_no == task.task_no,
                BackgroundOperation.operation_type == "DOCUMENT_CLASSIFY",
                BackgroundOperation.idempotency_key == record.upload_no,
            ))
            if operation is None or operation.status != "FAILED":
                raise
            logger.error("文件已保存但分类调度失败 upload_no=%s operation_no=%s", record.upload_no, operation.operation_no)
        return document, record, impact, operation
    except Exception:
        _remove_uncommitted_upload(db, task_id, upload_no, relative_path)
        raise


def _remove_uncommitted_upload(db, task_id, upload_no, relative_path):
    """确认事务未留下上传记录后，只删除本次落盘文件。"""
    try:
        db.rollback()
        with Session(bind=db.get_bind()) as verification:
            # 先等原任务写事务结束，再确认提交响应丢失时是否已经保存了上传记录。
            verification.scalar(select(AuditTask.id).where(AuditTask.id == task_id).with_for_update())
            saved = verification.scalar(select(DocumentUpload.id).where(DocumentUpload.upload_no == upload_no))
            if saved is None:
                resolve_storage_path(relative_path).unlink(missing_ok=True)
    except Exception:
        # 数据库不可达时不能确认文件未被引用；保留文件并记录清理错误，原上传错误继续抛出。
        logger.exception("无法确认上传文件的事务引用或清理文件，已保留以便核查 upload_no=%s path=%s", upload_no, relative_path)



def remove_current_document_file(
    db: Session,
    task_no: str,
    document_no: str,
) -> tuple[TaskDocument, dict[str, Any]]:
    """移除材料位的当前文件（输入修订：旧文件保留历史，依赖结果失效）。

    - 与后台执行互斥；任务已结束或正在执行时拒绝。
    - 没有当前文件时明确报错，不产生任何副作用。
    - 历史上传版本保留为历史，不删除文件；任务回到规则执行等待重新上传。
    """
    task = task_or_404(db, task_no, lock=True)
    assert_no_active_operation(db, OBJECT_AUDIT_TASK, task.task_no)
    document = db.scalar(
        select(TaskDocument).where(
            TaskDocument.audit_task_id == task.id,
            TaskDocument.document_no == document_no,
        )
    )
    if not document:
        raise BusinessError("TASK_DOCUMENT_NOT_FOUND", "任务材料位不存在", status_code=404)
    if document.current_upload_id is None:
        raise BusinessError("TASK_DOCUMENT_NO_CURRENT_FILE", "该材料位没有当前文件，无法移除", status_code=409)
    if task.status in {"COMPLETED", "FAILED"}:
        raise BusinessError("TASK_DOCUMENT_LOCKED", "任务已结束，不能移除材料文件", status_code=409)
    if task.status == "RUNNING":
        raise BusinessError("AUDIT_TASK_RUNNING", "任务正在执行当前节点，不能移除材料文件", status_code=409)
    previous_upload_id = document.current_upload_id
    db.execute(
        update(DocumentUpload)
        .where(DocumentUpload.id == previous_upload_id)
        .values(is_current=False, status="REMOVED")
    )
    document.current_upload_id = None
    document.status = "WAITING_UPLOAD"
    impact = _invalidate_for_input_change(
        db,
        task,
        document,
        previous_upload_id,
        action_label="已移除",
    )
    db.commit()
    db.refresh(document)
    return document, impact
