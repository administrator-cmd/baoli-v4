"""方案相对场景发布版本的定制差异展示。"""

from typing import Any, Callable
from sqlalchemy.orm import Session
from ...domain.calculation_config import calculation_store
from ...domain import manifest as manifest_module
from ...domain.rule_library import active
from ...common.exceptions import BusinessError
from ...entity import AuditPlan, ScenarioVersion
from .views import plan_config_view


_ADJUSTMENT_FIELDS: dict[str, tuple[tuple[str, str], ...]] = {
    "材料位": (("name", "名称"), ("required", "是否必传"), ("description", "业务说明"), ("source_ref", "来源编号")),
    "字段": (
        ("name", "名称"), ("material_key", "所属材料"), ("value_type", "数据类型"),
        ("extraction_strategy", "抽取策略"), ("structure_schema", "结构 Schema"),
        ("unit", "单位"), ("currency", "币种"), ("artifact_source", "产物来源"),
        ("period_scope", "期间口径"), ("statement_scope", "报表口径"),
        ("required", "是否必抽"), ("instruction", "提取要求"), ("source_ref", "来源编号"),
    ),
    "规则": (
        ("name", "名称"), ("node_code", "所属节点"), ("operator_code", "算子"),
        ("enabled", "是否启用"), ("source_ref", "来源编号"),
    ),
    "报告章节": (
        ("title", "名称"), ("content_instruction", "内容要求"), ("data_refs", "业务数据引用"),
        ("report_code", "报告编号"), ("report_name", "报告名称"),
        ("material_keys", "材料范围"), ("formats", "输出格式"),
        ("section_order", "章节顺序"), ("content_type", "内容类型"),
        ("risk_level_order", "等级顺序"), ("result_sort", "结果排序"), ("config_status", "配置状态"),
    ),
    "计算规则": (
        ("rule_name", "名称"), ("formula", "计算方法"), ("requirement", "业务要求"),
        ("result_type", "结果类型"), ("unit", "单位"), ("currency", "币种"),
        ("parameters", "计算参数"), ("depends_on_result_codes", "依赖计算结果"),
        ("period_policy", "期间要求"), ("statement_scope", "报表口径"),
        ("business_context", "业务补充"), ("remark", "备注"),
        ("status", "配置状态"), ("enabled", "是否启用"), ("source_id", "业务编号"),
    ),
}


_RULE_DEFINITION_LABELS = {
    "inputs": "输入字段",
    "materials": "适用材料",
    "parameters": "判定参数",
    "requirement": "审核要求",
    "applicability": "适用条件",
    "source_rule_key": "来源规则",
    "source_rule_keys": "来源规则集合",
    "source_rule_codes": "来源结果编号",
    "description": "业务说明",
    "check_level": "检查等级",
    "check_operator": "检查方式",
    "risk_level_value": "风险等级",
    "risk_condition": "风险成立条件",
    "rule_role": "规则角色",
    "config_status": "配置状态",
}


def _adjustment_index(
    rows: list[dict[str, Any]], identity: Callable[[dict[str, Any]], Any], category: str
) -> dict[str, dict[str, Any]]:
    indexed: dict[str, dict[str, Any]] = {}
    for row in rows:
        code = str(identity(row) or "")
        if not code or code in indexed:
            raise BusinessError("PLAN_ADJUSTMENT_ID_INVALID", f"{category}缺少唯一身份，无法列出旧方案调整", details={"code": code})
        indexed[code] = row
    return indexed


def _rule_adjustment_identity(row: dict[str, Any]) -> str:
    return str((row.get("rule_json") or {}).get("rule_code") or row.get("rule_key") or "")


def _adjustment_items(
    category: str,
    baseline_rows: list[dict[str, Any]],
    current_rows: list[dict[str, Any]],
    identity: Callable[[dict[str, Any]], Any],
    name_key: str,
) -> list[dict[str, Any]]:
    baseline = _adjustment_index(baseline_rows, identity, category)
    current = _adjustment_index(current_rows, identity, category)
    items: list[dict[str, Any]] = []
    for code in dict.fromkeys([*baseline, *current]):
        before = baseline.get(code)
        after = current.get(code)
        if before is None or after is None:
            row = after if after is not None else before
            items.append({
                "category": category,
                "code": code,
                "name": row.get(name_key) or code,
                "change": "ADDED" if before is None else "REMOVED",
                "fields": [],
            })
            continue
        system_rule = category == "规则" and bool(before.get("is_system") or after.get("is_system"))
        compared_fields = (("enabled", "是否启用"),) if system_rule else _ADJUSTMENT_FIELDS[category]
        fields = [
            {"label": label, "before": before.get(key), "after": after.get(key)}
            for key, label in compared_fields
            if before.get(key) != after.get(key)
        ]
        if category == "规则" and not system_rule:
            old_definition = dict(before.get("rule_json") or {})
            new_definition = dict(after.get("rule_json") or {})
            for duplicate in ("name", "node", "operator", "enabled"):
                old_definition.pop(duplicate, None)
                new_definition.pop(duplicate, None)
            for key in dict.fromkeys([*old_definition, *new_definition]):
                if old_definition.get(key) != new_definition.get(key):
                    fields.append({
                        "label": _RULE_DEFINITION_LABELS.get(key, f"规则定义 · {key}"),
                        "before": old_definition.get(key),
                        "after": new_definition.get(key),
                    })
        if fields:
            items.append({"category": category, "code": code, "name": after.get(name_key) or code, "change": "MODIFIED", "fields": fields})
    return items


def plan_adjustments_view(db: Session, plan: AuditPlan) -> dict[str, Any]:
    """列出旧方案相对其来源场景版本的调整，仅供重新生成时参考。"""
    version = db.get(ScenarioVersion, plan.scenario_version_id) if plan.scenario_version_id else None
    if version is None:
        raise BusinessError("PLAN_SCENARIO_VERSION_MISSING", "旧方案的来源场景版本不存在，无法比较调整", status_code=409)
    baseline = manifest_module.editable_config_of(version.manifest_json, namespace=plan.plan_code)
    current = plan_config_view(db, plan)
    groups = (
        ("材料位", baseline["materials"], current["materials"], lambda row: row.get("material_key"), "name"),
        ("字段", baseline["fields"], current["fields"], lambda row: row.get("field_key"), "name"),
        ("规则", baseline["rules"], current["rules"], _rule_adjustment_identity, "name"),
        ("报告章节", baseline["sections"], current["sections"], lambda row: row.get("section_key"), "title"),
    )
    items = [
        item
        for category, original, edited, identity, name_key in groups
        for item in _adjustment_items(category, original, edited, identity, name_key)
    ]
    candidates = calculation_store(baseline["calculation_json"])
    original_calculations = candidates
    items.extend(_adjustment_items(
        "计算规则",
        [
            {"rule_code": str(row["rule_code"]), "rule_name": row.get("rule_name") or row["rule_code"], "enabled": active(row)}
            for row in original_calculations
        ],
        current["calculation_rules"],
        lambda row: row.get("rule_code"),
        "rule_name",
    ))
    return {
        "plan_code": plan.plan_code,
        "plan_name": plan.plan_name,
        "scenario_code": plan.scenario_code,
        "base_scenario_version_no": version.version_no,
        "items": items,
    }
