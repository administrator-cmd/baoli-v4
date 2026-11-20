"""报告查询的 SQL 层：只取行与实体，不组装视图。"""

from datetime import datetime

from sqlalchemy import func, select

from app.core.base_crud import CRUDBase
from app.modules.audit.execution.crud import AuditExecutionCRUD
from app.modules.audit.execution.model import NodeExecution
from app.modules.audit.execution.service.execution_scope import json_text
from app.modules.audit.report.model import AuditReport
from app.modules.audit.task.crud import AuditTaskCRUD
from app.modules.audit.task.model import AuditTask
from app.modules.plan.plan.crud import PlanCRUD
from app.modules.plan.plan.model import AuditPlan
from app.modules.scenario.template.model import ScenarioVersion
from app.modules.scenario.template.service.crud import ScenarioTemplateCRUD


class AuditReportCRUD(CRUDBase):
    model = AuditReport

    def current_report_map(self, task_ids: set[int]) -> dict[tuple[int, str | None], int]:
        """每（任务, 冻结报告定义）最新一份快照的 id。

        同一任务可以有多个报告定义，各自保留自己的最新版本；按整个任务取最大 ID 会把
        除最后一份以外的有效报告都标成非当前。
        """
        if not task_ids:
            return {}
        rule_key = json_text(AuditReport.snapshot_json, "$.rule_key")
        rows = self.db.execute(
            select(AuditReport.audit_task_id, rule_key, func.max(AuditReport.id))
            .where(AuditReport.audit_task_id.in_(task_ids))
            .group_by(AuditReport.audit_task_id, rule_key)
        ).all()
        return {(int(task_id), key): int(report_id) for task_id, key, report_id in rows}

    def report_page_rows(
        self,
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
    ):
        """报告分页行；只取列表用到的列，避免大 JSON/正文进入 filesort。"""
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

        base = (
            select(func.count())
            .select_from(AuditReport)
            .join(AuditTask, AuditTask.id == AuditReport.audit_task_id)
            .join(ScenarioVersion, ScenarioVersion.id == AuditTask.scenario_version_id)
            .outerjoin(AuditPlan, AuditPlan.id == AuditTask.plan_id)
            .where(*conditions)
        )
        total = self.db.scalar(base) or 0
        # 只取列表真正用到的列：scenario_version 带 200KB+ 的 manifest_json、
        # node_execution 带 result_json。整行 SELECT * 会让 ORDER BY 的 filesort 把整行
        # 塞进排序缓冲（MySQL 默认 256KB），即使只有 1 份报告也会报 1038 Out of sort memory。
        rows = self.db.execute(
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
        return list(rows), int(total)

    def report_by_no(self, report_no: str) -> AuditReport | None:
        return self.db.scalar(select(AuditReport).where(AuditReport.report_no == report_no))

    def status_context_rows(self, report_ids: list[int]):
        # 分页完成后再读取原因与冻结配置，避免大 JSON 进入列表的排序缓冲。
        return self.db.execute(
            select(AuditReport.id, AuditReport.reasons_json, AuditTask.manifest_json,
                   AuditTask.task_schema_json["enabled_rule_keys"].label("enabled_keys"))
            .join(AuditTask, AuditTask.id == AuditReport.audit_task_id)
            .where(AuditReport.id.in_(report_ids))
        ).all()

    def report_context(
        self, report: AuditReport
    ) -> tuple[AuditTask | None, ScenarioVersion | None, AuditPlan | None, NodeExecution | None]:
        """报告关联的任务、场景版本、方案与执行记录。"""
        task = AuditTaskCRUD(self.db).get_of(AuditTask, report.audit_task_id)
        scenario = (
            ScenarioTemplateCRUD(self.db).get_of(ScenarioVersion, task.scenario_version_id) if task else None
        )
        plan = PlanCRUD(self.db).get_of(AuditPlan, task.plan_id) if task and task.plan_id else None
        execution = (
            AuditExecutionCRUD(self.db).get_of(NodeExecution, report.node_execution_id)
            if report.node_execution_id else None
        )
        return task, scenario, plan, execution

    def by_task_execution_rule(self, task_id: int, execution_id: int, rule_key: str) -> AuditReport | None:
        rows = self.db.scalars(select(AuditReport).where(
            AuditReport.audit_task_id == task_id,
            AuditReport.node_execution_id == execution_id,
        )).all()
        return next((row for row in rows if (row.snapshot_json or {}).get("rule_key") == rule_key), None)

    def risk_page_rows(
        self,
        *,
        task_no: str | None,
        task_name: str | None,
        scenario_code: str | None,
        plan_code: str | None,
        risk_level: str | None,
        status: str | None,
        page: int,
        page_size: int,
    ):
        """风险结果分页：当前有效批次、风险角色、排除待重跑。"""
        from app.modules.audit.execution.service.execution_scope import (
            current_execution_subquery,
            pending_rerun_predicate,
        )
        from app.modules.audit.rule.model import RuleResult

        latest_execution = current_execution_subquery(
            NodeExecution.audit_task_id, NodeExecution.node_code,
        )
        risk_rule = RuleResult.rule_role == "RISK"
        base_query = (
            select(RuleResult.id)
            .select_from(RuleResult)
            .join(NodeExecution, NodeExecution.id == RuleResult.node_execution_id)
            .join(AuditTask, AuditTask.id == RuleResult.audit_task_id)
            .join(ScenarioVersion, ScenarioVersion.id == AuditTask.scenario_version_id)
            .outerjoin(AuditPlan, AuditPlan.id == AuditTask.plan_id)
            .where(
                risk_rule,
                NodeExecution.id == latest_execution,
                ~pending_rerun_predicate(RuleResult.rule_key),
            )
        )
        risk_level_expr = RuleResult.risk_level
        conditions = []
        if task_no:
            conditions.append(AuditTask.task_no == task_no)
        if task_name:
            conditions.append(AuditTask.task_name.like(f"%{task_name.strip()}%"))
        if scenario_code:
            conditions.append(ScenarioVersion.scenario_code == scenario_code)
        if plan_code:
            conditions.append(AuditPlan.plan_code == plan_code)
        if status:
            conditions.append(RuleResult.status == status)
        level_conditions = list(conditions)
        if risk_level:
            conditions.append(risk_level_expr == risk_level)
        risk_levels = sorted({
            str(item)
            for item in self.db.scalars(
                base_query.with_only_columns(risk_level_expr).distinct().where(*level_conditions)
            ).all()
            if item
        })
        total = self.db.scalar(
            base_query.with_only_columns(func.count()).where(*conditions)
        ) or 0
        rows = self.db.execute(
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
        hit_count = self.db.scalar(
            base_query.with_only_columns(func.count())
            .where(*conditions, RuleResult.status == "ABNORMAL")
        ) or 0
        return risk_levels, int(total), list(rows), int(hit_count)

    def result_signal_rows(self, pairs: set[tuple[int, str]]):
        from sqlalchemy import tuple_

        from app.modules.audit.rule.model import RuleResult

        return list(self.db.execute(
            select(
                RuleResult.node_execution_id,
                RuleResult.rule_key,
                RuleResult.status,
                RuleResult.loop_status,
                json_text(RuleResult.result_json, "$.evaluation_id").label("evaluation_id"),
            ).where(tuple_(RuleResult.node_execution_id, RuleResult.rule_key).in_(pairs))
        ).all())

    def reports_for_task(self, task_id: int) -> list[AuditReport]:
        return list(self.db.scalars(
            select(AuditReport).where(AuditReport.audit_task_id == task_id).order_by(AuditReport.id.desc())
        ).all())
