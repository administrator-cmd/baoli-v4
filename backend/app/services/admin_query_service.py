"""跨任务的管理列表查询：审核材料、证据、执行记录、人工复核、报告与风险结果。

全部使用真实后端筛选、排序与分页（total 为同一作用域下的总行数），不把当前页数组长度冒充
总数；读取失败由调用方明确报错，不用空数组冒充成功。

「当前 / 历史」的口径：材料以 `task_document.current_upload_id` 指向的上传为当前，其余是历史
版本；证据的当前 = 属于当前上传且仍支撑当前有效对象，失效只表示不再支撑当前结论、历史证据仍
可按历史身份查看；报告取该任务最新生成的一份，来源执行已失效或输入修订已变化标为已失效；
风险只取当前有效批次的真实风险规则结果，不按业务节点分类筛选。
"""

import logging
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session, aliased

from ..common.pagination import PageResult, page_result
from ..common.exceptions import BusinessError
from ..entity import ImportJob
from ..entity import (
    AuditPlan,
    AuditReport,
    AuditTask,
    BackgroundOperation,
    DocumentUpload,
    Evidence,
    NodeExecution,
    RuleResult,
    ScenarioNode,
    ScenarioTemplate,
    ScenarioVersion,
    TaskDocument,
    TaskFieldValue,
)
from . import task_value_service
from .operation_log_service import record_operation
from .operation_types import OBJECT_REPORT

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
) -> PageResult[dict[str, Any]]:
    """审核材料分页列表（跨任务）。"""
    conditions = []
    if task_no:
        conditions.append(AuditTask.task_no == task_no)
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
                "filename": upload.original_filename if upload else None,
                "size_bytes": upload.size_bytes if upload else None,
                "uploaded_at": upload.created_at if upload else None,
                "version_no": upload_ranks.get(upload.id) if upload else None,
                "version_count": upload_counts.get(upload.id, 0) if upload else None,
            }
        )
    return page_result(items, total, page, page_size)


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


# 证据记录（跨任务）


def list_evidence_page(
    db: Session,
    *,
    task_no: str | None,
    reference_type: str | None,
    keyword: str | None,
    page: int,
    page_size: int,
) -> PageResult[dict[str, Any]]:
    """证据记录分页列表（跨任务）。"""
    conditions = []
    if task_no:
        conditions.append(AuditTask.task_no == task_no)
    # 分类口径与报告「证据引用」章节一致：带 rule_result_id 的证据属于规则判定证据。
    # 规则执行时会把字段证据复制给规则结果（两个外键同时存在），只按 field_value_id 判类会
    # 把它误显示成“字段事实”，并与筛选条件互相矛盾。
    if reference_type == "FIELD_FACT":
        conditions.append(and_(Evidence.field_value_id.is_not(None), Evidence.rule_result_id.is_(None)))
    elif reference_type == "RULE_RESULT":
        conditions.append(Evidence.rule_result_id.is_not(None))
    elif reference_type == "ARTIFACT":
        conditions.append(and_(Evidence.field_value_id.is_(None), Evidence.rule_result_id.is_(None)))
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
                "quote_text": evidence.quote_text,
                "artifact_type": None,
                "historical": historical,
                "created_at": evidence.created_at,
            }
        )
    return page_result(items, total, page, page_size)


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
) -> PageResult[dict[str, Any]]:
    """节点执行记录分页列表（跨任务）。"""
    conditions = []
    if task_no:
        conditions.append(AuditTask.task_no == task_no)
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
    return page_result(items, total, page, page_size)


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
) -> PageResult[dict[str, Any]]:
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
    # 只取列表真正用到的列：import_job 带 proposals_json 等大 JSON 列，整行 SELECT * 会让
    # ORDER BY 的 filesort 把整行塞进排序缓冲（默认 256KB），两三条作业就报 1038
    # Out of sort memory；列表页从不读这些列。
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
                "active_operation_count": active_operation_counts.get(job.job_no, 0),
                "failure_code": job.failure_code,
                "failure_message": job.failure_message,
                "created_at": job.created_at,
                "updated_at": job.updated_at,
            }
        )
    return page_result(items, total, page, page_size)


def delete_import_job(db: Session, job_no: str) -> None:
    """直接删除验证项目中的导入作业。

    验证项目不保留导入来源追溯，也不因作业状态、场景草稿、发布版本或后台操作
    增加删除门禁。已发布版本解除对作业的来源引用后继续保留版本本身；来源字段允许置空，
    其他发布版本内容仍由数据库触发器保护。
    """
    job = db.scalar(select(ImportJob).where(ImportJob.job_no == job_no).with_for_update())
    if not job:
        raise BusinessError("IMPORT_JOB_NOT_FOUND", "导入作业不存在", status_code=404, details={"job_no": job_no})
    # scenario_version.import_job_id 允许为空；解除来源引用后即可物理删除作业。
    for version in db.scalars(
        select(ScenarioVersion).where(ScenarioVersion.import_job_id == job.id)
    ).all():
        version.import_job_id = None
    db.flush()
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
) -> PageResult[dict[str, Any]]:
    """审核报告分页列表（跨任务）。"""
    conditions = []
    if name:
        conditions.append(AuditReport.report_name.like(f"%{name.strip()}%"))
    if task_no:
        conditions.append(AuditTask.task_no == task_no)
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
                "generated_at": row.generated_at,
                "report_generated": row.report_generated,
                "scope_execution_complete": row.scope_execution_complete,
                "risk_conclusion_available": row.risk_conclusion_available,
                "status": row.status,
                "archived_at": row.archived_at,
                "current": current_map.get(row.audit_task_id) == row.id,
                "stale": stale,
                "input_revision": row.input_revision,
                "task_input_revision": row.task_input_revision,
                "remark": row.remark,
            }
        )
    return page_result(items, total, page, page_size)


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
    """按真实风险结果读取当前有效批次，不依赖业务节点类型。"""
    later_execution = aliased(NodeExecution)
    latest_execution = (
        select(func.max(later_execution.id))
        .where(
            later_execution.audit_task_id == NodeExecution.audit_task_id,
            later_execution.node_code == NodeExecution.node_code,
            later_execution.status != "INVALIDATED",
        )
        .correlate(NodeExecution)
        .scalar_subquery()
    )
    # 只取当前有效批次的风险项，风险结果包含 risk_level，即使等级未声明也不漏项。
    base_query = (
        select(RuleResult.id)
        .select_from(RuleResult)
        .join(NodeExecution, NodeExecution.id == RuleResult.node_execution_id)
        .join(AuditTask, AuditTask.id == RuleResult.audit_task_id)
        .join(ScenarioVersion, ScenarioVersion.id == AuditTask.scenario_version_id)
        .outerjoin(AuditPlan, AuditPlan.id == AuditTask.plan_id)
        .where(
            RuleResult.risk_level.is_not(None),
            NodeExecution.id == latest_execution,
        )
    )
    risk_level_expr = RuleResult.risk_level
    conditions = []
    if task_no:
        conditions.append(AuditTask.task_no == task_no)
    if scenario_code:
        conditions.append(ScenarioVersion.scenario_code == scenario_code)
    if plan_code:
        conditions.append(AuditPlan.plan_code == plan_code)
    if status:
        conditions.append(RuleResult.status == status)
    # 等级下拉的取值范围：与当前筛选同一作用域，但不含 risk_level 条件本身
    # （否则选定一个等级后下拉里只剩它自己）。取值范围必须来自真实数据：
    # 各场景可以使用自己的风险等级名称，查询不预设等级集合。
    level_conditions = list(conditions)
    if risk_level:
        conditions.append(risk_level_expr == risk_level)
    risk_levels = sorted(
        {
            str(item)
            for item in db.scalars(
                base_query.with_only_columns(risk_level_expr).distinct()
                .where(*level_conditions)
            ).all()
            if item
        }
    )

    total = db.scalar(
        base_query.with_only_columns(func.count())
        .where(*conditions)
    ) or 0
    # 只取列表真正用到的列：scenario_version 带 200KB+ 的 manifest_json，
    # 整行 SELECT * 会让 ORDER BY 的 filesort 把整行塞进排序缓冲（MySQL 默认 256KB），
    # 少量结果就报 1038 Out of sort memory；列表页不读 manifest_json。
    rows = db.execute(
        base_query.with_only_columns(
            RuleResult.id,
            RuleResult.audit_task_id,
            RuleResult.created_at,
            AuditTask.task_no,
            AuditTask.task_name,
            ScenarioVersion.scenario_name,
            ScenarioVersion.version_no,
            AuditPlan.plan_name,
            AuditPlan.id.label("plan_id"),
        )
        .where(*conditions)
        .order_by(RuleResult.created_at.desc(), RuleResult.id.desc())
        .limit(page_size)
        .offset((page - 1) * page_size)
    ).all()
    hit_count = db.scalar(
        base_query.with_only_columns(func.count())
        .where(*conditions, RuleResult.status == "ABNORMAL")
    ) or 0
    results_by_id = {result.id: result for result in db.scalars(
        select(RuleResult).where(RuleResult.id.in_([row.id for row in rows]))
    )}
    # 取值按任务分组的 CSV：一次读一份文件，不按行重复读。
    task_owners: dict[int, AuditTask] = {}
    for result in results_by_id.values():
        if result.audit_task_id in task_owners:
            continue
        owner = db.get(AuditTask, result.audit_task_id)
        if owner is None:
            raise BusinessError("AUDIT_TASK_NOT_FOUND", "规则结果所属审核任务不存在", status_code=500)
        task_owners[result.audit_task_id] = owner
    items = []
    for row in rows:
        result = results_by_id[row.id]
        # 风险等级是索引列，其余业务结论（处理建议、来源规则）在任务 CSV。
        data = task_value_service.value_of(db, task_owners[result.audit_task_id], task_value_service.RECORD_RULE_RESULT, result.id)
        items.append(
            {
                "task_no": row.task_no,
                "task_name": row.task_name,
                "scenario_name": row.scenario_name,
                "scenario_version_no": row.version_no,
                "plan_name": row.plan_name,
                "rule_key": result.rule_key,
                "name": result.rule_name,
                "risk_level": result.risk_level,
                "status": result.status,
                "message": result.message,
                "suggestion": data.get("action"),
                "source_rule_key": data.get("source_rule_key"),
                "source_rule_name": data.get("source_rule_name"),
                "result_id": row.id,
                "created_at": row.created_at,
            }
        )
    return {
        "items": items,
        "total": int(total),
        "hit_count": int(hit_count),
        "risk_levels": risk_levels,
        "page": page,
        "page_size": page_size,
    }
