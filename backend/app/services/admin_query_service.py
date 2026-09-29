"""跨任务的管理列表查询：审核材料、证据、执行记录、人工复核、报告与风险结果。

全部使用真实后端筛选、排序与分页（total 为同一作用域下的总行数），
不把当前页数组长度冒充总数；读取失败由调用方明确报错，不用空数组冒充成功。

“当前 / 历史”的口径：
- 材料：当前文件 = task_document.current_upload_id 指向的上传；其余上传是历史版本。
- 证据：当前 = 属于当前上传且仍支撑当前有效对象；失效只表示不再支撑当前结论，
  历史证据仍可按历史身份查看。
- 报告：当前 = 该任务最新生成的一份；来源执行已失效或输入修订已变化标为已失效。
- 风险：只取当前有效执行批次（RISK_RATING 节点最新的未失效执行）的规则结果。
"""

import logging
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import and_, delete as sa_delete, func, or_, select
from sqlalchemy.orm import Session

from fastapi.responses import FileResponse

from ..errors import BusinessError
from ..models import (
    AuditPlan,
    AuditReport,
    AuditTask,
    BackgroundOperation,
    DocumentUpload,
    Evidence,
    FieldCandidate,
    ImportIssue,
    ImportJob,
    ImportSheet,
    ManualReview,
    NodeExecution,
    RuleResult,
    ScenarioNode,
    ScenarioTemplate,
    ScenarioVersion,
    TaskDocument,
    TaskFieldValue,
)
from .operation_log_service import OBJECT_REPORT, record_operation

logger = logging.getLogger(__name__)


# 审核材料（跨任务）


def list_documents_page(
    db: Session,
    *,
    task_no: str | None,
    keyword: str | None,
    material_name: str | None,
    has_file: bool | None,
    page: int,
    page_size: int,
) -> dict[str, Any]:
    """审核材料分页列表（跨任务）。"""
    conditions = []
    if task_no:
        conditions.append(AuditTask.task_no == task_no)
    else:
        # 跨任务浏览默认隐藏已删除任务的记录；按任务编号查询（任务详情上下文）保持只读可见。
        conditions.append(AuditTask.deleted_at.is_(None))
    if keyword:
        like = f"%{keyword.strip()}%"
        conditions.append(
            or_(
                TaskDocument.material_name.like(like),
                TaskDocument.document_no.like(like),
                DocumentUpload.original_filename.like(like),
            )
        )
    if material_name:
        conditions.append(TaskDocument.material_name.like(f"%{material_name.strip()}%"))
    if has_file is not None:
        conditions.append(
            TaskDocument.current_upload_id.is_not(None) if has_file else TaskDocument.current_upload_id.is_(None)
        )

    count_statement = (
        select(func.count())
        .select_from(TaskDocument)
        .join(AuditTask, AuditTask.id == TaskDocument.audit_task_id)
        .outerjoin(DocumentUpload, DocumentUpload.id == TaskDocument.current_upload_id)
        .where(*conditions)
    )
    total = db.scalar(count_statement) or 0
    rows = db.execute(
        select(TaskDocument, AuditTask, DocumentUpload)
        .join(AuditTask, AuditTask.id == TaskDocument.audit_task_id)
        .outerjoin(DocumentUpload, DocumentUpload.id == TaskDocument.current_upload_id)
        .where(*conditions)
        .order_by(TaskDocument.updated_at.desc(), TaskDocument.id.desc())
        .limit(page_size)
        .offset((page - 1) * page_size)
    ).all()

    document_ids = [document.id for document, _, _ in rows]
    upload_ranks: dict[int, int] = {}
    upload_counts: dict[int, int] = {}
    if document_ids:
        ranks = db.execute(
            select(
                DocumentUpload.id,
                func.row_number()
                .over(partition_by=DocumentUpload.task_document_id, order_by=DocumentUpload.id)
                .label("rank"),
                func.count().over(partition_by=DocumentUpload.task_document_id).label("total"),
            ).where(DocumentUpload.task_document_id.in_(document_ids))
        ).all()
        for upload_id, rank, count in ranks:
            upload_ranks[int(upload_id)] = int(rank)
            upload_counts[int(upload_id)] = int(count)

    items = []
    for document, task, upload in rows:
        items.append(
            {
                "document_no": document.document_no,
                "task_no": task.task_no,
                "task_name": task.task_name,
                "material_key": document.material_key,
                "material_name": document.material_name,
                "required": document.required,
                "status": document.status,
                "upload_no": upload.upload_no if upload else None,
                "filename": upload.original_filename if upload else None,
                "size_bytes": upload.size_bytes if upload else None,
                "uploaded_at": upload.created_at if upload else None,
                "version_no": upload_ranks.get(upload.id) if upload else None,
                "version_count": upload_counts.get(upload.id, 0) if upload else None,
            }
        )
    return {"items": items, "total": int(total), "page": page, "page_size": page_size}


def document_detail(db: Session, document_no: str) -> dict[str, Any]:
    """材料位详情：当前文件与历史上传版本。"""
    document = db.scalar(select(TaskDocument).where(TaskDocument.document_no == document_no))
    if not document:
        raise BusinessError("TASK_DOCUMENT_NOT_FOUND", "材料位不存在", status_code=404, details={"document_no": document_no})
    task = db.get(AuditTask, document.audit_task_id)
    uploads = db.scalars(
        select(DocumentUpload)
        .where(DocumentUpload.task_document_id == document.id)
        .order_by(DocumentUpload.id)
    ).all()
    history = []
    for rank, upload in enumerate(uploads, start=1):
        history.append(
            {
                "upload_no": upload.upload_no,
                "filename": upload.original_filename,
                "size_bytes": upload.size_bytes,
                "content_type": upload.content_type,
                "status": upload.status,
                "is_current": upload.is_current,
                "version_no": rank,
                "created_at": upload.created_at,
            }
        )
    return {
        "document_no": document.document_no,
        "task_no": task.task_no if task else None,
        "task_name": task.task_name if task else None,
        "material_key": document.material_key,
        "material_name": document.material_name,
        "required": document.required,
        "status": document.status,
        "current_upload_no": next((item.upload_no for item in uploads if item.is_current), None),
        "uploads": list(reversed(history)),
    }


def document_upload_by_no(db: Session, document_no: str, upload_no: str) -> tuple[TaskDocument, DocumentUpload]:
    """按材料位与上传编号取上传记录；取不到即 404。"""
    document = db.scalar(select(TaskDocument).where(TaskDocument.document_no == document_no))
    if not document:
        raise BusinessError("TASK_DOCUMENT_NOT_FOUND", "材料位不存在", status_code=404, details={"document_no": document_no})
    upload = db.scalar(
        select(DocumentUpload).where(
            DocumentUpload.task_document_id == document.id,
            DocumentUpload.upload_no == upload_no,
        )
    )
    if not upload:
        raise BusinessError("DOCUMENT_UPLOAD_NOT_FOUND", "上传文件不存在", status_code=404, details={"upload_no": upload_no})
    return document, upload


def upload_file_response(db: Session, document_no: str, upload_no: str, *, preview: bool) -> FileResponse:
    """下载/预览一个上传版本的文件；文件缺失时明确报错，不返回空的伪文件。"""
    from .storage_service import resolve_storage_path

    _, upload = document_upload_by_no(db, document_no, upload_no)
    path = resolve_storage_path(upload.relative_path)
    if not path.exists():
        raise BusinessError(
            "DOCUMENT_FILE_MISSING",
            "该上传记录的文件在存储中不存在，请核对数据目录与备份是否对应同一时间点",
            status_code=404,
            details={"upload_no": upload_no, "filename": upload.original_filename},
        )
    return FileResponse(
        path,
        media_type=upload.content_type or "application/octet-stream",
        filename=upload.original_filename,
        content_disposition_type="inline" if preview else "attachment",
    )


# 证据记录（跨任务）


def list_evidence_page(
    db: Session,
    *,
    task_no: str | None,
    reference_type: str | None,
    locator_status: str | None,
    keyword: str | None,
    page: int,
    page_size: int,
) -> dict[str, Any]:
    """证据记录分页列表（跨任务）。"""
    conditions = []
    if task_no:
        conditions.append(AuditTask.task_no == task_no)
    else:
        # 跨任务浏览默认隐藏已删除任务的记录；按任务编号查询（任务详情上下文）保持只读可见。
        conditions.append(AuditTask.deleted_at.is_(None))
    # 分类口径与报告「证据引用」章节一致：带 rule_result_id 的证据属于规则判定证据。
    # 规则执行时会把字段证据复制给规则结果（两个外键同时存在），只按 field_value_id 判类会
    # 把它误显示成“字段事实”，并与筛选条件互相矛盾。
    if reference_type == "FIELD_FACT":
        conditions.append(and_(Evidence.field_value_id.is_not(None), Evidence.rule_result_id.is_(None)))
    elif reference_type == "RULE_RESULT":
        conditions.append(Evidence.rule_result_id.is_not(None))
    elif reference_type == "ARTIFACT":
        conditions.append(and_(Evidence.field_value_id.is_(None), Evidence.rule_result_id.is_(None)))
    if locator_status:
        conditions.append(Evidence.locator_status == locator_status)
    if keyword:
        conditions.append(Evidence.quote_text.like(f"%{keyword.strip()}%"))

    count_statement = (
        select(func.count())
        .select_from(Evidence)
        .join(AuditTask, AuditTask.id == Evidence.audit_task_id)
        .where(*conditions)
    )
    total = db.scalar(count_statement) or 0
    rows = db.execute(
        select(Evidence, AuditTask, DocumentUpload, TaskDocument, RuleResult, TaskFieldValue)
        .join(AuditTask, AuditTask.id == Evidence.audit_task_id)
        .outerjoin(DocumentUpload, DocumentUpload.id == Evidence.document_upload_id)
        .outerjoin(TaskDocument, TaskDocument.id == DocumentUpload.task_document_id)
        .outerjoin(RuleResult, RuleResult.id == Evidence.rule_result_id)
        .outerjoin(TaskFieldValue, TaskFieldValue.id == Evidence.field_value_id)
        .where(*conditions)
        .order_by(Evidence.created_at.desc(), Evidence.id.desc())
        .limit(page_size)
        .offset((page - 1) * page_size)
    ).all()

    # 字段名来自任务自己的执行快照（field_key → name），与当时版本一致。
    task_ids = {task.id for _, task, _, _, _, _ in rows}
    field_names: dict[tuple[int, str], str] = {}
    if task_ids:
        for task in db.scalars(select(AuditTask).where(AuditTask.id.in_(task_ids))).all():
            for field in task.task_schema_json.get("fields", []):
                field_names[(task.id, field["field_key"])] = field["name"]

    items = []
    for evidence, task, upload, document, rule_result, fact in rows:
        # 规则证据优先：与报告口径一致（见上方筛选注释）。
        if rule_result is not None:
            reference_type_value = "RULE_RESULT"
            object_name = rule_result.rule_name
            material_name = None
        elif fact is not None:
            reference_type_value = "FIELD_FACT"
            object_name = field_names.get((task.id, fact.field_key), fact.field_key)
            material_name = document.material_name if document else None
        else:
            reference_type_value = "ARTIFACT"
            object_name = None
            material_name = document.material_name if document else None
        is_current_upload = bool(document and upload and document.current_upload_id == upload.id)
        is_current_target = False
        if rule_result is not None:
            is_current_target = (
                db.scalar(
                    select(NodeExecution.id).where(
                        NodeExecution.id == rule_result.node_execution_id,
                        NodeExecution.status != "INVALIDATED",
                    )
                )
                is not None
            )
        elif fact is not None:
            # 结构化字段的一条事实可以有多条属性证据，不能只把摘要证据标成当前。
            is_current_target = bool(fact.is_current)
        historical = not (is_current_upload and is_current_target)
        items.append(
            {
                "evidence_no": evidence.evidence_no,
                "task_no": task.task_no,
                "task_name": task.task_name,
                "reference_type": reference_type_value,
                "object_name": object_name,
                "material_name": material_name,
                "filename": upload.original_filename if upload else None,
                "document_upload_no": upload.upload_no if upload else None,
                "field_path": evidence.field_path,
                "page_no": evidence.page_no,
                "quote_text": evidence.quote_text,
                "locator_status": evidence.locator_status,
                "locator_note": evidence.locator_note,
                "artifact_type": None,
                "historical": historical,
                "created_at": evidence.created_at,
            }
        )
    return {"items": items, "total": int(total), "page": page, "page_size": page_size}


# 执行记录（跨任务）


def list_executions_page(
    db: Session,
    *,
    task_no: str | None,
    node_code: str | None,
    status: str | None,
    started_from: datetime | None,
    started_to: datetime | None,
    page: int,
    page_size: int,
) -> dict[str, Any]:
    """节点执行记录分页列表（跨任务）。"""
    conditions = []
    if task_no:
        conditions.append(AuditTask.task_no == task_no)
    else:
        conditions.append(AuditTask.deleted_at.is_(None))
    if node_code:
        conditions.append(NodeExecution.node_code == node_code)
    if status:
        conditions.append(NodeExecution.status == status)
    if started_from:
        conditions.append(NodeExecution.started_at >= started_from)
    if started_to:
        conditions.append(NodeExecution.started_at <= started_to)

    total = db.scalar(
        select(func.count())
        .select_from(NodeExecution)
        .join(AuditTask, AuditTask.id == NodeExecution.audit_task_id)
        .where(*conditions)
    ) or 0
    # 只取列表真正用到的列：scenario_version 带 200KB+ 的 manifest_json、audit_task 带
    # task_schema_json / checkpoint_json。整行 SELECT * 会让 ORDER BY 的 filesort 把整行
    # 塞进排序缓冲（MySQL 默认 256KB），少量执行记录就报 1038 Out of sort memory；
    # 列表页从不读这些大列。
    rows = db.execute(
        select(
            NodeExecution.id,
            NodeExecution.audit_task_id,
            NodeExecution.node_code,
            NodeExecution.sequence_no,
            NodeExecution.status,
            NodeExecution.started_at,
            NodeExecution.finished_at,
            NodeExecution.failure_code,
            NodeExecution.failure_message,
            NodeExecution.result_json,
            AuditTask.task_no,
            AuditTask.task_name,
            ScenarioNode.name.label("node_name"),
            ScenarioVersion.scenario_name,
            ScenarioVersion.version_no,
        )
        .join(AuditTask, AuditTask.id == NodeExecution.audit_task_id)
        .join(ScenarioVersion, ScenarioVersion.id == NodeExecution.scenario_version_id)
        .outerjoin(
            ScenarioNode,
            (ScenarioNode.scenario_version_id == NodeExecution.scenario_version_id)
            & (ScenarioNode.node_code == NodeExecution.node_code),
        )
        .where(*conditions)
        .order_by(NodeExecution.created_at.desc(), NodeExecution.id.desc())
        .limit(page_size)
        .offset((page - 1) * page_size)
    ).all()
    items = []
    for row in rows:
        duration_ms = None
        if row.started_at and row.finished_at:
            duration_ms = int((row.finished_at - row.started_at).total_seconds() * 1000)
        items.append(
            {
                "execution_id": row.id,
                "task_no": row.task_no,
                "task_name": row.task_name,
                "scenario_name": row.scenario_name,
                "scenario_version_no": row.version_no,
                "node_code": row.node_code,
                "node_name": row.node_name or row.node_code,
                "sequence_no": row.sequence_no,
                "status": row.status,
                "started_at": row.started_at,
                "finished_at": row.finished_at,
                "duration_ms": duration_ms,
                "failure_code": row.failure_code,
                "failure_message": row.failure_message,
                "result_summary": row.result_json if isinstance(row.result_json, dict) else None,
            }
        )
    return {"items": items, "total": int(total), "page": page, "page_size": page_size}


def execution_detail(db: Session, execution_id: int) -> dict[str, Any]:
    """节点执行详情：输入快照来源、结果、错误与关联的后台命令编号。"""
    execution = db.get(NodeExecution, execution_id)
    if not execution:
        raise BusinessError(
            "NODE_EXECUTION_NOT_FOUND",
            "执行记录不存在",
            status_code=404,
            details={"object_no": str(execution_id)},
        )
    task = db.get(AuditTask, execution.audit_task_id)
    scenario = db.get(ScenarioVersion, execution.scenario_version_id)
    node = db.scalar(
        select(ScenarioNode).where(
            ScenarioNode.scenario_version_id == execution.scenario_version_id,
            ScenarioNode.node_code == execution.node_code,
        )
    )
    related_operation = db.scalar(
        select(BackgroundOperation)
        .where(
            BackgroundOperation.object_no == (task.task_no if task else ""),
            BackgroundOperation.target_node == execution.node_code,
        )
        .order_by(BackgroundOperation.id.desc())
        .limit(1)
    )
    duration_ms = None
    if execution.started_at and execution.finished_at:
        duration_ms = int((execution.finished_at - execution.started_at).total_seconds() * 1000)
    return {
        "execution_id": execution.id,
        "task_no": task.task_no if task else None,
        "task_name": task.task_name if task else None,
        "task_status": task.status if task else None,
        "task_current_node": task.current_node if task else None,
        "input_revision": task.input_revision if task else None,
        "plan_bound": bool(task and task.plan_id) if task else None,
        "scenario_name": scenario.scenario_name if scenario else None,
        "scenario_version_no": scenario.version_no if scenario else None,
        "node_code": execution.node_code,
        "node_name": node.name if node else execution.node_code,
        "sequence_no": execution.sequence_no,
        "status": execution.status,
        "started_at": execution.started_at,
        "finished_at": execution.finished_at,
        "duration_ms": duration_ms,
        "failure_code": execution.failure_code,
        "failure_message": execution.failure_message,
        "result_summary": execution.result_json if isinstance(execution.result_json, dict) else None,
        "operation_no": related_operation.operation_no if related_operation else None,
        "created_at": execution.created_at,
    }


# 人工复核（跨任务）


def list_reviews_page(
    db: Session,
    *,
    task_no: str | None,
    review_type: str | None,
    status: str | None,
    updated_from: datetime | None,
    updated_to: datetime | None,
    page: int,
    page_size: int,
) -> dict[str, Any]:
    """人工复核分页列表（跨任务）：含已复核与已失效记录。"""
    conditions = []
    if task_no:
        conditions.append(AuditTask.task_no == task_no)
    else:
        conditions.append(AuditTask.deleted_at.is_(None))
    if review_type:
        conditions.append(ManualReview.review_type == review_type)
    if status:
        conditions.append(ManualReview.status == status)
    if updated_from:
        conditions.append(ManualReview.updated_at >= updated_from)
    if updated_to:
        conditions.append(ManualReview.updated_at <= updated_to)

    total = db.scalar(
        select(func.count())
        .select_from(ManualReview)
        .join(AuditTask, AuditTask.id == ManualReview.audit_task_id)
        .where(*conditions)
    ) or 0
    # 只取列表真正用到的列：scenario_version 带 200KB+ 的 manifest_json，
    # 整行 SELECT * 会让 ORDER BY 的 filesort 把整行塞进排序缓冲（MySQL 默认 256KB），
    # 即使只有 1 行待复核也会报 1038 Out of sort memory；列表页不读 manifest_json。
    # 注意 AuditTask 的 task_schema_json 不在这里取：字段中文名由下面的独立查询读取，
    # 保留它会让 filesort 重新背上 36KB 的大列。
    rows = db.execute(
        select(
            ManualReview.id,
            ManualReview.review_no,
            ManualReview.audit_task_id,
            ManualReview.review_type,
            ManualReview.status,
            ManualReview.system_judgment_json,
            ManualReview.business_note,
            ManualReview.decision,
            ManualReview.basis_type,
            ManualReview.before_value,
            ManualReview.after_value,
            ManualReview.updated_at,
            AuditTask.task_no,
            AuditTask.task_name,
            RuleResult.rule_name,
            RuleResult.status.label("rule_status"),
            TaskFieldValue.field_key,
            TaskFieldValue.status.label("fact_status"),
            DocumentUpload.id.label("upload_id"),
            TaskDocument.material_name,
            TaskDocument.status.label("document_status"),
            ScenarioVersion.scenario_name,
        )
        .join(AuditTask, AuditTask.id == ManualReview.audit_task_id)
        .join(ScenarioVersion, ScenarioVersion.id == AuditTask.scenario_version_id)
        .outerjoin(RuleResult, RuleResult.id == ManualReview.rule_result_id)
        .outerjoin(TaskFieldValue, TaskFieldValue.id == ManualReview.field_value_id)
        .outerjoin(DocumentUpload, DocumentUpload.id == ManualReview.document_upload_id)
        .outerjoin(TaskDocument, TaskDocument.id == DocumentUpload.task_document_id)
        .where(*conditions)
        .order_by(ManualReview.updated_at.desc(), ManualReview.id.desc())
        .limit(page_size)
        .offset((page - 1) * page_size)
    ).all()

    task_ids = {row.audit_task_id for row in rows}
    field_names: dict[tuple[int, str], str] = {}
    task_status_by_id: dict[int, str] = {}
    task_current_node: dict[int, str] = {}
    if task_ids:
        for task in db.scalars(select(AuditTask).where(AuditTask.id.in_(task_ids))).all():
            task_status_by_id[task.id] = task.status
            task_current_node[task.id] = task.current_node
            for field in task.task_schema_json.get("fields", []):
                field_names[(task.id, field["field_key"])] = field["name"]

    items = []
    for row in rows:
        if row.rule_name is not None:
            object_name = f"规则：{row.rule_name}"
            effective_status = row.rule_status
        elif row.field_key is not None:
            object_name = f"字段：{field_names.get((row.audit_task_id, row.field_key), row.field_key)}"
            effective_status = row.fact_status
        elif row.upload_id is not None:
            object_name = f"材料：{row.material_name}"
            effective_status = row.document_status
        else:
            object_name = "未关联业务对象"
            effective_status = None
        judgment = row.system_judgment_json if isinstance(row.system_judgment_json, dict) else {}
        items.append(
            {
                "review_no": row.review_no,
                "task_no": row.task_no,
                "task_name": row.task_name,
                "scenario_name": row.scenario_name,
                "review_type": row.review_type,
                "object_name": object_name,
                "status": row.status,
                "system_judgment_status": judgment.get("status"),
                "system_judgment_message": judgment.get("message") or row.business_note,
                # 取证检索与印章记录是主体核对复核的判据，列表投影不能把它丢掉。
                "system_judgment_retrieval": judgment.get("retrieval"),
                "system_judgment_seal_records": judgment.get("seal_records"),
                "decision": row.decision,
                "effective_status": effective_status,
                "basis_type": row.basis_type,
                "before_value": row.before_value,
                "after_value": row.after_value,
                "task_status": task_status_by_id.get(row.audit_task_id),
                "task_current_node": task_current_node.get(row.audit_task_id),
                "updated_at": row.updated_at,
            }
        )
    return {"items": items, "total": int(total), "page": page, "page_size": page_size}


# 规则库导入作业（管理列表）


def list_import_jobs_page(
    db: Session,
    *,
    filename: str | None,
    status: str | None,
    created_from: datetime | None,
    created_to: datetime | None,
    page: int,
    page_size: int,
    scenario_code: str | None = None,
) -> dict[str, Any]:
    """导入作业分页列表（管理端）。"""
    conditions = []
    if scenario_code:
        conditions.append(ImportJob.scenario_code == scenario_code)
    if filename:
        conditions.append(ImportJob.source_filename.like(f"%{filename.strip()}%"))
    if status:
        conditions.append(ImportJob.status == status)
    if created_from:
        conditions.append(ImportJob.created_at >= created_from)
    if created_to:
        conditions.append(ImportJob.created_at <= created_to)

    count_statement = select(func.count()).select_from(ImportJob)
    # 只取列表真正用到的列：import_job 带 workbook_ir_json / proposals_json / coverage_json
    # 等大 JSON 列，整行 SELECT * 会让 ORDER BY 的 filesort 把整行塞进排序缓冲
    # （默认 256KB），两三条作业就报 1038 Out of sort memory；列表页从不读这些列。
    rows_statement = select(
        ImportJob.id,
        ImportJob.job_no,
        ImportJob.source_filename,
        ImportJob.status,
        ImportJob.profile_version,
        # 作业所属场景：列表页显示「哪个场景的规则库」要用它，字段很小不影响排序缓冲。
        ImportJob.scenario_code,
        ImportJob.draft_revision,
        ImportJob.failure_code,
        ImportJob.failure_message,
        ImportJob.created_at,
        ImportJob.updated_at,
    )
    if conditions:
        count_statement = count_statement.where(*conditions)
        rows_statement = rows_statement.where(*conditions)
    total = db.scalar(count_statement) or 0
    rows = db.execute(
        rows_statement.order_by(ImportJob.created_at.desc(), ImportJob.id.desc()).limit(page_size).offset((page - 1) * page_size)
    ).all()

    job_ids = [row.id for row in rows]
    # 场景名一次查出，逐行查会让列表页变成 N+1 次查询。
    scenario_codes = sorted({row.scenario_code for row in rows if row.scenario_code})
    scenario_names = (
        {
            code: name
            for code, name in db.execute(
                select(ScenarioTemplate.scenario_code, ScenarioTemplate.scenario_name).where(
                    ScenarioTemplate.scenario_code.in_(scenario_codes)
                )
            ).all()
        }
        if scenario_codes
        else {}
    )
    issue_counts: dict[int, int] = {}
    if job_ids:
        for job_id, count in db.execute(
            select(ImportIssue.import_job_id, func.count())
            .where(ImportIssue.import_job_id.in_(job_ids), ImportIssue.status == "OPEN")
            .group_by(ImportIssue.import_job_id)
        ).all():
            issue_counts[int(job_id)] = int(count)

    # 转换期间的作业状态不会变（retry_conversion 全程持有作业行锁、结束时才提交），
    # 所以「有没有未结束的后台操作」只能问 background_operation，不能靠作业状态猜。
    # 列表只用来决定「中止」按钮是否可用，因此只回活跃操作数，不回整个操作对象。
    active_operation_counts: dict[str, int] = {}
    job_nos = [row.job_no for row in rows]
    if job_nos:
        for object_no, count in db.execute(
            select(BackgroundOperation.object_no, func.count())
            .where(
                BackgroundOperation.object_type == "IMPORT_JOB",
                BackgroundOperation.object_no.in_(job_nos),
                BackgroundOperation.status.in_(("PENDING", "RUNNING")),
            )
            .group_by(BackgroundOperation.object_no)
        ).all():
            active_operation_counts[str(object_no)] = int(count)

    items = []
    for job in rows:
        items.append(
            {
                "job_no": job.job_no,
                "source_filename": job.source_filename,
                "status": job.status,
                "profile_version": job.profile_version,
                # 作业所属场景：上传时必须指定，因此列表能直接看出这份规则库属于哪个场景。
                # 历史作业没有这一列，如实返回 None（页面显示「未记录场景」），
                # 不按表头或文件名替它补一个。
                "scenario_code": job.scenario_code,
                "scenario_name": scenario_names.get(job.scenario_code),
                "draft_revision": job.draft_revision,
                "pending_issue_count": issue_counts.get(job.id, 0),
                "active_operation_count": active_operation_counts.get(job.job_no, 0),
                "failure_code": job.failure_code,
                "failure_message": job.failure_message,
                "created_at": job.created_at,
                "updated_at": job.updated_at,
            }
        )
    return {"items": items, "total": int(total), "page": page, "page_size": page_size}


def delete_import_job(db: Session, job_no: str) -> None:
    """删除导入作业（仅限未发布的草稿作业）。
    已发布作业（或已经产生场景版本的作业）不删除：删除会破坏版本来源追溯。
    有活跃后台转换时同样拒绝——先等待或按其真实状态处理。
    """
    job = db.scalar(select(ImportJob).where(ImportJob.job_no == job_no).with_for_update())
    if not job:
        raise BusinessError("IMPORT_JOB_NOT_FOUND", "导入作业不存在", status_code=404, details={"job_no": job_no})
    if job.status in {"PUBLISHED", "APPLIED"}:
        raise BusinessError(
            "IMPORT_JOB_PUBLISHED",
            "该作业已写入场景草稿或已产生场景版本，不能删除；配置来源需要保留追溯",
            status_code=409,
            details={"job_no": job_no, "status": job.status},
        )
    published = db.scalar(select(ScenarioVersion.id).where(ScenarioVersion.import_job_id == job.id).limit(1))
    if published is not None:
        raise BusinessError(
            "IMPORT_JOB_PUBLISHED",
            "该作业已产生场景版本，不能删除；已发布版本及其来源需要保留追溯",
            status_code=409,
            details={"job_no": job_no},
        )
    active = db.scalar(
        select(BackgroundOperation.operation_no).where(
            BackgroundOperation.object_type == "IMPORT_JOB",
            BackgroundOperation.object_no == job_no,
            BackgroundOperation.status.in_(("PENDING", "RUNNING")),
        )
    )
    if active is not None:
        raise BusinessError(
            "OPERATION_ALREADY_ACTIVE",
            "该导入作业有正在进行的转换操作，请等待完成或查询其状态",
            status_code=409,
            details={"job_no": job_no, "operation_no": active},
        )
    # 子表必须先于父表删除：三个模型与 ImportJob 之间只有外键列、没有 relationship()，
    # unit-of-work 在同一次 flush 里不保证先发子表 DELETE，FK RESTRICT 会让父表删除报 500。
    # 这里用 Core 批量删除立即执行，顺序确定。
    for model in (FieldCandidate, ImportIssue, ImportSheet):
        db.execute(sa_delete(model).where(model.import_job_id == job.id))
    db.delete(job)
    record_operation(
        db,
        object_type="IMPORT_JOB",
        object_no=job_no,
        object_name=job.source_filename,
        operation_type="DELETE",
        detail={"job_no": job_no},
    )
    db.commit()


# 审核报告（跨任务）


def _report_current_map(db: Session, task_ids: set[int]) -> dict[int, int]:
    if not task_ids:
        return {}
    rows = db.execute(
        select(AuditReport.audit_task_id, func.max(AuditReport.id))
        .where(AuditReport.audit_task_id.in_(task_ids))
        .group_by(AuditReport.audit_task_id)
    ).all()
    return {int(task_id): int(report_id) for task_id, report_id in rows}


def list_reports_page(
    db: Session,
    *,
    name: str | None,
    task_no: str | None,
    scenario_code: str | None,
    plan_code: str | None,
    archived: bool | None,
    generated_from: datetime | None,
    generated_to: datetime | None,
    page: int,
    page_size: int,
) -> dict[str, Any]:
    """审核报告分页列表（跨任务）。"""
    conditions = []
    if name:
        conditions.append(AuditReport.report_name.like(f"%{name.strip()}%"))
    if task_no:
        conditions.append(AuditTask.task_no == task_no)
    else:
        conditions.append(AuditTask.deleted_at.is_(None))
    if scenario_code:
        conditions.append(ScenarioVersion.scenario_code == scenario_code)
    if plan_code:
        conditions.append(AuditPlan.plan_code == plan_code)
    if archived is not None:
        conditions.append(
            AuditReport.archived_at.is_not(None) if archived else AuditReport.archived_at.is_(None)
        )
    if generated_from:
        conditions.append(AuditReport.generated_at >= generated_from)
    if generated_to:
        conditions.append(AuditReport.generated_at <= generated_to)

    total = db.scalar(
        select(func.count())
        .select_from(AuditReport)
        .join(AuditTask, AuditTask.id == AuditReport.audit_task_id)
        .join(ScenarioVersion, ScenarioVersion.id == AuditTask.scenario_version_id)
        .outerjoin(AuditPlan, AuditPlan.id == AuditTask.plan_id)
        .where(*conditions)
    ) or 0
    # 只取列表真正用到的列：scenario_version 带 200KB+ 的 manifest_json、
    # node_execution 带 result_json。整行 SELECT * 会让 ORDER BY 的 filesort 把整行
    # 塞进排序缓冲（MySQL 默认 256KB），即使只有 1 份报告也会报 1038 Out of sort memory。
    rows = db.execute(
        select(
            AuditReport.id,
            AuditReport.audit_task_id,
            AuditReport.report_no,
            AuditReport.report_name,
            AuditReport.generated_at,
            AuditReport.report_generated,
            AuditReport.scope_execution_complete,
            AuditReport.risk_conclusion_available,
            AuditReport.status,
            AuditReport.archived_at,
            AuditReport.origin,
            AuditReport.input_revision,
            AuditReport.remark,
            AuditTask.task_no,
            AuditTask.task_name,
            AuditTask.input_revision.label("task_input_revision"),
            ScenarioVersion.scenario_name,
            ScenarioVersion.version_no,
            AuditPlan.plan_name,
            AuditPlan.id.label("plan_id"),
            NodeExecution.status.label("execution_status"),
        )
        .join(AuditTask, AuditTask.id == AuditReport.audit_task_id)
        .join(ScenarioVersion, ScenarioVersion.id == AuditTask.scenario_version_id)
        .outerjoin(AuditPlan, AuditPlan.id == AuditTask.plan_id)
        .outerjoin(NodeExecution, NodeExecution.id == AuditReport.node_execution_id)
        .where(*conditions)
        .order_by(AuditReport.generated_at.desc(), AuditReport.id.desc())
        .limit(page_size)
        .offset((page - 1) * page_size)
    ).all()
    current_map = _report_current_map(db, {row.audit_task_id for row in rows})
    items = []
    for row in rows:
        source_invalidated = row.execution_status == "INVALIDATED"
        stale = source_invalidated or (
            row.input_revision is not None and row.input_revision != row.task_input_revision
        )
        items.append(
            {
                "report_no": row.report_no,
                "report_name": row.report_name,
                "task_no": row.task_no,
                "task_name": row.task_name,
                "scenario_name": row.scenario_name,
                "scenario_version_no": row.version_no,
                "plan_name": row.plan_name,
                "is_legacy_config": row.plan_id is None,
                "generated_at": row.generated_at,
                "report_generated": row.report_generated,
                "scope_execution_complete": row.scope_execution_complete,
                "risk_conclusion_available": row.risk_conclusion_available,
                "status": row.status,
                "archived_at": row.archived_at,
                "current": current_map.get(row.audit_task_id) == row.id,
                "stale": stale,
                "origin": row.origin,
                "input_revision": row.input_revision,
                "task_input_revision": row.task_input_revision,
                "remark": row.remark,
            }
        )
    return {"items": items, "total": int(total), "page": page, "page_size": page_size}


def report_or_404(db: Session, report_no: str) -> AuditReport:
    """按报告编号取报告快照；取不到即 404。"""
    report = db.scalar(select(AuditReport).where(AuditReport.report_no == report_no))
    if not report:
        raise BusinessError("REPORT_NOT_FOUND", "审核报告不存在", status_code=404, details={"report_no": report_no})
    return report


def report_detail(db: Session, report_no: str) -> dict[str, Any]:
    """报告详情：正文、三信号、范围快照与结构化结论。"""
    report = report_or_404(db, report_no)
    task = db.get(AuditTask, report.audit_task_id)
    scenario = db.get(ScenarioVersion, task.scenario_version_id) if task else None
    plan = db.get(AuditPlan, task.plan_id) if task and task.plan_id else None
    execution = db.get(NodeExecution, report.node_execution_id) if report.node_execution_id else None
    stale = (execution is not None and execution.status == "INVALIDATED") or (
        task is not None and report.input_revision is not None and report.input_revision != task.input_revision
    )
    return {
        "report_no": report.report_no,
        "report_name": report.report_name,
        "task_no": task.task_no if task else None,
        "task_name": task.task_name if task else None,
        "scenario_name": scenario.scenario_name if scenario else None,
        "scenario_version_no": scenario.version_no if scenario else None,
        "plan_name": plan.plan_name if plan else None,
        "generated_at": report.generated_at,
        "content_type": report.content_type,
        "content": report.content_md,
        "report_generated": report.report_generated,
        "scope_execution_complete": report.scope_execution_complete,
        "risk_conclusion_available": report.risk_conclusion_available,
        "reasons": report.reasons_json or {},
        "snapshot": report.snapshot_json,
        "status": report.status,
        "archived_at": report.archived_at,
        "remark": report.remark,
        "origin": report.origin,
        "stale": stale,
        "input_revision": report.input_revision,
        "task_input_revision": task.input_revision if task else None,
    }


def update_report_remark(db: Session, report_no: str, remark: str | None) -> AuditReport:
    """只更新报告备注，不动正文与快照。"""
    report = report_or_404(db, report_no)
    report.remark = (str(remark).strip() or None) if remark is not None else None
    task = db.get(AuditTask, report.audit_task_id)
    record_operation(
        db,
        object_type=OBJECT_REPORT,
        object_no=report.report_no,
        object_name=report.report_name,
        operation_type="REPORT_REMARK",
        detail={"report_no": report.report_no, "task_no": task.task_no if task else None},
    )
    db.commit()
    db.refresh(report)
    return report


def set_report_archived(db: Session, report_no: str, archived: bool) -> AuditReport:
    """归档 / 取消归档报告快照；不删除任何历史快照。"""
    report = report_or_404(db, report_no)
    if archived and report.archived_at is not None:
        raise BusinessError("REPORT_ALREADY_ARCHIVED", "报告已归档", status_code=409, details={"report_no": report_no})
    if not archived and report.archived_at is None:
        raise BusinessError("REPORT_NOT_ARCHIVED", "报告未归档，无需取消归档", status_code=409, details={"report_no": report_no})
    report.archived_at = datetime.now(UTC) if archived else None
    report.status = "ARCHIVED" if archived else "GENERATED"
    task = db.get(AuditTask, report.audit_task_id)
    record_operation(
        db,
        object_type=OBJECT_REPORT,
        object_no=report.report_no,
        object_name=report.report_name,
        operation_type="REPORT_ARCHIVE" if archived else "REPORT_UNARCHIVE",
        detail={"report_no": report.report_no, "task_no": task.task_no if task else None},
    )
    db.commit()
    db.refresh(report)
    return report


# 风险结果（跨任务，只取当前有效执行批次）


def list_risk_results_page(
    db: Session,
    *,
    task_no: str | None,
    scenario_code: str | None,
    plan_code: str | None,
    risk_level: str | None,
    status: str | None,
    page: int,
    page_size: int,
) -> dict[str, Any]:
    """风险结果分页列表（跨任务，只取当前有效执行批次）。

    归属按**能力类型**判定：`node_execution.node_code` 是用户配置的实例码，
    拿它与能力类型比较在用户改了实例码之后一条风险都查不到。这里按
    `scenario_node.node_type == RISK_RATING` 先解析出该版本下的全部风险节点实例，
    再取这些实例的未失效执行。
    """
    latest_execution = (
        select(func.max(NodeExecution.id))
        .join(
            ScenarioNode,
            and_(
                ScenarioNode.scenario_version_id == NodeExecution.scenario_version_id,
                ScenarioNode.node_code == NodeExecution.node_code,
                ScenarioNode.node_type == "RISK_RATING",
            ),
        )
        .where(
            NodeExecution.audit_task_id == RuleResult.audit_task_id,
            NodeExecution.status != "INVALIDATED",
        )
        .correlate(RuleResult)
        .scalar_subquery()
    )
    risk_level_expr = func.json_unquote(func.json_extract(RuleResult.result_json, "$.risk_level"))
    conditions = [RuleResult.node_execution_id == latest_execution]
    if task_no:
        conditions.append(AuditTask.task_no == task_no)
    else:
        # 跨任务浏览默认隐藏已删除任务的记录；按任务编号查询（任务详情上下文）保持只读可见。
        conditions.append(AuditTask.deleted_at.is_(None))
    if scenario_code:
        conditions.append(ScenarioVersion.scenario_code == scenario_code)
    if plan_code:
        conditions.append(AuditPlan.plan_code == plan_code)
    # 等级下拉的取值范围：与当前筛选同一作用域，但**不含** risk_level 条件本身
    # （否则选定一个等级后下拉里只剩它自己）。取值范围必须来自真实数据：
    # 页面硬编码旧四级「高/中/低/待确认」时，节点版「最高/一级/二级/三级」一条都筛不出来，
    # 而页面看起来一切正常。
    level_conditions = list(conditions)
    if risk_level:
        conditions.append(risk_level_expr == risk_level)
    if status:
        conditions.append(RuleResult.status == status)
    risk_levels = sorted(
        {
            str(item)
            for item in db.scalars(
                select(func.distinct(risk_level_expr))
                .select_from(RuleResult)
                .join(AuditTask, AuditTask.id == RuleResult.audit_task_id)
                .join(ScenarioVersion, ScenarioVersion.id == AuditTask.scenario_version_id)
                .outerjoin(AuditPlan, AuditPlan.id == AuditTask.plan_id)
                .where(*level_conditions)
            ).all()
            if item
        }
    )

    total = db.scalar(
        select(func.count())
        .select_from(RuleResult)
        .join(AuditTask, AuditTask.id == RuleResult.audit_task_id)
        .join(ScenarioVersion, ScenarioVersion.id == AuditTask.scenario_version_id)
        .outerjoin(AuditPlan, AuditPlan.id == AuditTask.plan_id)
        .where(*conditions)
    ) or 0
    # 只取列表真正用到的列：scenario_version 带 200KB+ 的 manifest_json，
    # 整行 SELECT * 会让 ORDER BY 的 filesort 把整行塞进排序缓冲（MySQL 默认 256KB），
    # 少量结果就报 1038 Out of sort memory；列表页不读 manifest_json。
    rows = db.execute(
        select(
            RuleResult.id,
            RuleResult.rule_key,
            RuleResult.rule_name,
            RuleResult.status,
            RuleResult.message,
            RuleResult.result_json,
            RuleResult.created_at,
            AuditTask.task_no,
            AuditTask.task_name,
            ScenarioVersion.scenario_name,
            ScenarioVersion.version_no,
            AuditPlan.plan_name,
            AuditPlan.id.label("plan_id"),
        )
        .join(AuditTask, AuditTask.id == RuleResult.audit_task_id)
        .join(ScenarioVersion, ScenarioVersion.id == AuditTask.scenario_version_id)
        .outerjoin(AuditPlan, AuditPlan.id == AuditTask.plan_id)
        .where(*conditions)
        .order_by(RuleResult.created_at.desc(), RuleResult.id.desc())
        .limit(page_size)
        .offset((page - 1) * page_size)
    ).all()
    hit_count = db.scalar(
        select(func.count())
        .select_from(RuleResult)
        .join(AuditTask, AuditTask.id == RuleResult.audit_task_id)
        .join(ScenarioVersion, ScenarioVersion.id == AuditTask.scenario_version_id)
        .outerjoin(AuditPlan, AuditPlan.id == AuditTask.plan_id)
        .where(*conditions, RuleResult.status == "ABNORMAL")
    ) or 0
    items = []
    for row in rows:
        data = row.result_json if isinstance(row.result_json, dict) else {}
        items.append(
            {
                "task_no": row.task_no,
                "task_name": row.task_name,
                "scenario_name": row.scenario_name,
                "scenario_version_no": row.version_no,
                "plan_name": row.plan_name,
                "is_legacy_config": row.plan_id is None,
                "rule_key": row.rule_key,
                "name": row.rule_name,
                "risk_level": data.get("risk_level"),
                "status": row.status,
                "message": row.message,
                "suggestion": data.get("action"),
                "source_rule_key": data.get("source_rule_key"),
                "source_rule_name": data.get("source_rule_name"),
                "result_id": row.id,
                "created_at": row.created_at,
            }
        )
    # 等级下拉的取值范围：与当前筛选同一作用域，但**不含** risk_level 条件本身
    # （否则选定一个等级后下拉里只剩它自己）。取值范围必须来自真实数据：
    # 页面硬编码旧四级「高/中/低/待确认」时，节点版「最高/一级/二级/三级」一条都筛不出来，
    # 而页面看起来一切正常。
    level_conditions = [item for item in conditions if item is not None][: len(conditions)]
    conditions = conditions + [risk_level_expr == risk_level] if risk_level else conditions
    levels = db.scalars(
        select(func.distinct(risk_level_expr))
        .select_from(RuleResult)
        .join(AuditTask, AuditTask.id == RuleResult.audit_task_id)
        .join(ScenarioVersion, ScenarioVersion.id == AuditTask.scenario_version_id)
        .outerjoin(AuditPlan, AuditPlan.id == AuditTask.plan_id)
        .where(*level_conditions)
    ).all()
    risk_levels = sorted({str(item) for item in levels if item})
    return {
        "items": items,
        "total": int(total),
        "hit_count": int(hit_count),
        "risk_levels": risk_levels,
        "page": page,
        "page_size": page_size,
    }
