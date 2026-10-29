"""审核报告列表、快照详情与当前性查询。"""

from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ...common.exceptions import BusinessError
from ...common.pagination import PageResult, page_result
from ...entity.plans import AuditPlan
from ...entity.reports import AuditReport
from ...entity.scenarios import ScenarioVersion
from ...entity.tasks import AuditTask, NodeExecution
from ..audit.execution_scope import (
    current_execution_ids,
    json_text,
    pending_rerun_by_execution,
)
from .validity import report_current_reason, result_signals


def _report_current_map(db: Session, task_ids: set[int]) -> dict[tuple[int, str | None], int]:
    """每（任务, 冻结报告定义）最新一份快照的 id。

    同一任务可以有多个报告定义，各自保留自己的最新版本；按整个任务取最大 ID 会把
    除最后一份以外的有效报告都标成非当前。
    """
    if not task_ids:
        return {}
    rule_key = json_text(AuditReport.snapshot_json, "$.rule_key")
    rows = db.execute(
        select(AuditReport.audit_task_id, rule_key, func.max(AuditReport.id))
        .where(AuditReport.audit_task_id.in_(task_ids))
        .group_by(AuditReport.audit_task_id, rule_key)
    ).all()
    return {(int(task_id), key): int(report_id) for task_id, key, report_id in rows}


def list_reports_page(
    db: Session,
    *,
    name: str | None,
    task_no: str | None,
    task_name: str | None,
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
    if task_name:
        conditions.append(AuditTask.task_name.like(f"%{task_name.strip()}%"))
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
            AuditReport.scope_execution_complete,
            AuditReport.risk_conclusion_available,
            AuditReport.archived_at,
            AuditReport.input_revision,
            AuditReport.node_execution_id,
            # 快照只取判定用的两个标量：整列 JSON 会重新进入 ORDER BY 的 filesort 缓冲。
            json_text(AuditReport.snapshot_json, "$.rule_key").label("snapshot_rule_key"),
            json_text(AuditReport.snapshot_json, "$.evaluation_id").label("snapshot_evaluation_id"),
            # 正文是否存在：正文是 MEDIUMTEXT，列表只读长度，不载入内容。
            (func.length(AuditReport.content_md) > 0).label("has_content"),
            AuditReport.remark,
            AuditTask.task_no,
            AuditTask.task_name,
            AuditTask.input_revision.label("task_input_revision"),
            ScenarioVersion.scenario_name,
            ScenarioVersion.version_no,
            AuditPlan.plan_name,
            AuditPlan.id.label("plan_id"),
        )
        .join(AuditTask, AuditTask.id == AuditReport.audit_task_id)
        .join(ScenarioVersion, ScenarioVersion.id == AuditTask.scenario_version_id)
        .outerjoin(AuditPlan, AuditPlan.id == AuditTask.plan_id)
        .where(*conditions)
        .order_by(AuditReport.generated_at.desc(), AuditReport.id.desc())
        .limit(page_size)
        .offset((page - 1) * page_size)
    ).all()
    task_ids = {row.audit_task_id for row in rows}
    latest_report_map = _report_current_map(db, task_ids)
    current_ids = current_execution_ids(db, task_ids)
    pending_by_execution = pending_rerun_by_execution(
        db, {row.node_execution_id for row in rows if row.node_execution_id},
    )
    signals = result_signals(
        db, [(row.node_execution_id, row.snapshot_rule_key) for row in rows],
    )
    items = []
    for row in rows:
        reason = report_current_reason(
            report_input_revision=row.input_revision,
            input_revision=row.task_input_revision,
            execution_id=row.node_execution_id,
            current_ids=current_ids,
            rule_key=row.snapshot_rule_key,
            pending_rerun_keys=pending_by_execution.get(row.node_execution_id, set()),
            snapshot_evaluation_id=row.snapshot_evaluation_id,
            result=signals.get((row.node_execution_id, row.snapshot_rule_key)),
        )
        stale = reason is not None
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
                "report_generated": bool(row.has_content),
                # 范围完整与风险可判定是报告生成时的真实记录，不随快照失效改写。
                "scope_execution_complete": row.scope_execution_complete,
                "risk_conclusion_available": row.risk_conclusion_available,
                "status": "ARCHIVED" if row.archived_at else "GENERATED",
                "archived_at": row.archived_at,
                "current": latest_report_map.get((row.audit_task_id, row.snapshot_rule_key)) == row.id,
                "stale": stale,
                "stale_reason": reason,
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
    current_ids = current_execution_ids(
        db, [report.audit_task_id], node_codes=[execution.node_code],
    ) if execution is not None else set()
    pending = pending_rerun_by_execution(db, [execution.id]).get(execution.id, set()) if execution else set()
    snapshot = report.snapshot_json or {}
    rule_key = snapshot.get("rule_key")
    signal = result_signals(db, [(report.node_execution_id, rule_key)]).get((report.node_execution_id, rule_key))
    reason = report_current_reason(
        report_input_revision=report.input_revision,
        input_revision=task.input_revision if task else None,
        execution_id=report.node_execution_id,
        current_ids=current_ids,
        rule_key=rule_key,
        pending_rerun_keys=pending,
        snapshot_evaluation_id=snapshot.get("evaluation_id"),
        result=signal,
    )
    stale = reason is not None
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
        "report_generated": bool(report.content_md),
        # 范围完整与风险可判定是报告生成时的真实记录，不随快照失效改写。
        "scope_execution_complete": report.scope_execution_complete,
        "risk_conclusion_available": report.risk_conclusion_available,
        "reasons": report.reasons_json or {},
        "snapshot": report.snapshot_json,
        "status": "ARCHIVED" if report.archived_at else "GENERATED",
        "archived_at": report.archived_at,
        "remark": report.remark,
        "stale": stale,
        "stale_reason": reason,
        "input_revision": report.input_revision,
        "task_input_revision": task.input_revision if task else None,
    }
