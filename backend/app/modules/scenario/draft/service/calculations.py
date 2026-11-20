"""场景草稿计算定义维护与结构化引用同步。"""

from copy import deepcopy
from re import fullmatch
from typing import Any

from sqlalchemy.orm import Session

from app.common.exceptions import BusinessError
from app.domain.configuration.calculation_config import calculation_store
from app.domain.shared.identity import stable_key
from app.domain.configuration.report_data import report_data_refs
from app.modules.scenario.draft.model import ScenarioDraft, ScenarioDraftRule, ScenarioDraftSection
from app.modules.scenario.draft.service.draft import ScenarioDraftService
from app.services.shared import configuration as config_store


class ScenarioDraftCalculationsService:
    """本实体的服务方法；db 由构造时注入。"""

    def __init__(self, db: Session | None = None) -> None:
        self.db = db

    def _find(self, items: list[dict[str, Any]], rule_code: str) -> dict[str, Any]:
        matches = [item for item in items if item.get("rule_code") == rule_code]
        if not matches:
            raise BusinessError("DRAFT_CALCULATION_NOT_FOUND", "计算规则不存在", status_code=404)
        if len(matches) != 1:
            raise BusinessError("CALCULATION_CODE_DUPLICATED", "计算规则编号重复，无法确定操作对象", status_code=409)
        return matches[0]


    def _result_refs(self, definition: dict[str, Any]) -> set[str]:
        refs = set(definition.get("depends_on_result_codes") or [])
        refs.update(definition.get("source_rule_codes") or [])
        parameters = definition.get("parameters") or []
        if isinstance(parameters, list):
            refs.update(item["source_code"] for item in parameters
                        if item.get("source_type") == "CALC_RESULT" and item.get("source_code"))
        return refs


    def _remap_results(self, definition: dict[str, Any], previous: str, current: str) -> dict[str, Any]:
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


    def save_draft_calculation(self, scenario_code: str, payload: dict[str, Any], *, rule_code: str | None = None,
    ) -> ScenarioDraft:
        template, draft = ScenarioDraftService(self.db)._locked_draft( scenario_code)
        items = deepcopy(calculation_store(draft.calculation_json))
        previous = self._find(items, rule_code) if rule_code is not None else None
        definition = {**(previous or {}), **deepcopy(payload)}
        if previous is None and not definition["rule_code"].strip():
            next_number = max(
                (int(match[1]) for item in items
                 if (match := fullmatch(r"CALC_([0-9]+)", item.get("rule_code") or ""))),
                default=0,
            ) + 1
            definition["rule_code"] = f"CALC_{next_number:04d}"
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
                items = [self._remap_results(item, old_result, new_result) for item in items]
                for rule in config_store.rows(self.db, ScenarioDraftRule, draft.id):
                    config_store.update_rule(rule, rule_json=self._remap_results(rule.rule_json, old_result, new_result))
                for section in config_store.rows(self.db, ScenarioDraftSection, draft.id):
                    config_store.update_section(section, data_refs=[new_result if ref == old_result else ref for ref in section.data_refs or []])
        draft.calculation_json = items
        return ScenarioDraftService(self.db)._save_draft(
             template, draft,
            operation_type="DRAFT_CALCULATION_CREATE" if previous is None else "DRAFT_CALCULATION_UPDATE",
            object_name=definition["rule_name"],
            detail={"scenario_code": scenario_code, "rule_code": definition["rule_code"]},
        )


    def delete_draft_calculation(self, scenario_code: str, rule_code: str) -> ScenarioDraft:
        template, draft = ScenarioDraftService(self.db)._locked_draft( scenario_code)
        items = deepcopy(calculation_store(draft.calculation_json))
        item = self._find(items, rule_code)
        rule_key = stable_key("rule", scenario_code, f"calculation:{item.get('candidate_ref') or rule_code}")
        references = {rule_code, item["result_code"], rule_key}
        calculations = [other.get("rule_name") or other["rule_code"] for other in items
                        if other is not item and self._result_refs(other) & references]
        rules = []
        for rule in config_store.rows(self.db, ScenarioDraftRule, draft.id):
            definition = rule.rule_json
            keys = set(definition.get("source_rule_keys") or [])
            if definition.get("source_rule_key"):
                keys.add(definition["source_rule_key"])
            if (self._result_refs(definition) | keys) & references:
                rules.append(rule.name)
        sections = [section.title for section in config_store.rows(self.db, ScenarioDraftSection, draft.id)
                    if set(report_data_refs({"data_refs": section.data_refs, "field_keys": section.field_keys})) & references]
        if calculations or rules or sections:
            names = "、".join([*calculations, *rules, *sections])
            raise BusinessError(
                "CALCULATION_REFERENCED", f"该计算规则仍被以下配置引用：{names}。请先调整引用后再删除。",
                status_code=409, details={"calculations": calculations, "rules": rules, "sections": sections},
            )
        draft.calculation_json = [other for other in items if other is not item]
        return ScenarioDraftService(self.db)._save_draft(
             template, draft, operation_type="DRAFT_CALCULATION_DELETE", object_name=item.get("rule_name") or rule_code,
            detail={"scenario_code": scenario_code, "rule_code": rule_code},
        )
