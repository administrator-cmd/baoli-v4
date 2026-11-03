"""方案查询的 SQL 层：只取主键、实体与行，不组装视图。"""

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.engine import Row

from app.core.base_crud import CRUDBase
from app.modules.plan.plan.model import AuditPlan
from app.modules.scenario.template.model import ScenarioTemplate, ScenarioVersion


class PlanCRUD(CRUDBase):
    model = AuditPlan

    def by_code(
        self, plan_code: str, *, lock: bool = False, include_deleted: bool = False,
    ) -> AuditPlan | None:
        statement = select(AuditPlan).where(AuditPlan.plan_code == plan_code)
        if not include_deleted:
            statement = statement.where(AuditPlan.deleted_at.is_(None))
        if lock:
            statement = statement.with_for_update()
        return self.db.scalar(statement)

    def effective_predicate(self):
        """「范围已生效」的 SQL 判据：sync_error 为空且有 scope_hash。

        注意：sync_error 是 JSON 列，Python 的 None 会存成 JSON 字面量 null，
        MySQL 里 `sync_error IS NULL` 对它返回假（只有 SQL NULL 才为真），
        用它做过滤会把全部生效方案都排除掉。必须按 JSON 类型判断。
        """
        return (func.json_type(AuditPlan.sync_error) == "NULL") & AuditPlan.scope_hash.is_not(None)


    def plan_ids_page(self, *,
        name: str | None,
        scenario_code: str | None,
        enabled: bool | None,
        effective: bool | None,
        deleted: bool,
        page: int,
        page_size: int,
    ) -> tuple[list[int], int]:
        """按筛选条件分页取方案主键，避免大 JSON 列参与 filesort。"""
        conditions = [AuditPlan.deleted_at.is_not(None) if deleted else AuditPlan.deleted_at.is_(None)]
        if name:
            conditions.append(AuditPlan.plan_name.like(f"%{name.strip()}%"))
        if scenario_code:
            conditions.append(AuditPlan.scenario_code == scenario_code)
        if enabled is not None:
            conditions.append(AuditPlan.enabled.is_(enabled))
        if effective is not None:
            is_effective = self.effective_predicate()
            conditions.append(is_effective if effective else ~is_effective)

        total = self.db.scalar(select(func.count()).select_from(AuditPlan).where(*conditions)) or 0
        plan_ids = self.db.scalars(
            select(AuditPlan.id)
            .where(*conditions)
            .order_by(AuditPlan.updated_at.desc(), AuditPlan.id.desc())
            .limit(page_size)
            .offset((page - 1) * page_size)
        ).all()
        return list(plan_ids), int(total)


    def plans_by_ids(self, plan_ids: list[int]) -> dict[int, AuditPlan]:
        if not plan_ids:
            return {}
        return {
            plan.id: plan
            for plan in self.db.scalars(select(AuditPlan).where(AuditPlan.id.in_(plan_ids))).all()
        }


    def scenario_names(self, codes: set[str]) -> dict[str, str]:
        if not codes:
            return {}
        return dict(
            self.db.execute(
                select(ScenarioTemplate.scenario_code, ScenarioTemplate.scenario_name)
                .where(ScenarioTemplate.scenario_code.in_(codes))
            ).all()
        )


    def scenario_versions_by_ids(self, version_ids: set[int]) -> dict[int, Any]:
        """来源版本只取三列：manifest_json 单份可达 200KB，列表不需要。"""
        if not version_ids:
            return {}
        rows = self.db.execute(
            select(ScenarioVersion.id, ScenarioVersion.version_no, ScenarioVersion.scenario_name)
            .where(ScenarioVersion.id.in_(version_ids))
        ).all()
        return {row.id: row for row in rows}


    def plan_option_rows(self, *, scenario_code: str | None) -> list[Row]:
        """下拉只需要几个轻量字段；整行读取会触发 MySQL 1038。"""
        conditions = [
            AuditPlan.deleted_at.is_(None),
            AuditPlan.enabled.is_(True),
            self.effective_predicate(),
        ]
        if scenario_code:
            conditions.append(AuditPlan.scenario_code == scenario_code)
        return list(
            self.db.execute(
                select(
                    AuditPlan.plan_code,
                    AuditPlan.plan_name,
                    AuditPlan.scenario_code,
                    ScenarioVersion.version_no,
                    AuditPlan.scope_hash,
                )
                .outerjoin(ScenarioVersion, ScenarioVersion.id == AuditPlan.scenario_version_id)
                .where(*conditions)
                .order_by(AuditPlan.plan_name, AuditPlan.id)
            ).all()
        )


    def scenario_version(self, version_id: int) -> ScenarioVersion | None:
        return self.db.get(ScenarioVersion, version_id)
