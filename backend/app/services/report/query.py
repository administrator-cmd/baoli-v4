"""审核报告列表、快照详情与当前性查询。"""

from datetime import datetime
from typing import Any
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from ...common.pagination import PageResult, page_result
from ...common.exceptions import BusinessError
from ...entity import AuditPlan, AuditReport, AuditTask, NodeExecution, ScenarioVersion


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
