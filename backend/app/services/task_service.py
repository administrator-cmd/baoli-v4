import logging
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import UploadFile
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from ..config import get_settings
from ..errors import BusinessError, exception_summary
from .audit.interrupt import start_audit_interrupt
from ..models import (
    AuditPlan,
    AuditTask,
    DocumentUpload,
    Evidence,
    ManualReview,
    NodeExecution,
    RuleResult,
    ScenarioVersion,
    StoredArtifact,
    TaskDocument,
    TaskFieldValue,
    WorkflowCheckpoint,
)
from ..schemas import CreateTaskRequest
from . import rag_service
from .background_service import assert_no_active_operation, latest_operation, operation_view
from ..domain.identity import public_no
from .operation_types import OBJECT_AUDIT_TASK
from .task_lookup import scenario_or_404, task_or_404
from .storage_service import DOCUMENT_EXTENSIONS, save_upload


logger = logging.getLogger(__name__)


def create_task(
    db: Session,
    scenario_code: str,
    version_no: int,
    request: CreateTaskRequest,
) -> AuditTask:
    """旧入口已收回：新审核任务只能从已发布的方案版本创建。

    任务必须绑定固定的方案版本（含场景版本引用与执行快照），直接按场景版本建任务
    会绕过方案的范围冻结。这里明确报错并给出修正入口，不静默降级。
    """
    raise BusinessError(
        "TASK_CREATE_REQUIRES_PLAN",
        "新审核任务必须从已发布的方案版本创建：请先在方案管理中为该场景建立并发布方案",
        status_code=409,
        details={"scenario_code": scenario_code, "version_no": version_no},
    )


def create_task_from_plan(
    db: Session,
    plan: AuditPlan,
    *,
    task_name: str,
    remark: str | None,
    task_no: str | None = None,
) -> AuditTask:
    """使用审核方案创建审核任务。

    任务固定方案绑定的场景版本，并把方案当前范围抓成自己的 task_schema_json 快照；
    之后修改方案不会影响本任务。task_no 可由调用方预生成，使幂等命令记录与任务
    在同一次提交中对账。
    """
    if plan.scenario_version_id is None:
        raise BusinessError("PLAN_SCOPE_UNAVAILABLE", "方案还没有绑定的场景版本，不能创建任务", status_code=409)
    scenario = db.get(ScenarioVersion, plan.scenario_version_id)
    if not scenario:
        raise BusinessError("SCENARIO_VERSION_MISSING", "方案关联的场景版本不存在", status_code=500)
    scope = plan.scope_json if isinstance(plan.scope_json, dict) else None
    # scope_json 本身就是保存时算好的任务 schema（外挂材料/字段数量与场景引用）：
    # 任务直接抓这一份，不重新解释选择。
    if not isinstance(scope, dict) or "enabled_rule_keys" not in scope:
        raise BusinessError(
            "PLAN_SCOPE_UNAVAILABLE",
            "方案当前范围尚未生效，不能创建任务；请在方案页处理待核对项后重试",
            status_code=409,
            details={"plan_code": plan.plan_code},
        )
    schema = scope
    manifest = scenario.manifest_json
    first_node = min(manifest["nodes"], key=lambda item: item["sequence_no"])
    task = AuditTask(
        task_no=task_no or public_no("TASK"),
        scenario_version_id=scenario.id,
        plan_id=plan.id,
        task_name=task_name,
        remark=remark,
        status="READY",
        current_node=first_node["node_code"],
        thread_id=public_no("thread"),
        task_schema_json=schema,
        checkpoint_json={
            "node_index": 0,
            "current_node": first_node["node_code"],
            "phase": "AWAITING_CONFIRMATION",
        },
    )
    db.add(task)
    db.flush()
    for material in manifest["materials"]:
        db.add(
            TaskDocument(
                document_no=public_no("DOC"),
                audit_task_id=task.id,
                material_key=material["material_key"],
                material_name=material["name"],
                required=material["required"],
                status="WAITING_UPLOAD",
            )
        )
    start_audit_interrupt(db, task)
    db.refresh(task)
    return task


def copy_task(db: Session, task_no: str, *, task_name: str) -> AuditTask:
    """复制任务：保留原任务的方案绑定与执行快照，材料与结果重新开始。"""
    source = task_or_404(db, task_no)
    scenario = db.get(ScenarioVersion, source.scenario_version_id)
    if not scenario:
        raise BusinessError("SCENARIO_VERSION_MISSING", "任务关联的场景版本不存在", status_code=500)
    first_node = min(scenario.manifest_json["nodes"], key=lambda item: item["sequence_no"])
    task = AuditTask(
        task_no=public_no("TASK"),
        scenario_version_id=source.scenario_version_id,
        plan_id=source.plan_id,
        task_name=task_name,
        remark=source.remark,
        status="READY",
        current_node=first_node["node_code"],
        thread_id=public_no("thread"),
        # 复制的是原任务的执行快照：不重新计算范围，历史任务按原配置继续。
        task_schema_json=source.task_schema_json,
        checkpoint_json={
            "node_index": 0,
            "current_node": first_node["node_code"],
            "phase": "AWAITING_CONFIRMATION",
        },
    )
    db.add(task)
    db.flush()
    for material in scenario.manifest_json["materials"]:
        db.add(
            TaskDocument(
                document_no=public_no("DOC"),
                audit_task_id=task.id,
                material_key=material["material_key"],
                material_name=material["name"],
                required=material["required"],
                status="WAITING_UPLOAD",
            )
        )
    start_audit_interrupt(db, task)
    db.refresh(task)
    return task


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
    失效通过“节点执行批次 + current_upload_id 绑定”表达，旧版本事实本身保留，
    因此不覆盖原始字段值和原始规则结论，历史仍能查到当时抽到了什么、判定了什么。
    调用方负责更新材料位指针，并在完成后重新建立审核等待点。
    """
    scenario = db.get(ScenarioVersion, task.scenario_version_id)
    if not scenario:
        raise BusinessError("SCENARIO_VERSION_MISSING", "任务关联的场景版本不存在", status_code=500)
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
            TaskFieldValue.document_upload_id == previous_upload_id,
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
    db.execute(
        update(StoredArtifact)
        .where(
            StoredArtifact.document_upload_id == previous_upload_id,
            StoredArtifact.status.in_(["READY", "PENDING", "SUBMITTING"]),
        )
        .values(status="INVALIDATED")
    )
    # 替换/补传材料后，旧上传版本的证据索引必须退出当前检索；旧分块与旧检索记录保留为历史。
    if previous_upload_id is not None:
        rag_service.invalidate_upload_indexes(
            db,
            task_id=task.id,
            upload_id=previous_upload_id,
            reason=f"{action_label}材料「{document.material_name}」的当前文件，旧索引不再适用于当前上传版本",
        )
    invalidated_reviews = db.execute(
        update(ManualReview)
        .where(ManualReview.audit_task_id == task.id, ManualReview.status == "PENDING")
        .values(status="INVALIDATED")
    ).rowcount
    db.execute(
        update(WorkflowCheckpoint)
        .where(WorkflowCheckpoint.audit_task_id == task.id, WorkflowCheckpoint.status == "WAITING")
        .values(status="INVALIDATED")
    )
    db.execute(
        update(NodeExecution)
        .where(NodeExecution.audit_task_id == task.id)
        .values(status="INVALIDATED")
    )
    material_check_index = next(
        item["sequence_no"] - 1
        for item in scenario.manifest_json["nodes"]
        if item["node_code"] == "MATERIAL_CHECK"
    )
    task.current_node = "MATERIAL_CHECK"
    task.status = "READY"
    task.pending_review_hash = None
    task.checkpoint_json = {
        "node_index": material_check_index,
        "current_node": "MATERIAL_CHECK",
        "phase": "AWAITING_CONFIRMATION",
    }
    # 当前有效事实与结果已失效：必须递增输入修订号，使携带旧修订的命令被明确拒绝。
    task.input_revision = (task.input_revision or 0) + 1
    logger.info(
        "材料位%s使影响范围内的结果失效 task_no=%s material_name=%s 受影响产物=%s 旧版本字段事实=%s "
        "失效规则结果=%s 失效节点执行=%s 失效待复核项=%s 新输入修订=%s",
        action_label,
        task.task_no,
        document.material_name,
        affected_artifacts,
        superseded_field_values,
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
        "reset_to_node": "MATERIAL_CHECK",
        "description": (
            f"{action_label}「{document.material_name}」的当前文件：{affected_artifacts} 个处理产物、"
            f"{superseded_field_values} 条旧版本字段事实不再进入当前结果，"
            f"{affected_rule_results} 条规则结果与 {affected_executions} 个节点执行已失效，"
            f"任务回到材料检查节点等待确认。历史事实仍可追溯。"
        ),
    }


async def upload_document(
    db: Session,
    task_no: str,
    document_no: str,
    upload: UploadFile,
    *,
    replace: bool = False,
) -> DocumentUpload:
    settings = get_settings()
    task = task_or_404(db, task_no, lock=True)
    if task.deleted_at is not None:
        raise BusinessError(
            "AUDIT_TASK_DELETED",
            "任务已被删除，请先恢复后再上传材料",
            status_code=409,
            details={"task_no": task_no},
        )
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
    # 只要任务已越过材料检查（移动过节点或执行过任何节点），补传就必须与替换走同一套失效流程。
    input_changed = bool(previous_upload_id) or task.current_node != "MATERIAL_CHECK" or execution_count > 0
    upload_no = public_no("UPL")
    suffix = Path(upload.filename or "").suffix.lower()
    relative_path = f"tasks/{task.task_no}/{document.document_no}/{upload_no}{suffix}"
    size, sha256 = await save_upload(
        upload,
        relative_path=relative_path,
        allowed_extensions=DOCUMENT_EXTENSIONS,
        max_bytes=settings.max_document_bytes,
    )
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
        size_bytes=size,
        sha256=sha256,
        status="UPLOADED",
        is_current=True,
    )
    db.add(record)
    db.flush()
    # 首次上传且任务尚未越过材料检查时没有失效范围；其余情况给出真实影响范围供页面展示。
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
    if input_changed:
        start_audit_interrupt(db, task)
    else:
        db.commit()
    db.refresh(record)
    return record, impact


def remove_current_document_file(
    db: Session,
    task_no: str,
    document_no: str,
) -> tuple[TaskDocument, dict[str, Any]]:
    """移除材料位的当前文件（输入修订：旧文件保留历史，依赖结果失效）。

    - 与后台执行互斥；任务已结束或正在执行时拒绝。
    - 没有当前文件时明确报错，不产生任何副作用。
    - 历史上传版本保留为历史，不删除文件；任务回到材料检查等待重新上传。
    """
    task = task_or_404(db, task_no, lock=True)
    if task.deleted_at is not None:
        raise BusinessError(
            "AUDIT_TASK_DELETED",
            "任务已被删除，请先恢复后再移除材料文件",
            status_code=409,
            details={"task_no": task_no},
        )
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
    start_audit_interrupt(db, task)
    db.refresh(document)
    return document, impact


def ocr_status_view(db: Session, task_no: str, document_no: str) -> dict[str, Any]:
    """当前上传文件对应的 OCR 产物只读状态。

    只返回真实记录的产物状态；没有产物时明确说明“尚未提交”，不编造作业号或截止时间。
    job_id / deadline_at 直接取自产物元数据，submit_attempts 缺失时保持 None。
    """
    task = task_or_404(db, task_no)
    document = db.scalar(
        select(TaskDocument).where(
            TaskDocument.audit_task_id == task.id,
            TaskDocument.document_no == document_no,
        )
    )
    if not document:
        raise BusinessError("TASK_DOCUMENT_NOT_FOUND", "任务材料位不存在", status_code=404)
    base = {
        "document_no": document.document_no,
        "artifact_no": None,
        "job_id": None,
        "deadline_at": None,
        "submit_attempts": None,
    }
    if document.current_upload_id is None:
        return {**base, "status": "NOT_SUBMITTED", "message": "该材料位尚未上传文件，没有 OCR 产物"}
    artifact = db.scalar(
        select(StoredArtifact)
        .where(
            StoredArtifact.document_upload_id == document.current_upload_id,
            StoredArtifact.artifact_type == "OCR",
        )
        .order_by(StoredArtifact.id.desc())
    )
    if artifact is None:
        return {**base, "status": "NOT_SUBMITTED", "message": "当前上传文件尚未提交 OCR 作业"}
    metadata = artifact.metadata_json if isinstance(artifact.metadata_json, dict) else {}
    job_id = metadata.get("job_id")
    deadline_raw = metadata.get("deadline_at")
    try:
        deadline_at = datetime.fromisoformat(deadline_raw) if isinstance(deadline_raw, str) else None
    except ValueError as exc:
        raise BusinessError("OCR_JOB_METADATA_INVALID", "当前上传文件的 OCR 截止时间无效", status_code=500) from exc
    attempts = metadata.get("submit_attempts")
    message = {
        "SUBMITTING": "OCR 作业提交结果未知，系统不会自动重复提交，请人工对账",
        "PENDING": "OCR 作业已提交，等待供应商完成",
        "READY": "OCR 产物已就绪",
        "FAILED": "OCR 作业失败",
        "INVALIDATED": "OCR 产物已随材料替换失效",
    }.get(artifact.status, f"OCR 产物状态：{artifact.status}")
    return {
        "document_no": document.document_no,
        "artifact_no": artifact.artifact_no,
        "status": artifact.status,
        "job_id": job_id if isinstance(job_id, str) else None,
        "deadline_at": deadline_at,
        "submit_attempts": attempts if isinstance(attempts, int) else None,
        "message": message,
    }


def _execution_skip_reason(execution: NodeExecution | None) -> str | None:
    """节点被显式跳过时给出原因（例如“本模板未配置此类检查”）。"""
    if execution is None or not isinstance(execution.result_json, dict):
        return None
    reason = execution.result_json.get("skip_reason")
    return reason if isinstance(reason, str) and reason else None


def task_view(db: Session, task: AuditTask) -> dict[str, Any]:
    scenario = db.get(ScenarioVersion, task.scenario_version_id)
    if not scenario:
        raise BusinessError("SCENARIO_VERSION_MISSING", "任务关联的场景版本不存在", status_code=500)
    plan = db.get(AuditPlan, task.plan_id) if task.plan_id else None
    manifest = scenario.manifest_json
    nodes = sorted(manifest["nodes"], key=lambda item: item["sequence_no"])
    executions = {
        item.node_code: item
        for item in db.scalars(
            select(NodeExecution)
            .where(NodeExecution.audit_task_id == task.id, NodeExecution.status != "INVALIDATED")
            .order_by(NodeExecution.id)
        ).all()
    }
    documents = db.scalars(select(TaskDocument).where(TaskDocument.audit_task_id == task.id).order_by(TaskDocument.id)).all()
    uploads = {
        item.id: item
        for item in db.scalars(
            # document_upload 与 task_document 之间有两条外键，必须显式给出连接条件。
            select(DocumentUpload)
            .join(TaskDocument, TaskDocument.id == DocumentUpload.task_document_id)
            .where(TaskDocument.audit_task_id == task.id, DocumentUpload.is_current.is_(True))
        ).all()
    }
    # 各材料位的 8018 识别结论，来自文件处理节点的真实执行结果；没有产物时为空。
    # 只作展示：识别结论与置信度不参与材料状态判定，也不影响后续节点执行。
    document_prepare_result = executions["DOCUMENT_PREPARE"].result_json if "DOCUMENT_PREPARE" in executions else None
    material_classify = (
        document_prepare_result.get("material_classify") or {}
        if isinstance(document_prepare_result, dict)
        else {}
    )
    evidence_rows = db.scalars(select(Evidence).where(Evidence.audit_task_id == task.id)).all()
    evidence_by_id = {item.id: item for item in evidence_rows}
    field_facts = db.scalars(
        select(TaskFieldValue)
        .join(DocumentUpload, DocumentUpload.id == TaskFieldValue.document_upload_id)
        .join(TaskDocument, TaskDocument.id == DocumentUpload.task_document_id)
        .where(
            TaskFieldValue.audit_task_id == task.id,
            TaskFieldValue.scenario_version_id == task.scenario_version_id,
            TaskDocument.audit_task_id == task.id,
            # 只看当前上传版本的事实版本；是否当前修订由 is_current 判定（同字段可能有多版历史）。
            DocumentUpload.is_current.is_(True),
            TaskDocument.current_upload_id == TaskFieldValue.document_upload_id,
        )
        .order_by(TaskFieldValue.id)
    ).all()
    # 规则结果只取当前有效执行批次：非 INVALIDATED 口径之上，再按节点取最大执行的批次，
    # 排除同一节点被重跑后遗留的旧批次结果。盲区：同一节点若有两个都未失效的批次，只展示较新的一批。
    current_execution_ids = {item.id for item in executions.values()}
    rule_results = (
        db.scalars(
            select(RuleResult)
            .join(NodeExecution, NodeExecution.id == RuleResult.node_execution_id)
            .where(
                RuleResult.audit_task_id == task.id,
                NodeExecution.status != "INVALIDATED",
                RuleResult.node_execution_id.in_(current_execution_ids),
            )
            .order_by(RuleResult.id)
        ).all()
        if current_execution_ids
        else []
    )
    reviews = db.scalars(select(ManualReview).where(ManualReview.audit_task_id == task.id, ManualReview.status == "PENDING")).all()
    current_node_definition = next((item for item in nodes if item["node_code"] == task.current_node), None)
    if not current_node_definition:
        raise BusinessError("AUDIT_NODE_NOT_IN_SCENARIO", "任务当前节点不属于场景版本", status_code=500)
    # 冻结的规则快照：范围取自创建时写入的 task_schema_json，名称取自任务绑定的
    # 场景版本 manifest（不可变）。这里不读场景最新版本，避免让人以为任务会跟随升级。
    rule_name_by_key = {
        rule["rule_key"]: rule.get("name", rule["rule_key"]) for rule in manifest.get("rules", [])
    }
    scope_enabled_keys = list(task.task_schema_json["enabled_rule_keys"])
    scope_unenabled_keys = list(task.task_schema_json["unenabled_rule_keys"])
    scope_auto_keys = list(task.task_schema_json["auto_included_rule_keys"])
    scope_auto_reasons = task.task_schema_json.get("auto_included_reasons", {}) or {}
    current_execution = executions.get(current_node_definition["node_code"])
    current_node = {
        **current_node_definition,
        "status": current_execution.status if current_execution else "CURRENT",
        "failure_message": current_execution.failure_message if current_execution else None,
    }
    # 当前事实按字段+材料取 is_current 版本；fields_by_id 保留该上传下的全部修订，
    # 使仍挂在历史修订上的待复核项不会因为查不到事实而报错。
    current_field_facts = {
        (item.field_key, item.material_key): item for item in field_facts if item.is_current
    }
    fields_by_id = {item.id: item for item in field_facts}
    rules_by_id = {item.id: item for item in rule_results}
    documents_by_id = {item.id: item for item in documents}
    documents_by_material_key = {item.material_key: item for item in documents}
    field_definitions = {item["field_key"]: item for item in task.task_schema_json["fields"]}
    # 展示规则当时保存的原文，不拿字段的最新取值替换历史判断依据。
    rule_evidence: dict[int, list[dict[str, Any]]] = {}
    evidence_upload_ids = {
        item.document_upload_id for item in evidence_rows if item.rule_result_id in rules_by_id
    }
    evidence_uploads = {
        item.id: item
        for item in db.scalars(
            select(DocumentUpload).where(
                DocumentUpload.audit_task_id == task.id,
                DocumentUpload.id.in_(evidence_upload_ids),
            )
        ).all()
    } if evidence_upload_ids else {}
    for evidence in evidence_rows:
        if evidence.rule_result_id not in rules_by_id:
            continue
        upload = evidence_uploads.get(evidence.document_upload_id)
        document = documents_by_id.get(upload.task_document_id) if upload else None
        rule_evidence.setdefault(evidence.rule_result_id, []).append({
            "evidence_no": evidence.evidence_no,
            "material_name": document.material_name if document else None,
            "filename": upload.original_filename if upload else None,
            "quote_text": evidence.quote_text,
        })
    rule_definitions = {item["rule_key"]: item for item in manifest["rules"]}
    extraction_finished = "FIELD_EXTRACTION" in executions and executions["FIELD_EXTRACTION"].status == "SUCCEEDED"
    field_view = []
    field_correction_candidates = []
    for field in task.task_schema_json["fields"]:
        fact = current_field_facts.get((field["field_key"], field["material_key"]))
        fact_is_current = fact is not None and fact.status != "NOT_EXECUTED"
        document = documents_by_material_key[field["material_key"]]
        evidence = evidence_by_id.get(fact.evidence_id) if fact_is_current and fact.evidence_id in evidence_by_id else None
        status = fact.status if fact else ("NOT_EXECUTED" if extraction_finished else "PENDING")
        normalized_value = fact.normalized_value_json if fact_is_current else None
        field_view.append(
            {
                # 字段键与材料键是主动纠错的定位依据；页面据此对某个字段事实发起纠错。
                "field_key": field["field_key"],
                "material_key": field["material_key"],
                "name": field["name"],
                "material_name": document.material_name,
                "status": status,
                "raw_value": fact.raw_value if fact_is_current else None,
                "normalized_value": normalized_value,
                "evidence_no": evidence.evidence_no if evidence else None,
                "evidence_locator_status": evidence.locator_status if evidence else None,
                # 材料未上传时字段属于“缺输入”；不适用与否由规则自己的适用条件决定。
                "material_uploaded": document.current_upload_id is not None,
                "material_required": document.required,
                "fact_revision_no": fact.revision_no if fact_is_current else None,
                "origin": fact.origin if fact_is_current else None,
            }
        )
        if fact_is_current:
            field_correction_candidates.append(
                {
                    "field_key": field["field_key"],
                    "material_key": field["material_key"],
                    "field_name": field["name"],
                    "material_name": document.material_name,
                    "status": status,
                    "raw_value": fact.raw_value,
                    "normalized_value": normalized_value,
                }
            )
    return {
        "task_no": task.task_no,
        "task_name": task.task_name,
        # 任务备注是可编辑的基本资料；执行快照不随重命名或备注修改变化。
        "remark": task.remark,
        "created_at": task.created_at,
        "updated_at": task.updated_at,
        # 方案绑定：旧任务没有方案关联时显式为 None，页面显示「历史任务配置」，
        # 不伪造关联、不改写历史结果。
        "plan": (
            {
                "plan_code": plan.plan_code,
                "plan_name": plan.plan_name,
                "scenario_version_no": scenario.version_no,
            }
            if plan
            else None
        ),
        "is_legacy_config": plan is None,
        "scenario_code": scenario.scenario_code,
        "scenario_name": scenario.scenario_name,
        "version_no": scenario.version_no,
        "status": task.status,
        "deleted": task.deleted_at is not None,
        "current_node": current_node,
        # 任务输入修订号：携带旧修订的命令会被后端明确拒绝，页面必须据此刷新。
        "input_revision": task.input_revision,
        # 本任务实际执行范围：未配置 / 未启用 / 自动补入 的区分依据。
        # 三种规则都带中文名与自动纳入原因，页面无需再去查场景最新版本。
        "scope": {
            "enabled_rule_keys": scope_enabled_keys,
            "unenabled_rule_keys": scope_unenabled_keys,
            "auto_included_rule_keys": scope_auto_keys,
            "auto_included_reasons": scope_auto_reasons,
            "unconfigured_nodes": manifest.get("unconfigured_check_nodes", []),
            "enabled_rules": [
                {"rule_key": key, "name": rule_name_by_key.get(key, key)} for key in scope_enabled_keys
            ],
            "unenabled_rules": [
                {"rule_key": key, "name": rule_name_by_key.get(key, key)} for key in scope_unenabled_keys
            ],
            "auto_included_rules": [
                {
                    "rule_key": key,
                    "name": rule_name_by_key.get(key, key),
                    "reason": scope_auto_reasons.get(key),
                }
                for key in scope_auto_keys
            ],
        },
        "nodes": [
            {
                **node,
                "status": executions[node["node_code"]].status if node["node_code"] in executions else ("CURRENT" if node["node_code"] == task.current_node else "PENDING"),
                "failure_code": executions[node["node_code"]].failure_code if node["node_code"] in executions else None,
                "failure_message": executions[node["node_code"]].failure_message if node["node_code"] in executions else None,
                "skip_reason": _execution_skip_reason(executions.get(node["node_code"])),
                # 真实子步骤（8010/OCR 处理数量、印章是否按需启用、未配置的文件质量规则）保存在节点结果里。
                "result_summary": executions[node["node_code"]].result_json if node["node_code"] in executions else None,
            }
            for node in nodes
        ],
        # 本模板没有配置这类检查；页面和执行都必须明确说明，不能显示成通过。
        "unconfigured_check_nodes": manifest.get("unconfigured_check_nodes", []),
        "documents": [
            {
                "document_no": item.document_no,
                # 材料键是检索取证的定位依据：页面据此限定检索范围，不用中文名去匹配。
                "material_key": item.material_key,
                "material_name": item.material_name,
                "required": item.required,
                "status": item.status,
                "filename": uploads[item.current_upload_id].original_filename if item.current_upload_id in uploads else None,
                # 当前文件的上传编号：页面下载/预览与历史版本入口用它定位真实文件。
                "upload_no": uploads[item.current_upload_id].upload_no if item.current_upload_id in uploads else None,
                # 该材料位的 8018 识别结论（含置信度）：只作展示，不影响材料状态与后续执行。
                "material_classify": material_classify.get(item.material_key),
            }
            for item in documents
        ],
        "fields": field_view,
        # 可主动纠错的字段事实清单（供页面提供纠错入口）。注意：AuditTaskResponse 未声明该键，
        # 集成人需在 schemas.py 补字段后前端才能从顶层读取；当前前端可从 fields[] 的 field_key/material_key 取。
        "field_correction_candidates": field_correction_candidates,
        "rules": [
            {
                "rule_key": item.rule_key,
                "name": item.rule_name,
                "status": item.status,
                "message": item.message,
                "result": item.result_json,
                "evidence": rule_evidence.get(item.id, []),
                # 旧结果没有 standard 快照时，单独返回任务绑定配置的要求，不改写执行结果。
                "configured_requirement": rule_definitions.get(item.rule_key, {}).get("requirement"),
            }
            for item in rule_results
        ],
        "pending_reviews": [
            _pending_review_view(
                review=item,
                documents_by_id=documents_by_id,
                documents_by_material_key=documents_by_material_key,
                uploads_by_id=uploads,
                fields_by_id=fields_by_id,
                field_definitions=field_definitions,
                rules_by_id=rules_by_id,
                evidence_rows=evidence_rows,
                evidence_by_id=evidence_by_id,
            )
            for item in reviews
        ],
        "latest_operation": operation_view(latest) if (latest := latest_operation(db, OBJECT_AUDIT_TASK, task.task_no)) else None,
    }


def _pending_review_view(
    *,
    review: ManualReview,
    documents_by_id: dict[int, TaskDocument],
    documents_by_material_key: dict[str, TaskDocument],
    uploads_by_id: dict[int, DocumentUpload],
    fields_by_id: dict[int, TaskFieldValue],
    field_definitions: dict[str, dict[str, Any]],
    rules_by_id: dict[int, RuleResult],
    evidence_rows: list[Evidence],
    evidence_by_id: dict[int, Evidence],
) -> dict[str, Any]:
    """复核表单只暴露业务上下文，不返回内部标识符。"""
    material_name: str | None = None
    field_name: str | None = None
    rule_name: str | None = None
    evidence_quote: str | None = None
    evidence_no: str | None = None
    effective_status: str | None = None

    if review.document_upload_id is not None:
        upload = uploads_by_id.get(review.document_upload_id)
        if upload is None:
            raise BusinessError("REVIEW_DOCUMENT_MISSING", "复核关联材料不存在", status_code=500)
        document = documents_by_id.get(upload.task_document_id)
        if document is None:
            raise BusinessError("REVIEW_DOCUMENT_MISSING", "复核关联材料不存在", status_code=500)
        material_name = document.material_name
        effective_status = document.status
    elif review.field_value_id is not None:
        fact = fields_by_id.get(review.field_value_id)
        if fact is None:
            raise BusinessError("REVIEW_FIELD_MISSING", "复核关联字段不存在", status_code=500)
        field = field_definitions.get(fact.field_key)
        document = documents_by_material_key.get(fact.material_key)
        if field is None or document is None:
            raise BusinessError("REVIEW_FIELD_SCHEMA_MISSING", "复核字段不在任务字段清单中", status_code=500)
        field_name = field["name"]
        material_name = document.material_name
        effective_status = fact.status
        evidence = evidence_by_id.get(fact.evidence_id) if fact.evidence_id is not None else None
        evidence_quote = evidence.quote_text if evidence else None
        evidence_no = evidence.evidence_no if evidence else None
    elif review.rule_result_id is not None:
        result = rules_by_id.get(review.rule_result_id)
        if result is None:
            raise BusinessError("REVIEW_RULE_MISSING", "复核关联规则不存在", status_code=500)
        rule_name = result.rule_name
        effective_status = result.status
        evidence = next((item for item in evidence_rows if item.rule_result_id == result.id), None)
        evidence_quote = evidence.quote_text if evidence else None
        evidence_no = evidence.evidence_no if evidence else None
    else:
        raise BusinessError("REVIEW_TARGET_INVALID", "人工复核缺少关联业务对象", status_code=500)

    # 系统原判断是不可变快照：状态、消息、原值、标准化值与证据整体返回，不能只取 message。
    judgment = review.system_judgment_json if isinstance(review.system_judgment_json, dict) else {}
    system_judgment = {
        "status": judgment.get("status"),
        "message": judgment.get("message"),
        "raw_value": judgment.get("raw_value"),
        "normalized_value": judgment.get("normalized_value"),
        "evidence": judgment.get("evidence"),
    }
    return {
        "review_no": review.review_no,
        "review_type": review.review_type,
        "status": review.status,
        "business_note": review.business_note,
        "proposed_value": review.proposed_value,
        "material_name": material_name,
        "field_name": field_name,
        "rule_name": rule_name,
        "system_judgment": system_judgment,
        "manual_conclusion": review.decision,
        "effective_status": effective_status,
        "basis_type": review.basis_type,
        "before_value": review.before_value,
        "after_value": review.after_value,
        "evidence_quote": evidence_quote,
        "evidence_no": evidence_no,
    }
