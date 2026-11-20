"""审核报告列表、快照详情与当前性查询：SQL 在 crud，本层负责组装视图。"""

from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from app.common.exceptions import BusinessError
from app.common.pagination import PageResult, page_result
from app.modules.audit.execution.service.execution_scope import current_execution_ids, pending_rerun_by_execution
from app.modules.audit.report import crud
from app.modules.audit.report.model import AuditReport
from app.modules.audit.report.service.validity import AuditReportValidityService


class AuditReportQueryService:
    """本实体的服务方法；db 由构造时注入。"""

    def __init__(self, db: Session | None = None) -> None:
        self.db = db

    def list_reports_page(self, *,
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
        rows, total = crud.AuditReportCRUD(self.db).report_page_rows(
            name=name,
            task_no=task_no,
            task_name=task_name,
            scenario_code=scenario_code,
            plan_code=plan_code,
            archived=archived,
            generated_from=generated_from,
            generated_to=generated_to,
            page=page,
            page_size=page_size,
        )
        task_ids = {row.audit_task_id for row in rows}
        scope_signals = {
            row.id: AuditReportValidityService().scope_signals(row.manifest_json, set(row.enabled_keys), row.reasons_json)
            for row in crud.AuditReportCRUD(self.db).status_context_rows([row.id for row in rows])
        }
        latest_report_map = crud.AuditReportCRUD(self.db).current_report_map(task_ids)
        current_ids = current_execution_ids(self.db, task_ids)
        pending_by_execution = pending_rerun_by_execution(
            self.db, {row.node_execution_id for row in rows if row.node_execution_id},
        )
        signals = AuditReportValidityService(self.db).result_signals(
             [(row.node_execution_id, row.snapshot_rule_key) for row in rows],
        )
        items = []
        for row in rows:
            reason = AuditReportValidityService().report_current_reason(
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
                    **scope_signals[row.id],
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


    def report_or_404(self, report_no: str) -> AuditReport:
        """按报告编号取报告快照；取不到即 404。"""
        report = crud.AuditReportCRUD(self.db).report_by_no(report_no)
        if not report:
            raise BusinessError("REPORT_NOT_FOUND", "审核报告不存在", status_code=404, details={"report_no": report_no})
        return report


    def report_detail(self, report_no: str) -> dict[str, Any]:
        """报告详情：正文、三信号、范围快照与结构化结论。"""
        report = self.report_or_404( report_no)
        task, scenario, plan, execution = crud.AuditReportCRUD(self.db).report_context(report)
        current_ids = current_execution_ids(
            self.db, [report.audit_task_id], node_codes=[execution.node_code],
        ) if execution is not None else set()
        pending = pending_rerun_by_execution(self.db, [execution.id]).get(execution.id, set()) if execution else set()
        snapshot = report.snapshot_json or {}
        rule_key = snapshot.get("rule_key")
        signal = AuditReportValidityService(self.db).result_signals( [(report.node_execution_id, rule_key)]).get((report.node_execution_id, rule_key))
        reason = AuditReportValidityService().report_current_reason(
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
            **AuditReportValidityService().scope_signals(
                task.manifest_json, set(task.task_schema_json["enabled_rule_keys"]), report.reasons_json,
            ),
            "current": crud.AuditReportCRUD(self.db).current_report_map({report.audit_task_id}).get(
                (report.audit_task_id, rule_key)
            ) == report.id,
            "reasons": report.reasons_json or {},
            "snapshot": report.snapshot_json,
            "report_generation": snapshot.get("report_generation"),
            "status": "ARCHIVED" if report.archived_at else "GENERATED",
            "archived_at": report.archived_at,
            "remark": report.remark,
            "stale": stale,
            "stale_reason": reason,
            "input_revision": report.input_revision,
            "task_input_revision": task.input_revision if task else None,
        }
