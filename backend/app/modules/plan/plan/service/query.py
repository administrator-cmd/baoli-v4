"""方案管理列表及可用方案选项的只读查询：SQL 在 crud，本层负责组装视图。"""

from typing import Any

from sqlalchemy.orm import Session

from app.common.pagination import PageResult, page_result
from app.constants.status import SCENARIO_VERSION_STATUS_PUBLISHED
from app.domain.plan_calculations import plan_calculation_config
from app.domain.rule_library import active
from app.modules.plan.config.model import PlanMaterial, PlanRule
from app.modules.plan.plan import crud
from app.modules.plan.plan.model import AuditPlan
from app.services.shared import configuration as config_store


class PlanQueryService:
    """本实体的服务方法；db 由构造时注入。"""

    def __init__(self, db: Session | None = None) -> None:
        self.db = db

    def list_plans_page(self, *,
        name: str | None,
        scenario_code: str | None,
        enabled: bool | None,
        effective: bool | None,
        deleted: bool,
        page: int,
        page_size: int,
    ) -> PageResult[dict[str, Any]]:
        """方案分页列表（真实后端筛选与 total）。"""
        plan_ids, total = crud.PlanCRUD(self.db).plan_ids_page(
            name=name,
            scenario_code=scenario_code,
            enabled=enabled,
            effective=effective,
            deleted=deleted,
            page=page,
            page_size=page_size,
        )
        plans_by_id = crud.PlanCRUD(self.db).plans_by_ids( plan_ids)
        plans = [plans_by_id[plan_id] for plan_id in plan_ids]

        rule_counts: dict[int, int] = {}
        scenario_names: dict[str, str] = {}
        base_versions: dict[int, Any] = {}
        if plans:
            rule_counts = {
                plan.id: sum(
                    active({**(item.get("rule_json") or {}), "enabled": item["enabled"]})
                    for item in plan.config_json["rules"]
                )
                for plan in plans
            }
            scenario_names = crud.PlanCRUD(self.db).scenario_names( {plan.scenario_code for plan in plans})
            base_versions = crud.PlanCRUD(self.db).scenario_versions_by_ids( {plan.scenario_version_id for plan in plans if plan.scenario_version_id}
            )

        items = []
        for plan in plans:
            # 列表只用一个计数，不必为每个方案重建整套 task schema。
            rules = plan.manifest_json.get("rules") if isinstance(plan.manifest_json, dict) else None
            enabled_rule_count = sum(1 for item in rules if active(item)) if isinstance(rules, list) else 0
            context = plan.context_json if isinstance(plan.context_json, dict) else {}
            _, calculation_enabled = plan_calculation_config(context)
            base_version = base_versions.get(plan.scenario_version_id) if plan.scenario_version_id else None
            items.append(
                {
                    "plan_code": plan.plan_code,
                    "plan_name": plan.plan_name,
                    "scenario_code": plan.scenario_code,
                    "scenario_name": (
                        scenario_names[plan.scenario_code] if plan.scenario_code in scenario_names
                        else base_version.scenario_name if base_version else "来源场景已删除，未记录场景名称"
                    ),
                    "description": plan.description,
                    "enabled": plan.enabled,
                    "deleted": plan.deleted_at is not None,
                    "base_scenario_version_no": base_version.version_no if base_version else None,
                    "effective": plan.sync_error is None and plan.scope_hash is not None,
                    "sync_error": plan.sync_error,
                    "enabled_rule_count": enabled_rule_count,
                    "explicit_rule_count": rule_counts.get(plan.id, 0) + sum(calculation_enabled.values()),
                    "updated_at": plan.updated_at,
                }
            )
        return page_result(items, total, page, page_size)


    def list_plan_options(self, *, scenario_code: str | None) -> list[dict[str, Any]]:
        """下拉选项：启用且未删除、且范围已生效的方案。

        选项带 `scope_hash`：创建任务页据此提交并发保护，不再为拿范围哈希单独读一次方案详情。
        """
        return [
            {
                "plan_code": plan_code,
                "plan_name": plan_name,
                "scenario_code": row_scenario_code,
                "scenario_version_no": version_no,
                "scope_hash": scope_hash,
            }
            for plan_code, plan_name, row_scenario_code, version_no, scope_hash in crud.PlanCRUD(self.db).plan_option_rows( scenario_code=scenario_code
            )
        ]


    def plan_selection_view(self, plan: AuditPlan) -> dict[str, Any]:
        """方案视图。

        规则的「选没选」就是方案 JSON 中的 enabled。规则、材料、字段都从方案 config_json
        读取，不再从场景版本或配置明细表读，也不限制系统派生规则的选择。
        """
        rules = sorted(config_store.rows(self.db, PlanRule, plan.id), key=lambda item: (item.sort_no, item.rule_key,))
        materials = sorted(config_store.rows(self.db, PlanMaterial, plan.id), key=lambda item: (item.sort_no, item.id,))
        field_count = len(plan.config_json["fields"])
        context = plan.context_json if isinstance(plan.context_json, dict) else {}
        calculation_candidates, calculation_enabled = plan_calculation_config(context)
        calculation_source = calculation_candidates
        # 场景版本只是溯源：这份方案从哪一版复制而来，执行与范围都不读它。
        version = crud.PlanCRUD(self.db).scenario_version( plan.scenario_version_id) if plan.scenario_version_id else None
        return {
            "plan_code": plan.plan_code,
            "plan_name": plan.plan_name,
            "scenario_code": plan.scenario_code,
            "description": plan.description,
            "enabled": plan.enabled,
            "deleted": plan.deleted_at is not None,
            # 范围是否已生效：sync_error 非空表示最近一次保存没有算出可用范围，
            # 页面据此显示「待核对 + 原因」，不把不完整配置说成已生效。
            "effective": plan.sync_error is None and plan.scope_hash is not None,
            "sync_error": plan.sync_error,
            "scope_hash": plan.scope_hash,
            "base_scenario_version": (
                {
                    "version_no": version.version_no,
                    "status": SCENARIO_VERSION_STATUS_PUBLISHED,
                    "published_at": version.published_at,
                    "scenario_name": version.scenario_name,
                }
                if version
                else None
            ),
            # 方案自己的全部规则与启停：这就是「这个方案跑哪些规则」的唯一来源。
            "rules": [
                {
                    "rule_key": item.rule_key,
                    "name": item.name,
                    "node": item.node_code,
                    "enabled": active({**(item.rule_json or {}), "enabled": item.enabled}),
                }
                for item in rules if not item.rule_json.get("calculation_ref")
            ],
            "calculation_rules": [
                {
                    "rule_code": item["rule_code"],
                    "name": item.get("rule_name") or item["rule_code"],
                    "enabled": calculation_enabled[str(item["rule_code"])],
                }
                for item in calculation_source
            ],
            "materials": [
                {"material_key": item.material_key, "name": item.name, "required": bool(item.required)}
                for item in materials
            ],
            "field_count": int(field_count),
            "updated_at": plan.updated_at,
        }
