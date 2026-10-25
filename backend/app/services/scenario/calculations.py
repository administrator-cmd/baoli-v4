"""场景草稿计算定义维护与结构化引用同步。"""

from copy import deepcopy
from typing import Any

from sqlalchemy.orm import Session

from ...common.exceptions import BusinessError
from ...domain.calculation_config import calculation_store
from ...domain.identity import stable_key
from ...domain.report_data import report_data_refs
from ...entity.scenario_drafts import ScenarioDraft, ScenarioDraftRule, ScenarioDraftSection
from ..shared import configuration as config_store
from .draft import _locked_draft, _save_draft


def _find(items: list[dict[str, Any]], rule_code: str) -> dict[str, Any]:
    matches = [item for item in items if item.get("rule_code") == rule_code]
    if not matches:
        raise BusinessError("DRAFT_CALCULATION_NOT_FOUND", "计算规则不存在", status_code=404)
    if len(matches) != 1:
        raise BusinessError("CALCULATION_CODE_DUPLICATED", "计算规则编号重复，无法确定操作对象", status_code=409)
    return matches[0]


def _result_refs(definition: dict[str, Any]) -> set[str]:
    refs = set(definition.get("depends_on_result_codes") or [])
    refs.update(definition.get("source_rule_codes") or [])
    parameters = definition.get("parameters") or []
    if isinstance(parameters, list):
        refs.update(item["source_code"] for item in parameters
                    if item.get("source_type") == "CALC_RESULT" and item.get("source_code"))
    return refs


def _remap_results(definition: dict[str, Any], previous: str, current: str) -> dict[str, Any]:
    updated = deepcopy(definition)
    for key in ("depends_on_result_codes", "source_rule_codes"):
        if key in updated:
            updated[key] = [current if code == previous else code for code in updated[key] or []]
    parameters = updated.get("parameters") or []
    if isinstance(parameters, list):
        for parameter in parameters:
            if parameter.get("source_type") == "CALC_RESULT" and parameter.get("source_code") == previous:
                parameter["source_code"] = current
    return updated


def save_draft_calculation(
    db: Session, scenario_code: str, payload: dict[str, Any], *, rule_code: str | None = None,
) -> ScenarioDraft:
    template, draft = _locked_draft(db, scenario_code)
    items = deepcopy(calculation_store(draft.calculation_json))
    previous = _find(items, rule_code) if rule_code is not None else None
    definition = {**(previous or {}), **deepcopy(payload)}
    for key in ("rule_code", "rule_name", "result_code"):
        definition[key] = definition[key].strip()
        if not definition[key]:
            raise BusinessError("CALCULATION_IDENTITY_REQUIRED", "计算规则编号、名称和结果标识不能为空", status_code=400)
    if previous is not None and definition["rule_code"] != rule_code:
        raise BusinessError("CALCULATION_CODE_CHANGED", "编辑计算规则时须保留原规则编号", status_code=400)
    others = [item for item in items if item is not previous]
    if any(item.get("rule_code") == definition["rule_code"] for item in others):
        raise BusinessError("CALCULATION_CODE_DUPLICATED", "计算规则编号已存在", status_code=409)
    if any(item.get("result_code") == definition["result_code"] for item in others):
        raise BusinessError("CALCULATION_RESULT_DUPLICATED", "计算结果标识已存在", status_code=409)
    definition["enabled"] = definition["status"] == "CLEAR"
    definition["depends_on_field_codes"] = [
        parameter["source_code"] for parameter in definition["parameters"]
        if parameter.get("source_type") == "FIELD"
    ]
    if "config_status" in definition:
        definition["config_status"] = definition["status"]
    if previous is None:
        items.append(definition)
    else:
        items[items.index(previous)] = definition
        old_result = previous["result_code"]
        new_result = definition["result_code"]
        if old_result != new_result:
            items = [_remap_results(item, old_result, new_result) for item in items]
            for rule in config_store.rows(db, ScenarioDraftRule, draft.id):
                rule.rule_json = _remap_results(rule.rule_json, old_result, new_result)
            for section in config_store.rows(db, ScenarioDraftSection, draft.id):
                section.data_refs = [new_result if ref == old_result else ref for ref in section.data_refs or []]
    draft.calculation_json = items
    return _save_draft(
        db, template, draft,
        operation_type="DRAFT_CALCULATION_CREATE" if previous is None else "DRAFT_CALCULATION_UPDATE",
        object_name=definition["rule_name"],
        detail={"scenario_code": scenario_code, "rule_code": definition["rule_code"]},
    )


def delete_draft_calculation(db: Session, scenario_code: str, rule_code: str) -> ScenarioDraft:
    template, draft = _locked_draft(db, scenario_code)
    items = deepcopy(calculation_store(draft.calculation_json))
    item = _find(items, rule_code)
    rule_key = stable_key("rule", scenario_code, f"calculation:{item.get('candidate_ref') or rule_code}")
    references = {rule_code, item["result_code"], rule_key}
    calculations = [other.get("rule_name") or other["rule_code"] for other in items
                    if other is not item and _result_refs(other) & references]
    rules = []
    for rule in config_store.rows(db, ScenarioDraftRule, draft.id):
        definition = rule.rule_json
        keys = set(definition.get("source_rule_keys") or [])
        if definition.get("source_rule_key"):
            keys.add(definition["source_rule_key"])
        if (_result_refs(definition) | keys) & references:
            rules.append(rule.name)
    sections = [section.title for section in config_store.rows(db, ScenarioDraftSection, draft.id)
                if set(report_data_refs({"data_refs": section.data_refs, "field_keys": section.field_keys})) & references]
    if calculations or rules or sections:
        names = "、".join([*calculations, *rules, *sections])
        raise BusinessError(
            "CALCULATION_REFERENCED", f"该计算规则仍被以下配置引用：{names}。请先调整引用后再删除。",
            status_code=409, details={"calculations": calculations, "rules": rules, "sections": sections},
        )
    draft.calculation_json = [other for other in items if other is not item]
    return _save_draft(
        db, template, draft, operation_type="DRAFT_CALCULATION_DELETE", object_name=item.get("rule_name") or rule_code,
        detail={"scenario_code": scenario_code, "rule_code": rule_code},
    )
