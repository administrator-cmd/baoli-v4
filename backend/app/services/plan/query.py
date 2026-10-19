"""方案管理列表及可用方案选项的只读查询。"""

from typing import Any
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from ...common.pagination import PageResult, page_result
from ...domain.plan_calculations import plan_calculation_config
from ...domain.rule_library import active
from ...entity import AuditPlan, PlanRule, PlanMaterial, ScenarioTemplate, ScenarioVersion
from ...repositories.shared import configuration as config_store


def _effective_predicate():
    """「范围已生效」的 SQL 判据：sync_error 为空且有 scope_json。

    注意：sync_error 是 JSON 列，Python 的 None 会存成 JSON 字面量 null，
    MySQL 里 `sync_error IS NULL` 对它返回假（只有 SQL NULL 才为真），
    用它做过滤会把全部生效方案都排除掉。必须按 JSON 类型判断。
    scope_json 同理，用 JSON_TYPE 而不是 IS NOT NULL。
    """
    return (func.json_type(AuditPlan.sync_error) == "NULL") & (func.json_type(AuditPlan.scope_json) != "NULL")


def list_plans_page(
    db: Session,
    *,
    name: str | None,
    scenario_code: str | None,
    enabled: bool | None,
    effective: bool | None,
    deleted: bool,
    page: int,
    page_size: int,
) -> PageResult[dict[str, Any]]:
    """方案分页列表（真实后端筛选与 total）。"""
    conditions = [AuditPlan.deleted_at.is_not(None) if deleted else AuditPlan.deleted_at.is_(None)]
    if name:
        conditions.append(AuditPlan.plan_name.like(f"%{name.strip()}%"))
    if scenario_code:
        conditions.append(AuditPlan.scenario_code == scenario_code)
    if enabled is not None:
        conditions.append(AuditPlan.enabled.is_(enabled))
    if effective is not None:
        is_effective = _effective_predicate()
        conditions.append(is_effective if effective else ~is_effective)

    total = db.scalar(select(func.count()).select_from(AuditPlan).where(*conditions)) or 0
    # 先只按主键分页排序，再按主键读取完整方案。
    # AuditPlan 含 manifest/context/scope 等大 JSON 列；直接 SELECT AuditPlan
    # 并 ORDER BY updated_at 会把整行带入 MySQL filesort，方案保存后列表容易报
    # 1038 Out of sort memory。轻量主键排序后再回表，避免大 JSON 参与排序。
    ordered_plan_ids = db.scalars(
        select(AuditPlan.id)
        .where(*conditions)
        .order_by(AuditPlan.updated_at.desc(), AuditPlan.id.desc())
        .limit(page_size)
        .offset((page - 1) * page_size)
    ).all()

    if not ordered_plan_ids:
        plans = []
    else:
        plans_by_id = {
            plan.id: plan
            for plan in db.scalars(select(AuditPlan).where(AuditPlan.id.in_(ordered_plan_ids))).all()
        }
        plans = [plans_by_id[plan_id] for plan_id in ordered_plan_ids]

    plan_ids = [item.id for item in plans]
    rule_counts: dict[int, int] = {}
    scenario_names: dict[str, str] = {}
    if plan_ids:
        rule_counts = {
            plan.id: sum(active({**(item.get("rule_json") or {}), "enabled": item["enabled"]}) for item in plan.config_json["rules"])
            for plan in plans
        }
        codes = {plan.scenario_code for plan in plans}
        for code, scenario_name in db.execute(
            select(ScenarioTemplate.scenario_code, ScenarioTemplate.scenario_name).where(ScenarioTemplate.scenario_code.in_(codes))
        ).all():
            scenario_names[code] = scenario_name

    items = []
    for plan in plans:
        scope = plan.scope_json if isinstance(plan.scope_json, dict) else {}
        context = plan.context_json if isinstance(plan.context_json, dict) else {}
        _, calculation_enabled = plan_calculation_config(context)
        base_version = db.get(ScenarioVersion, plan.scenario_version_id) if plan.scenario_version_id else None
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
                "effective": plan.sync_error is None and plan.scope_json is not None,
                "sync_error": plan.sync_error,
                "enabled_rule_count": len(scope.get("enabled_rule_keys", [])),
                "explicit_rule_count": rule_counts.get(plan.id, 0) + sum(calculation_enabled.values()),
                "updated_at": plan.updated_at,
            }
        )
    return page_result(items, total, page, page_size)


def list_plan_options(db: Session, *, scenario_code: str | None) -> list[dict[str, Any]]:
    """下拉选项：启用且未删除、且范围已生效的方案。"""
    conditions = [
        AuditPlan.deleted_at.is_(None),
        AuditPlan.enabled.is_(True),
        _effective_predicate(),
    ]
    if scenario_code:
        conditions.append(AuditPlan.scenario_code == scenario_code)
    # 下拉只需要四个轻量字段。直接 select(AuditPlan) 会把多个大 JSON 列
    # 带入 ORDER BY plan_name，在 MySQL 小 sort buffer 下触发 1038。
    rows = db.execute(
        select(
            AuditPlan.plan_code,
            AuditPlan.plan_name,
            AuditPlan.scenario_code,
            ScenarioVersion.version_no,
        )
        .outerjoin(ScenarioVersion, ScenarioVersion.id == AuditPlan.scenario_version_id)
        .where(*conditions)
        .order_by(AuditPlan.plan_name, AuditPlan.id)
    ).all()
    return [
        {
            "plan_code": plan_code,
            "plan_name": plan_name,
            "scenario_code": scenario_code,
            "scenario_version_no": version_no,
        }
        for plan_code, plan_name, scenario_code, version_no in rows
    ]


def plan_selection_view(db: Session, plan: AuditPlan) -> dict[str, Any]:
    """方案视图。

    规则的「选没选」就是方案 JSON 中的 enabled。规则、材料、字段都从方案 config_json
    读取，不再从场景版本或配置明细表读，也不限制系统派生规则的选择。
    """
    rules = sorted(config_store.rows(db, PlanRule, plan.id), key=lambda item: (item.sort_no, item.rule_key,))
    materials = sorted(config_store.rows(db, PlanMaterial, plan.id), key=lambda item: (item.sort_no, item.id,))
    field_count = len(plan.config_json["fields"])
    context = plan.context_json if isinstance(plan.context_json, dict) else {}
    calculation_candidates, calculation_enabled = plan_calculation_config(context)
    calculation_source = calculation_candidates
    # 场景版本只是溯源：这份方案从哪一版复制而来，执行与范围都不读它。
    version = db.get(ScenarioVersion, plan.scenario_version_id) if plan.scenario_version_id else None
    return {
        "plan_code": plan.plan_code,
        "plan_name": plan.plan_name,
        "scenario_code": plan.scenario_code,
        "description": plan.description,
        "enabled": plan.enabled,
        "deleted": plan.deleted_at is not None,
        # 范围是否已生效：sync_error 非空表示最近一次保存没有算出可用范围，
        # 页面据此显示「待核对 + 原因」，不把不完整配置说成已生效。
        "effective": plan.sync_error is None and plan.scope_json is not None,
        "sync_error": plan.sync_error,
        "scope_hash": plan.scope_hash,
        "base_scenario_version": (
            {
                "version_no": version.version_no,
                "status": version.status,
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
