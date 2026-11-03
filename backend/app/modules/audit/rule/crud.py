"""规则结果与执行的 SQL 层。"""

from sqlalchemy import or_, select

from app.core.base_crud import CRUDBase
from app.modules.audit.field.crud import AuditFieldCRUD
from app.modules.audit.field.model import TaskFact
from app.modules.audit.rule.model import RuleResult


class AuditRuleCRUD(CRUDBase):
    model = RuleResult

    def context_rows(
        self,
        *,
        task_id: int,
        selected_keys,
        execution_id: int | None,
        sources,
        codes,
        report: bool,
        definitions,
        owner_keys,
    ):
        """规则上下文的上游事实与结果；报告规则只取当前批次的已选结果。"""
        fact_query = select(TaskFact).where(
            TaskFact.audit_task_id == task_id,
            TaskFact.is_current.is_(True),
        )
        result_query = select(RuleResult).where(
            RuleResult.audit_task_id == task_id,
            RuleResult.rule_key.in_(selected_keys),
            RuleResult.node_execution_id == execution_id,
        )
        if not report:
            fact_query = fact_query.where(or_(
                AuditFieldCRUD.extracted_identity_filter(definitions, owner_keys=owner_keys),
                (TaskFact.fact_kind == "DERIVED") & TaskFact.fact_code.in_(codes),
            ))
            result_query = result_query.where(or_(
                RuleResult.rule_key.in_(sources), RuleResult.rule_code.in_(codes),
            ))
        facts = list(self.db.scalars(fact_query.order_by(TaskFact.id)))
        results = list({r.rule_key: r for r in self.db.scalars(result_query.order_by(RuleResult.id))}.values())
        return facts, results

    def results_for_scope(self, task_id: int, scope, execution_id: int) -> list[RuleResult]:
        return list(self.db.scalars(
            select(RuleResult).where(
                RuleResult.audit_task_id == task_id,
                RuleResult.rule_key.in_(scope),
                RuleResult.node_execution_id == execution_id,
            ).order_by(RuleResult.id)
        ).all())

    def current_extracted_facts_by_codes(self, task_id: int, fact_codes) -> list[TaskFact]:
        return list(self.db.scalars(select(TaskFact).where(
            TaskFact.audit_task_id == task_id,
            TaskFact.fact_kind == "EXTRACTED",
            TaskFact.fact_code.in_(fact_codes),
            TaskFact.is_current.is_(True),
            TaskFact.source_execution_id.is_not(None),
        )).all())

    def derived_facts_for_result(
        self, task_id: int, *, result_code: str, rule_code: str | None, execution_id: int,
    ) -> list[TaskFact]:
        return list(self.db.scalars(select(TaskFact).where(
            TaskFact.audit_task_id == task_id,
            TaskFact.fact_code == result_code,
            TaskFact.fact_kind == "DERIVED",
            TaskFact.calculation_rule_code == rule_code,
            TaskFact.source_execution_id == execution_id,
            TaskFact.is_current.is_(True),
        )).all())

    def current_facts_for_definitions(self, task_id: int, definitions, owner_keys) -> list[TaskFact]:
        return list(self.db.scalars(select(TaskFact).where(
            TaskFact.audit_task_id == task_id,
            TaskFact.is_current.is_(True),
            AuditFieldCRUD.extracted_identity_filter(definitions, owner_keys=owner_keys),
        ).order_by(TaskFact.id)))

    def current_derived_facts(self, task_id: int, result_codes, report: bool) -> list[TaskFact]:
        query = select(TaskFact).where(
            TaskFact.audit_task_id == task_id,
            TaskFact.is_current.is_(True),
            TaskFact.fact_kind == "DERIVED",
        )
        if not report:
            query = query.where(TaskFact.fact_code.in_(result_codes))
        return list(self.db.scalars(query.order_by(TaskFact.id)))

    def results_for_execution(self, execution_id: int) -> list[RuleResult]:
        return list(self.db.scalars(
            select(RuleResult).where(RuleResult.node_execution_id == execution_id)
        ).all())

    def results_for_executions(self, execution_ids: list[int]) -> list[RuleResult]:
        """多个执行批次的规则结果；列表页用它按批次统计执行概况与模型轮次。"""
        if not execution_ids:
            return []
        return list(self.db.scalars(
            select(RuleResult)
            .where(RuleResult.node_execution_id.in_(execution_ids))
            .order_by(RuleResult.id)
        ).all())

    def by_execution_rule(self, execution_id: int, rule_key: str) -> RuleResult | None:
        return self.db.scalar(
            select(RuleResult).where(
                RuleResult.node_execution_id == execution_id,
                RuleResult.rule_key == rule_key,
            )
        )

    def by_ids(self, result_ids: list[int]) -> list[RuleResult]:
        if not result_ids:
            return []
        return list(self.db.scalars(select(RuleResult).where(RuleResult.id.in_(result_ids)).execution_options(populate_existing=True)).all())

    def results_for_task(self, task_id: int, execution_ids, rule_keys=None) -> list[RuleResult]:
        query = select(RuleResult).where(
            RuleResult.audit_task_id == task_id,
            RuleResult.node_execution_id.in_(execution_ids),
        )
        if rule_keys is not None:
            query = query.where(RuleResult.rule_key.in_(rule_keys))
        return list(self.db.scalars(query.order_by(RuleResult.id)).all())

    def ids_for_executions(self, execution_ids: list[int]) -> list[int]:
        return list(self.db.scalars(
            select(RuleResult.id).where(RuleResult.node_execution_id.in_(execution_ids))
        ).all())
