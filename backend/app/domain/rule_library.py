"""规则库编译：保留业务原文，仅组织身份、实际引用与执行产出。

RULE_EXECUTION 是现有数据库执行记录的关联容器，没有业务阶段或默认依赖。
执行顺序只取自输入字段、上游结果与规则显式引用，不使用工作表或旧节点类型。
"""
from copy import deepcopy
import json
from typing import Any

from ..common.exceptions import BusinessError
from . import scenario_schema
from .identity import stable_key
from .node_profile import NODE_PROFILE_VERSION
from .parameter_contract import CONFIG_STATUS_DISABLED, CONFIG_STATUS_PENDING
from .report_data import report_data_refs

EXECUTION_CODE = "RULE_EXECUTION"
EXECUTION_VERSION = NODE_PROFILE_VERSION


def execution_container() -> dict[str, Any]:
    return {"node_code": EXECUTION_CODE, "node_type": EXECUTION_CODE, "name": "执行生效规则",
            "sequence_no": 1, "enabled": True, "depends_on": [], "config_json": {},
            "execution_contract": {"field_keys": [], "rule_keys": [], "calculation_rule_codes": []}}


def execution_container_of(manifest: dict[str, Any]) -> dict[str, Any]:
    if manifest.get("execution_contract_version") != EXECUTION_VERSION:
        raise BusinessError("RULE_LIBRARY_EXECUTION_REQUIRED", "该配置使用已移除的九阶段执行器，请以当前规则库重新发布；原任务快照不会被改写", status_code=409)
    return next(item for item in manifest["nodes"] if item["node_code"] == EXECUTION_CODE)


def active(item: dict[str, Any]) -> bool:
    """启用开关与确认状态共同决定生效；待确认和暂停都不执行。"""
    return bool(item.get("enabled", True)) and item.get("config_status", item.get("status")) not in {
        CONFIG_STATUS_PENDING, CONFIG_STATUS_DISABLED,
    }


def is_risk_rule(rule: dict[str, Any]) -> bool:
    """仅认明确的风险角色或风险动作，不按条件文本、名称或业务编号猜测。"""
    return rule.get("rule_role") == "RISK" or rule.get("operator") == "RISK_TRIGGER"


def compile_library(**config: Any) -> dict[str, Any]:
    materials, fields = deepcopy(config["materials"]), deepcopy(config["fields"])
    rules = deepcopy(config["rules"])
    sections = deepcopy(config["sections"])
    calculations = deepcopy(config.get("calculation_candidates") or [])
    namespace = config["namespace"]
    for collection, identity in ((materials, "material_key"), (fields, "field_key"), (rules, "rule_key"), (sections, "section_key")):
        identities = [item.get(identity) for item in collection]
        if any(not key for key in identities) or len(identities) != len(set(identities)):
            raise BusinessError("RULE_LIBRARY_IDENTITY_INVALID", "规则库对象缺少唯一身份", details={"identity": identity})
    field_by_key = {item["field_key"]: item for item in fields}
    field_by_code = {str(item.get("source_ref") or item["field_key"]): item for item in fields}
    for field in fields:
        field["node_code"] = EXECUTION_CODE
        field["enabled"] = active(field)
        field.setdefault("period_scope", "NOT_APPLICABLE")
        field.setdefault("statement_scope", "NOT_APPLICABLE")
    for rule in rules:
        rule["node"] = rule["node_type"] = EXECUTION_CODE
        rule["enabled"] = active(rule)
        rule["rule_code"] = rule.get("rule_code") or rule.get("source_ref") or rule["rule_key"]
        rule.setdefault("operator", "LLM_EXECUTE")
        rule.setdefault("inputs", [])
        rule.setdefault("outputs", [])
        rule.setdefault("materials", [])
        rule.setdefault("parameters", {})
        rule.setdefault("requirement", "")
        rule.setdefault("applicability", {"kind": "ALWAYS", "material_keys": []})
    # 已有明确产出规则时尊重其启停，不能因用户停用它而派生另一条替代规则。
    explicit_outputs = {key for item in rules for key in item["outputs"]}
    switches = config.get("system_rule_enabled") or {}
    for field in fields:
        if field["field_key"] in explicit_outputs:
            continue
        code = f"SYS-EXTRACT-{field['field_key']}"
        rules.append({"rule_key": stable_key("rule", namespace, f"extract:{field['field_key']}"),
                      "rule_code": code, "name": f"抽取{field['name']}", "kind": "EXTRACTION", "generated_from": "FIELD",
                      "node": EXECUTION_CODE, "node_type": EXECUTION_CODE, "operator": "LLM_EXTRACT",
                      "enabled": active(field) and switches.get(code, True),
                      "config_status": field.get("config_status"),
                      "source_ref": field.get("source_ref"),
                      "inputs": [], "outputs": [field["field_key"]], "materials": [field["material_key"]],
                      "parameters": {}, "requirement": field.get("instruction") or "",
                      "field_definition": deepcopy(field), "origin": field.get("origin"),
                      "applicability": {"kind": "ALWAYS", "material_keys": []}})
    for calculation in calculations:
        calculation["node_code"] = calculation["node_instance_code"] = EXECUTION_CODE
        calculation["node_type"] = EXECUTION_CODE
        inputs = []
        for parameter in calculation.get("parameters") or []:
            if parameter.get("source_type") == "FIELD":
                field = field_by_key.get(str(parameter.get("source_code"))) or field_by_code.get(str(parameter.get("source_code")))
                if field is not None:
                    inputs.append({"field_key": field["field_key"], "material_key": field["material_key"]})
        calculation["depends_on_field_codes"] = [p["source_code"] for p in calculation.get("parameters") or [] if p.get("source_type") == "FIELD"]
        key = stable_key("rule", namespace, f"calculation:{calculation.get('candidate_ref') or calculation['rule_code']}")
        # 方案往返时计算执行项已经存在，不重新产生同一业务项。
        existing = next((item for item in rules if item.get("calculation_ref") == calculation["rule_code"]), None)
        if existing is not None:
            existing["enabled"] = active(existing) and active(calculation)
            existing["calculation_definition"] = deepcopy(calculation)
            continue
        rules.append({"rule_key": key, "rule_code": calculation["rule_code"],
                      "name": calculation.get("rule_name") or calculation["rule_code"], "kind": "CALCULATION",
                      "node": EXECUTION_CODE, "node_type": EXECUTION_CODE, "operator": "LLM_CALCULATE",
                      "enabled": active(calculation), "inputs": inputs, "outputs": [],
                      "config_status": calculation.get("status"),
                      "source_ref": calculation.get("source_id"),
                      "result_code": calculation["result_code"], "calculation_ref": calculation["rule_code"],
                      "materials": list(dict.fromkeys(i["material_key"] for i in inputs)),
                      "parameters": {}, "requirement": "\n".join(text for text in (calculation.get("formula"), calculation.get("requirement")) if text),
                      "calculation_definition": deepcopy(calculation), "origin": calculation.get("origin"),
                      "applicability": {"kind": "ALWAYS", "material_keys": []}})
    groups: dict[str, dict[str, Any]] = {}
    for section in sections:
        section["data_refs"] = report_data_refs(section)
        section["field_keys"] = [ref for ref in section["data_refs"] if ref in field_by_key]
        code = str(section.get("report_code") or section["section_key"])
        group = groups.setdefault(code, {"report_code": code, "report_name": section.get("report_name") or section.get("section_name") or section.get("title"),
                                         "sections": [], "formats": [], "material_keys": []})
        group["sections"].append(section)
        if active(section):
            group["formats"] = list(dict.fromkeys([*group["formats"], *(section.get("formats") or [])]))
            group["material_keys"] = list(dict.fromkeys([*group["material_keys"], *(section.get("material_keys") or [])]))
    rules = [item for item in rules if not item.get("report_ref") or item["report_ref"] in groups]
    for code, group in groups.items():
        existing = next((item for item in rules if item.get("report_ref") == code), None)
        key = existing["rule_key"] if existing is not None else stable_key("rule", namespace, f"report:{code}")
        active_sections = [item for item in group["sections"] if active(item)]
        output_fields = list(dict.fromkeys(key for s in active_sections for key in s.get("field_keys") or []))
        definition = {"rule_key": key, "rule_code": f"SYS-REPORT-{key}", "name": group["report_name"], "kind": "REPORT", "generated_from": "REPORT",
                      "source_ref": group["sections"][0].get("report_code"),
                      "origin": deepcopy(group["sections"][0].get("origin")),
                      "node": EXECUTION_CODE, "node_type": EXECUTION_CODE, "operator": "REPORT_RENDER",
                      "enabled": bool(active_sections) and switches.get(f"report:{code}", True) and (active(existing) if existing is not None else True),
                      "inputs": [{"field_key": k, "material_key": field_by_key[k]["material_key"]} for k in output_fields if k in field_by_key],
                      "outputs": [], "materials": group["material_keys"], "parameters": {},
                      "report_ref": code, "report_definition": {**deepcopy(group), "sections": deepcopy(active_sections)}, "requirement": "\n".join(s.get("content_instruction") or "" for s in active_sections),
                      "applicability": {"kind": "ALWAYS", "material_keys": []}}
        if existing is None:
            rules.append(definition)
        else:
            existing.update(definition)
            existing.pop("dependency_error", None)
            existing.pop("unavailable_dependencies", None)
    # 不检查暂停或待确认项的内容。实际执行项的引用错误在该项执行时明确记录。
    for rule in rules:
        rule.setdefault("kind", "EXTRACTION" if rule["operator"] == "LLM_EXTRACT" else "RULE")
        rule["output_definitions"] = [deepcopy(field_by_key[key]) for key in rule["outputs"] if key in field_by_key]
        if not rule["enabled"]:
            continue
        material_keys = {m["material_key"] for m in materials}
        missing = [f"field:{i['field_key']}" for i in rule["inputs"] if i["field_key"] not in field_by_key]
        missing += [f"material:{key}" for key in rule["materials"] if key not in material_keys]
        missing += [f"output:{key}" for key in rule["outputs"] if key not in field_by_key]
        missing += [f"material:{i['material_key']}" for i in rule["inputs"] if i["material_key"] not in material_keys]
        missing += [f"input_material:{i['field_key']}@{i['material_key']}" for i in rule["inputs"]
                    if i["field_key"] in field_by_key and field_by_key[i["field_key"]]["material_key"] != i["material_key"]]
        if missing:
            rule["dependency_error"] = missing
    container = execution_container()
    container["execution_contract"].update(field_keys=list(field_by_key), rule_keys=[i["rule_key"] for i in rules], calculation_rule_codes=[c["rule_code"] for c in calculations])
    manifest = {"manifest_version": "2.0", "execution_contract_version": EXECUTION_VERSION,
                "profile_version": EXECUTION_VERSION, "source_kind": config["source_kind"],
                "scene_code": config["scene_code"], "scene_name": config["scene_name"],
                "scenario_schema": {**scenario_schema.normalize_schema(config.get("scenario_schema_config")), "profile_version": EXECUTION_VERSION},
                "materials": materials, "fields": fields, "rules": rules, "nodes": [container],
                "calculations": calculations, "calculation_inputs": calculations,
                "report_template": sections, "report_groups": {"reports": list(groups.values()), "source_kind": "RULE_LIBRARY"},
                "compiled_at": config["compiled_at"].isoformat(),
                "source_workbook_id": None}
    if config.get("import_proposals") is not None:
        manifest["import_proposals"] = deepcopy(config["import_proposals"])
    return manifest


def execution_order(rules: list[dict[str, Any]], *, all_rules: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """只按显式输入/结果引用排序；无法满足的引用与环在执行项上暴露。"""
    rules = deepcopy(rules)
    producers = {field: r["rule_key"] for r in rules for field in r.get("outputs") or []}
    result_producers = {r["result_code"]: r["rule_key"] for r in rules if r.get("result_code")}
    code_producers = {r["rule_code"]: r["rule_key"] for r in rules if r.get("rule_code")}
    selected = {r["rule_key"] for r in rules}
    known_by_key = {r["rule_key"]: r for r in all_rules}
    known_by_code = {str(code): r for r in all_rules for code in (r.get("result_code"), r.get("rule_code")) if code}
    dependencies = {}
    for rule in rules:
        deps = set(rule.get("source_rule_keys") or [])
        if rule.get("source_rule_key"):
            deps.add(rule["source_rule_key"])
        deps.update(producers[i["field_key"]] for i in rule.get("inputs") or [] if i["field_key"] in producers)
        codes = [*(rule.get("source_rule_codes") or []), *(rule.get("calculation_definition", {}).get("depends_on_result_codes") or []),
                 *(p["source_code"] for p in rule.get("calculation_definition", {}).get("parameters") or [] if p.get("source_type") == "CALC_RESULT")]
        missing_codes = []
        for code in codes:
            producer = result_producers.get(code) or code_producers.get(code)
            if producer is not None:
                deps.add(producer)
            else:
                missing_codes.append(str(code))
        # 报告消费任务结果集合，等该任务其他生效项执行完再生成。
        if rule.get("kind") == "REPORT":
            deps.update(r["rule_key"] for r in rules if r.get("kind") != "REPORT")
        if rule.get("kind") == "EXTRACTION":
            deps.discard(rule["rule_key"])
        if deps - selected or missing_codes:
            rule["unavailable_dependencies"] = sorted(deps - selected) + missing_codes
            rule["dependency_details"] = []
            for reference in rule["unavailable_dependencies"]:
                definition = known_by_key.get(reference) or known_by_code.get(reference)
                rule["dependency_details"].append({
                    "reference": reference,
                    "name": definition.get("name") if definition is not None else None,
                    "status": (
                        "MISSING" if definition is None else
                        "PENDING" if definition.get("config_status", definition.get("status")) == CONFIG_STATUS_PENDING else
                        "DISABLED" if not active(definition) else "NOT_SELECTED"
                    ),
                })
        dependencies[rule["rule_key"]] = deps & selected
    ordered, remaining, done = [], list(rules), set()
    while remaining:
        ready = [r for r in remaining if dependencies[r["rule_key"]] <= done]
        if not ready:
            # 报告消费执行集合，存在依赖环也必须等其余项形成结果或失败原因再生成。
            for rule in sorted(remaining, key=lambda item: item.get("kind") == "REPORT"):
                rule["unavailable_dependencies"] = list(dict.fromkeys([
                    *(rule.get("unavailable_dependencies") or []),
                    *sorted(dependencies[rule["rule_key"]] - done),
                ]))
                if rule.get("kind") != "REPORT":
                    rule["dependency_details"] = [*(rule.get("dependency_details") or []), {
                        "status": "CYCLE_OR_WAITING_ON_CYCLE", "references": sorted(dependencies[rule["rule_key"]] - done),
                    }]
                ordered.append(rule)
            break
        for rule in ready:
            ordered.append(rule)
            done.add(rule["rule_key"])
            remaining.remove(rule)
    return ordered
