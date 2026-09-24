import logging
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import UploadFile
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from ..config import get_settings
from ..errors import BusinessError, exception_summary
from .audit.interrupt import start_audit_interrupt
# 复核批次的划分与哈希口径只有一份（`services.audit.reviews`）：
# 任务视图给出的本批、提交校验用的本批、检查点里的哈希必须是同一个集合，
# 在这里另算一套会让页面提交的集合与后端要求的集合永远对不上。
from .audit.reviews import _current_review_batch, _review_hash
from .audit.state import _node_codes_of_type, _single_node_code_of_type
from ..models import (
    AuditPlan,
    AuditTask,
    DocumentUpload,
    Evidence,
    ManualReview,
    NodeExecution,
    RuleResult,
    ScenarioTemplate,
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
from ..domain.node_registry import legacy_node_type_of
from ..integrations.http_services import list_material_types
from .operation_types import OBJECT_AUDIT_TASK
from .task_lookup import scenario_or_404, task_or_404
from .storage_service import DOCUMENT_EXTENSIONS, save_upload


logger = logging.getLogger(__name__)


def _verified_material_type(declared: str) -> str:
    """核对上传时声明的材料类型，返回可落库的取值。

    8018 的 doc_type 是严格枚举，目录是**外部服务的事实**：本函数不缓存、不内置默认清单、
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
        "新审核任务必须从已启用的方案创建：请先基于已发布场景建立方案，再从方案创建任务",
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
    expected_scope_hash: str | None = None,
) -> AuditTask:
    """使用审核方案创建审核任务。这是新任务的唯一创建服务。

    同一事务里先锁定方案行，再校验方案、场景和已固定版本。通过后把**创建时确认的
    方案范围**整体冻结进 task_schema_json：场景版本、节点、Sheet、规则、材料与字段，
    以及方案的编号、名称与范围指纹。任务之后一切读取都从这里取，
    不再读方案当前名称或方案后来重算的范围。

    `expected_scope_hash` 是页面预览时看到的方案范围指纹：与锁定后的实际指纹不一致时
    明确返回 `PLAN_SCOPE_CHANGED`——任务必须冻结用户刚刚确认的那一版范围，
    不在创建时静默取一个已经改过的范围。

    task_no 可由调用方预生成，使幂等命令记录与任务在同一次提交中对账。
    本函数**不**记录成功操作日志：调用方要在等待点确认成功之后才记。
    """
    locked = db.scalar(select(AuditPlan).where(AuditPlan.id == plan.id).with_for_update())
    if locked is None:
        raise BusinessError(
            "PLAN_NOT_FOUND",
            "审核方案不存在或已被删除",
            status_code=404,
            details={"plan_code": plan.plan_code},
        )
    plan = locked
    if plan.deleted_at is not None:
        raise BusinessError(
            "PLAN_DELETED",
            "该方案已删除，不能创建任务",
            status_code=409,
            details={"plan_code": plan.plan_code},
        )
    if not plan.enabled:
        raise BusinessError(
            "PLAN_DISABLED",
            "该方案已停用，不能创建任务",
            status_code=409,
            details={"plan_code": plan.plan_code},
        )
    scenario_template = db.scalar(
        select(ScenarioTemplate).where(ScenarioTemplate.scenario_code == plan.scenario_code)
    )
    if scenario_template is None or scenario_template.deleted_at is not None:
        raise BusinessError(
            "SCENARIO_NOT_FOUND",
            "方案所属场景不存在或已被删除",
            status_code=409,
            details={"scenario_code": plan.scenario_code},
        )
    if not scenario_template.enabled:
        raise BusinessError(
            "SCENARIO_DISABLED",
            "该场景已停用，不能创建任务",
            status_code=409,
            details={"scenario_code": plan.scenario_code},
        )
    if plan.scenario_version_id is None:
        raise BusinessError(
            "PLAN_SCENARIO_VERSION_MISSING",
            "方案还没有绑定已发布的场景版本，不能创建任务",
            status_code=409,
            details={"plan_code": plan.plan_code},
        )
    scenario = db.get(ScenarioVersion, plan.scenario_version_id)
    if not scenario:
        raise BusinessError(
            "SCENARIO_VERSION_MISSING",
            "方案关联的场景版本不存在",
            status_code=409,
            details={"plan_code": plan.plan_code, "scenario_version_id": plan.scenario_version_id},
        )
    if scenario.status != "PUBLISHED":
        # 方案只能引用已发布版本；非发布版本不能作为任务的执行依据。
        raise BusinessError(
            "SCENARIO_VERSION_NOT_PUBLISHED",
            "方案关联的场景版本不是已发布状态，不能创建任务",
            status_code=409,
            details={"plan_code": plan.plan_code, "status": scenario.status},
        )
    if (
        expected_scope_hash is not None
        and plan.scope_hash is not None
        and expected_scope_hash != plan.scope_hash
    ):
        raise BusinessError(
            "PLAN_SCOPE_CHANGED",
            "方案范围在创建任务期间已被修改，任务必须冻结刚刚确认的那一版范围。请刷新方案页重新核对后再创建任务",
            status_code=409,
            details={
                "plan_code": plan.plan_code,
                "expected_scope_hash": expected_scope_hash,
                "current_scope_hash": plan.scope_hash,
            },
        )
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
    if plan.scope_hash is None:
        raise BusinessError(
            "PLAN_SCOPE_HASH_MISSING",
            "方案范围没有指纹，无法确认任务冻结的是哪一版范围；请在方案页重新保存后再创建任务",
            status_code=409,
            details={"plan_code": plan.plan_code},
        )
    # 任务快照同时冻结方案身份、方案范围、节点计划和 Sheet 范围。
    # 执行范围与配置都取自方案自身；场景版本只保留来源身份。
    schema = dict(scope)
    manifest = plan.manifest_json
    if not isinstance(manifest, dict):
        raise BusinessError(
            "PLAN_MANIFEST_UNAVAILABLE",
            "方案当前没有可执行配置，不能创建任务",
            status_code=409,
            details={"plan_code": plan.plan_code},
        )
    schema["scenario_code"] = scenario.scenario_code
    schema["scenario_version_no"] = scenario.version_no
    # 方案身份与范围指纹随任务冻结：任务详情与幂等重放都读它，
    # 不读方案后来改过的名称或重算过的范围。
    schema["plan_code"] = plan.plan_code
    schema["plan_name"] = plan.plan_name
    schema["plan_scope_hash"] = plan.scope_hash
    schema["node_plan"] = [dict(item) for item in manifest.get("nodes") or []]
    schema["selected_sheets"] = [dict(item) for item in manifest.get("selected_sheets") or []]
    nodes = list(manifest.get("nodes") or [])
    if not nodes:
        # `min()` 对空序列会抛 ValueError，那不是业务错误；这里显式报出业务原因。
        raise BusinessError(
            "SCENARIO_NODE_REQUIRED",
            "该场景版本没有配置任何节点，不能创建任务：请先在场景配置里建立节点并重新发布",
            status_code=409,
            details={"scenario_code": scenario.scenario_code, "version_no": scenario.version_no},
        )
    first_node = min(nodes, key=lambda item: item["sequence_no"])
    task = AuditTask(
        task_no=task_no or public_no("TASK"),
        scenario_version_id=scenario.id,
        plan_id=plan.id,
        task_name=task_name,
        remark=remark,
        status="READY",
        current_node=first_node["node_code"],
        thread_id=public_no("thread"),
        manifest_json=deepcopy(manifest),
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
    db.refresh(task)
    return task


def copy_task(db: Session, task_no: str, *, task_name: str) -> AuditTask:
    """按源任务所属方案的**当前范围**重新创建任务。

    明确不是「复制原任务的执行快照」：复制出来的是一个全新的任务，它冻结的是
    方案此刻的范围——源任务的冻结快照与已发生的执行事实一概不带过来。
    没有方案、方案已删除或已停用时拒绝；场景与版本门禁由 create_task_from_plan 统一执行。
    """
    source = task_or_404(db, task_no)
    if source.plan_id is None:
        raise BusinessError(
            "TASK_COPY_REQUIRES_PLAN",
            "该任务没有关联方案，不能复制。请从已启用的方案创建任务",
            status_code=409,
            details={"task_no": task_no},
        )
    plan = db.get(AuditPlan, source.plan_id)
    if plan is None or plan.deleted_at is not None:
        raise BusinessError(
            "PLAN_DELETED",
            "该任务所属方案不存在或已删除，不能复制任务",
            status_code=409,
            details={"task_no": task_no, "plan_id": source.plan_id},
        )
    if not plan.enabled:
        raise BusinessError(
            "PLAN_DISABLED",
            "该任务所属方案已停用，不能复制任务",
            status_code=409,
            details={"plan_code": plan.plan_code},
        )
    created = create_task_from_plan(db, plan, task_name=task_name, remark=source.remark)
    # 与新建任务同一口径：任务与等待点都成功后，才记一条成功操作日志。
    start_audit_interrupt(db, created)
    db.refresh(created)
    return created


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
    # 材料变化后任务退回材料检查节点：节点实例码由用户配置，按**能力类型**解析，
    # 不写死能力类型当实例码（用户把实例码改成别的值时写死会一条都找不到）。
    material_check_code = _single_node_code_of_type(
        manifest, "MATERIAL_CHECK", purpose="材料变化后的退回节点"
    )
    material_check_index = next(
        index
        for index, item in enumerate(
            sorted(manifest["nodes"], key=lambda row: row["sequence_no"])
        )
        if item["node_code"] == material_check_code
    )
    task.current_node = material_check_code
    task.status = "READY"
    task.pending_review_hash = None
    task.checkpoint_json = {
        "node_index": material_check_index,
        "current_node": material_check_code,
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
        "reset_to_node": material_check_code,
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
    material_type: str,
    replace: bool = False,
) -> DocumentUpload:
    """上传或替换材料文件：落库、失效下游结果，并返回本次输入修订的真实影响范围。

    `material_type` 是用户本次上传时从 8018 支持目录中选择的材料类型。它随**本次上传**
    保存（见 DocumentUpload.declared_material_type），不来自场景配置：材料位本身不声明
    类型，替换文件时可以改选。空值或不在 8018 目录内都显式拒绝，不代填、不跳过校验。
    """
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
    # 材料检查节点按**能力类型**解析实例码：写死能力类型当实例码会在用户改过实例码后失效。
    task_manifest = task.manifest_json
    input_changed = (
        bool(previous_upload_id)
        or task.current_node not in _node_codes_of_type(task_manifest, "MATERIAL_CHECK")
        or execution_count > 0
    )
    # 类型核对放在所有任务侧校验之后、写文件之前：请求本身不合法时不必打扰 8018，
    # 而类型不合法时必须在落盘前失败，不留下一份没有类型的孤儿文件。
    declared_material_type = _verified_material_type(material_type)
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
        declared_material_type=declared_material_type,
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


def _task_display_structure(
    task: AuditTask,
    manifest: dict[str, Any],
    nodes: list[dict[str, Any]],
) -> dict[str, Any]:
    """当前任务的**展示结构**：只包含本场景真实选中的节点、Sheet 与字段。

    需求 31~34 要求「展示方式通用、展示内容动态」：前端提供通用组件（文本、金额、
    日期、表格、审核结果），具体展示什么由后端按当前场景配置返回，页面不得按场景名
    或固定条件分支写死内容。

    因此这里返回的是**数据**而不是文案：
    - `nodes`：本任务真实配置的节点实例（实例码 + 能力类型 + 顺序 + 依赖），
      前端据此渲染流程条与工作区，不再维护一份写死的阶段清单；
    - `sheets`：本场景选用的 Sheet 及各自归属的节点（来自 manifest 的
      `selected_sheets`，即用户在场景配置里确认过的选择）；
    - `fields`：本任务执行范围内的字段定义（名称、类型、单位、币种、所属材料），
      前端据此决定用哪种通用组件渲染，不靠字段名猜类型。

    任务创建时写入的 task_schema_json 优先：有 node_plan / selected_sheets 键就用它们。
    历史任务没有这些键时，才读该任务所钉版本的 manifest，不读场景草稿。
    """
    schema = task.task_schema_json if isinstance(task.task_schema_json, dict) else {}
    node_source = schema["node_plan"] if isinstance(schema.get("node_plan"), list) else nodes
    if "selected_sheets" in schema and isinstance(schema.get("selected_sheets"), list):
        sheet_source = schema["selected_sheets"]
    else:
        sheet_source = manifest.get("selected_sheets") or []
    return {
        "task_no": task.task_no,
        "nodes": [
            {
                "node_code": str(node["node_code"]),
                # 能力类型：缺 `node_type` 的旧节点只有在其实例码本身就是已知能力类型时
                # 才能确定性迁移；无法确定时如实返回 null，页面按「未识别」处理，
                # 不拿实例码冒充能力类型去匹配工作区。
                "node_type": node.get("node_type") or legacy_node_type_of(str(node["node_code"])),
                "name": str(node.get("name") or node["node_code"]),
                "sequence_no": int(node.get("sequence_no") or 0),
                "depends_on": [str(code) for code in (node.get("depends_on") or [])],
                "enabled": bool(node.get("enabled", True)),
            }
            for node in node_source
        ],
        "sheets": [
            {
                "sheet_role": str(item.get("sheet_role")),
                "node_code": item.get("node_code"),
            }
            for item in sheet_source
            if isinstance(item, dict) and item.get("sheet_role")
        ],
        "fields": [
            {
                "field_key": str(field["field_key"]),
                "name": str(field["name"]),
                "value_type": str(field["value_type"]),
                "unit": field.get("unit"),
                "currency": field.get("currency"),
                "material_key": str(field["material_key"]),
            }
            for field in task.task_schema_json["fields"]
        ],
    }


def _execution_skip_reason(execution: NodeExecution | None) -> str | None:
    """节点被显式跳过时给出原因（例如“本模板未配置此类检查”）。"""
    if execution is None or not isinstance(execution.result_json, dict):
        return None
    reason = execution.result_json.get("skip_reason")
    return reason if isinstance(reason, str) and reason else None


def task_view(db: Session, task: AuditTask) -> dict[str, Any]:
    """任务详情视图：节点、材料位、字段、规则、执行范围与最近一次后台操作。"""
    scenario = db.get(ScenarioVersion, task.scenario_version_id)
    if not scenario:
        raise BusinessError("SCENARIO_VERSION_MISSING", "任务关联的场景版本不存在", status_code=500)
    manifest = task.manifest_json
    schema = task.task_schema_json if isinstance(task.task_schema_json, dict) else {}
    if isinstance(schema.get("node_plan"), list):
        nodes = sorted(schema["node_plan"], key=lambda item: item.get("sequence_no") or 0)
    else:
        nodes = sorted(manifest["nodes"], key=lambda item: item["sequence_no"])
    frozen_version_no = (
        schema["scenario_version_no"]
        if isinstance(schema.get("scenario_version_no"), int)
        else scenario.version_no
    )
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
    prepare_codes = _node_codes_of_type(manifest, "DOCUMENT_PREPARE")
    document_prepare_result = next(
        (executions[code].result_json for code in prepare_codes if code in executions), None
    )
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
    # 待办分两层，页面必须按**本批**提交：
    # `reviews` 是当前复核批次（唯一允许提交的集合），`all_pending` 是全部待办。
    # 复核是分批判决的：存在字段确认待办时本批只含字段事实，规则结论待办留到下一批。
    # 页面把全部待办一次提交时，集合恒不等于本批，服务端只会 409。
    all_pending = db.scalars(
        select(ManualReview)
        .where(ManualReview.audit_task_id == task.id, ManualReview.status == "PENDING")
        .order_by(ManualReview.id)
    ).all()
    reviews = _current_review_batch(db, task)
    batch_hash = _review_hash(reviews) if reviews else None
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
    # 未配置的检查类别优先读**任务冻结的快照**：它随任务一起冻结，之后场景发布新版本
    # 不会改写历史任务的范围说明。历史任务没有这两个键时按该任务所钉版本的 manifest 读取，
    # 都取不到时返回空列表（不编造「都没配」）。
    unconfigured_node_types = list(
        schema.get("unconfigured_check_node_types")
        or manifest.get("unconfigured_check_node_types")
        or []
    )
    unconfigured_node_codes = list(
        schema.get("unconfigured_check_node_codes")
        or manifest.get("unconfigured_check_node_codes")
        or []
    )
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
    extraction_codes = _node_codes_of_type(manifest, "FIELD_EXTRACTION")
    extraction_finished = any(
        executions[code].status == "SUCCEEDED" for code in extraction_codes if code in executions
    )
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
                "plan_code": schema["plan_code"],
                "plan_name": schema["plan_name"],
                "scenario_version_no": frozen_version_no,
            }
            if task.plan_id is not None
            else None
        ),
        "is_legacy_config": task.plan_id is None,
        "scenario_code": scenario.scenario_code,
        "scenario_name": scenario.scenario_name,
        "version_no": frozen_version_no,
        "status": task.status,
        "deleted": task.deleted_at is not None,
        "current_node": current_node,
        # 任务输入修订号：携带旧修订的命令会被后端明确拒绝，页面必须据此刷新。
        "input_revision": task.input_revision,
        # 本任务实际执行范围：未配置 / 未启用 的区分依据。
        # **没有「自动补入」这一类**：范围就是创建任务时勾选的那些规则。
        "scope": {
            "enabled_rule_keys": scope_enabled_keys,
            "unenabled_rule_keys": scope_unenabled_keys,
            # 未配置的检查类别：类型级与实例级分开返回，页面按各自口径使用。
            "unconfigured_node_types": unconfigured_node_types,
            "unconfigured_node_codes": unconfigured_node_codes,
            "enabled_rules": [
                {"rule_key": key, "name": rule_name_by_key.get(key, key)} for key in scope_enabled_keys
            ],
            "unenabled_rules": [
                {"rule_key": key, "name": rule_name_by_key.get(key, key)} for key in scope_unenabled_keys
            ],
            "enabled_calculation_rules": [
                {"rule_key": item["rule_code"], "name": item.get("rule_name") or item["rule_code"]}
                for item in manifest.get("calculations", [])
                if item.get("status") != "DISABLED"
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
        # 两个口径分开：类型级说明「哪一类检查没配」，实例级让页面判断
        # 「当前这个节点是不是未配置的类别」——拿实例码与类型列表交叉比较恒为假。
        "unconfigured_check_node_types": unconfigured_node_types,
        "unconfigured_check_node_codes": unconfigured_node_codes,
        # 展示结构（需求 31~34）：**由当前任务的场景配置动态决定**，前端按它通用渲染。
        # 页面不按场景名称或固定条件分支写死展示内容；这里给出本任务真实使用的
        # 节点、Sheet、字段及其数据，统一协议里未被本场景选中的内容不出现。
        "display": _task_display_structure(task, manifest, nodes),
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
        # 全部待办（含不属于本批的批次）：仅供页面展示「还有多少项在排队」。
        # 提交必须用 `pending_reviews` / `pending_review_batch` 给出的本批集合。
        "all_pending_reviews": [
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
            for item in all_pending
        ],
        # 当前复核批次的身份：**提交校验、检查点校验、Agent 复核准备与前端提交
        # 用的是同一个集合**（`reviews._current_review_batch`）。
        # `stale=True` 说明本批哈希与进入等待时落库的哈希不一致（待办集合在等待期间
        # 被改动过），页面必须刷新后再提交，不能拿旧批次继续。
        "pending_review_batch": {
            "review_nos": [item.review_no for item in reviews],
            "count": len(reviews),
            "hash": batch_hash,
            "stale": bool(reviews) and batch_hash != task.pending_review_hash,
            "review_types": sorted({item.review_type for item in reviews}),
            "all_pending_count": len(all_pending),
        },
        "pending_review_hash": batch_hash,
        "pending_review_count": len(reviews),
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
