"""规则库编译：保留业务原文，仅组织身份、实际引用与执行产出。

RULE_EXECUTION 是现有数据库执行记录的关联容器，没有业务阶段或默认依赖。
执行顺序只取自输入字段、上游结果与规则显式引用，不使用工作表或旧节点类型。
"""
from copy import deepcopy
from typing import Any

from ..common.exceptions import BusinessError
from ..constants.execution import NODE_RULE_EXECUTION, OPERATOR_LLM_EXECUTE
from ..constants.status import CONFIG_STATUS_DISABLED, CONFIG_STATUS_PENDING
from . import fact_projection, scenario_schema
from .execution_mode import (
    EXECUTION_MODE_ORDER,
    EXECUTION_MODES,
    MODE_AGGREGATE,
    MODE_CALCULATE,
    MODE_CROSS_SCOPE_COMPARE,
    MODE_DERIVE_RISK,
    MODE_EXTRACT,
    MODE_REPORT,
    MODE_UNKNOWN,
    MODE_VALUE_COMPARE,
)
from .identity import stable_key
from .node_profile import NODE_PROFILE_VERSION
from .report_data import report_data_refs

EXECUTION_CODE = NODE_RULE_EXECUTION
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


def _validate_execution_modes(rules: list[dict[str, Any]]) -> None:
    """执行方式是执行器选择上下文的唯一依据；缺失时拒绝发布，不默认成 CHECK。

    停用项不执行，内容不参与校验：这与 `_validate_calculation_codes` 只判启用项一致。
    """
    missing: list[dict[str, str]] = []
    problems: list[str] = []
    for rule in rules:
        if not rule.get("enabled"):
            continue
        name = str(rule.get("name") or rule.get("rule_key") or "")
        mode = str(rule.get("execution_mode") or MODE_UNKNOWN)
        if mode == MODE_UNKNOWN or mode not in EXECUTION_MODES:
            missing.append({"rule_key": str(rule.get("rule_key") or ""), "name": name})
            continue
        if rule.get("kind") != "RULE":
            continue
        if mode in {MODE_AGGREGATE, MODE_DERIVE_RISK} and not (
            rule.get("source_rule_keys") or rule.get("source_rule_codes")
        ):
            problems.append(f"{name}（{mode}）没有来源规则引用")
        if mode == MODE_DERIVE_RISK and not (rule.get("risk_condition") or rule.get("requirement")):
            problems.append(f"{name}（{mode}）没有风险成立条件")
        if mode in {MODE_VALUE_COMPARE, MODE_CROSS_SCOPE_COMPARE} and not (rule.get("inputs") or rule.get("materials")):
            problems.append(f"{name}（{mode}）没有输入字段或材料范围")
        if mode == MODE_EXTRACT and not rule.get("outputs"):
            problems.append(f"{name}（{mode}）没有输出字段")
        if mode == MODE_REPORT and not rule.get("report_definition"):
            problems.append(f"{name}（{mode}）没有报告定义")
    if missing:
        names = "、".join(item["name"] for item in missing[:8])
        more = f" 等 {len(missing)} 条" if len(missing) > 8 else ""
        raise BusinessError(
            "RULE_EXECUTION_MODE_MISSING",
            f"以下规则没有确定执行方式：{names}{more}。请在场景规则编辑中补填或重新导入规则库；"
            "旧配置不会默认按普通核查执行。",
            details={"rules": missing},
        )
    if problems:
        raise BusinessError(
            "RULE_EXECUTION_MODE_INVALID",
            "规则执行方式与配置不匹配：" + "；".join(problems[:8]),
            details={"problems": problems},
        )


def _validate_calculation_codes(rules: list[dict[str, Any]]) -> None:
    """启用的计算规则编号必须唯一，且不能与其它启用规则同号。

    计算事实按业务编号回查产出规则；同号时无法确定来源，不能等到读取事实才报错。
    停用与待确认项不执行、不产出事实，让它们参与判重只会把可用配置挡在发布之外，
    因此判重只看启用项——与 `_validate_execution_modes` 跳过停用规则的口径一致。
    """
    calculations = [rule for rule in rules if rule.get("calculation_ref") and rule.get("enabled")]
    codes = [str(rule.get("rule_code") or "") for rule in calculations]
    if not all(codes) or len(codes) != len(set(codes)):
        raise BusinessError(
            "RULE_CODE_CONFLICT", "计算规则编号缺失或重复，无法确定结果来源",
            details={"rule_codes": codes},
        )
    # 计算事实按 result_code 落库与回查；两条计算规则同号时当前事实会互相覆盖。
    result_codes = [str(rule.get("result_code") or "") for rule in calculations]
    if not all(result_codes) or len(result_codes) != len(set(result_codes)):
        raise BusinessError(
            "RULE_CODE_CONFLICT", "计算结果编号缺失或重复，无法确定事实来源",
            details={"result_codes": result_codes},
        )
    calculation_keys = {rule["rule_key"] for rule in calculations}
    by_code: dict[str, list[str]] = {}
    for rule in rules:
        if not rule.get("enabled"):
            continue
        by_code.setdefault(str(rule.get("rule_code") or ""), []).append(rule["rule_key"])
    conflicts = {
        code: sorted(keys) for code, keys in by_code.items()
        if len(keys) > 1 and calculation_keys.intersection(keys)
    }
    if conflicts:
        raise BusinessError(
            "RULE_CODE_CONFLICT", "计算规则编号与其它规则相同，无法确定结果来源",
            details={"conflicts": conflicts},
        )


def _validate_output_producers(rules: list[dict[str, Any]]) -> None:
    """同一字段只能有一条启用规则声明为产出。

    字段事实按身份只保留一个当前值；两条启用规则产出同一字段时，写入与撤销都无法
    判断该由谁负责，运行期只会静默互相覆盖。这里在编译期直接拒绝。
    """
    producers: dict[str, set[str]] = {}
    for rule in rules:
        if not rule.get("enabled"):
            continue
        for field_key in rule.get("outputs") or []:
            producers.setdefault(str(field_key), set()).add(rule["rule_key"])
    conflicts = {field_key: sorted(keys) for field_key, keys in producers.items() if len(keys) > 1}
    if conflicts:
        raise BusinessError(
            "RULE_OUTPUT_CONFLICT", "同一字段被多条启用规则声明为产出，无法确定当前值来源",
            details={"conflicts": conflicts},
        )


def _validate_field_fact_identity(fields: list[dict[str, Any]]) -> None:
    """所有字段的完整事实身份必须唯一。

    人工核对允许给没有自动抽取的字段补值，任何字段都可能形成事实；运行期
    `_current_field_fact_pairs` 也按全部字段配对。因此唯一性必须覆盖全部字段，
    只校验启用规则的产出会让重复身份等到写入事实才暴露。
    """
    identities: dict[tuple[Any, ...], str] = {}
    for field in fields:
        identity = fact_projection.field_fact_identity(field)
        if identity in identities:
            raise BusinessError(
                "RULE_FIELD_IDENTITY_CONFLICT", "多个字段使用相同的完整事实身份，无法确定事实归属",
                details={"field_keys": [identities[identity], field["field_key"]], "identity": list(identity)},
            )
        identities[identity] = field["field_key"]


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
    fields_by_code: dict[str, list[dict[str, Any]]] = {}
    for field in fields:
        fields_by_code.setdefault(str(field.get("source_ref") or field["field_key"]), []).append(field)
    for field in fields:
        field["node_code"] = EXECUTION_CODE
        field["enabled"] = active(field)
        field.setdefault("period_scope", "NOT_APPLICABLE")
        field.setdefault("statement_scope", "NOT_APPLICABLE")
    for rule in rules:
        rule["node"] = rule["node_type"] = EXECUTION_CODE
        rule["enabled"] = active(rule)
        rule["rule_code"] = rule.get("rule_code") or rule.get("source_ref") or rule["rule_key"]
        rule.setdefault("operator", OPERATOR_LLM_EXECUTE)
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
        reference_errors = []
        for parameter in calculation.get("parameters") or []:
            if parameter.get("source_type") == "FIELD":
                code = str(parameter.get("source_code") or "")
                candidates = [field_by_key[code]] if code in field_by_key else fields_by_code.get(code, [])
                if len(candidates) == 1:
                    field = candidates[0]
                    inputs.append({"field_key": field["field_key"], "material_key": field["material_key"]})
                    parameter["resolved_field_key"] = field["field_key"]
                    parameter["resolved_material_key"] = field["material_key"]
                else:
                    parameter.pop("resolved_field_key", None)
                    parameter.pop("resolved_material_key", None)
                    reference_errors.append({"parameter_code": parameter.get("parameter_code"), "source_code": code,
                                             "status": "MISSING" if not candidates else "AMBIGUOUS",
                                             "field_keys": [field["field_key"] for field in candidates]})
        calculation["field_reference_errors"] = reference_errors
        calculation["depends_on_field_codes"] = [p["source_code"] for p in calculation.get("parameters") or [] if p.get("source_type") == "FIELD"]
        key = stable_key("rule", namespace, f"calculation:{calculation.get('candidate_ref') or calculation['rule_code']}")
        # 方案往返时计算执行项已经存在，不重新产生同一业务项。
        existing = next((item for item in rules if item.get("calculation_ref") == calculation["rule_code"]), None)
        if existing is not None:
            existing["enabled"] = active(existing) and active(calculation)
            existing["calculation_definition"] = deepcopy(calculation)
            existing["inputs"] = inputs
            existing["materials"] = list(dict.fromkeys(i["material_key"] for i in inputs))
            existing.pop("dependency_error", None)
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
        system_mode = {
            "EXTRACTION": MODE_EXTRACT,
            "CALCULATION": MODE_CALCULATE,
            "REPORT": MODE_REPORT,
        }.get(rule["kind"])
        if system_mode:
            rule["execution_mode"] = system_mode
        else:
            rule.setdefault("execution_mode", MODE_UNKNOWN)
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
        missing += [f"parameter:{item['parameter_code']}:{item['source_code']}:{item['status']}"
                    for item in (rule.get("calculation_definition") or {}).get("field_reference_errors", [])]
        if missing:
            rule["dependency_error"] = missing
        else:
            rule.pop("dependency_error", None)
    _validate_calculation_codes(rules)
    _validate_output_producers(rules)
    _validate_execution_modes(rules)
    _validate_field_fact_identity(fields)
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


def dependency_indexes(
    all_rules: list[dict[str, Any]],
) -> tuple[dict[str, set[str]], dict[str, list[str]]]:
    """字段产出规则与业务编号索引；调用方按需构建一次后复用。"""
    producers: dict[str, set[str]] = {}
    by_code: dict[str, list[str]] = {}
    for item in all_rules:
        for field in item.get("outputs") or []:
            producers.setdefault(field, set()).add(item["rule_key"])
        for code in {item.get("result_code"), item.get("rule_code")} - {None, ""}:
            by_code.setdefault(str(code), []).append(item["rule_key"])
    return producers, by_code


def _dependency_refs(
    rule: dict[str, Any], *, producers: dict[str, set[str]], by_code: dict[str, list[str]],
) -> tuple[set[str], set[str]]:
    """本条规则直接引用的规则键与无法唯一解析的引用。

    只按显式引用解析：字段输入的产出规则、`source_rule_key(s)`、唯一命中的
    `source_rule_codes` / `CALC_RESULT`。找不到或命中多条的业务编号不猜测，
    作为无法解析的引用交调用方处理。
    """
    deps = set(rule.get("source_rule_keys") or [])
    if rule.get("source_rule_key"):
        deps.add(rule["source_rule_key"])
    unresolved: set[str] = set()
    for item in rule.get("inputs") or []:
        matches = producers.get(item["field_key"])
        if matches:
            deps.update(matches)
        else:
            unresolved.add(f"field:{item['field_key']}")
    calculation = rule.get("calculation_definition") or {}
    codes = [
        *(rule.get("source_rule_codes") or []),
        *(calculation.get("depends_on_result_codes") or []),
        *(p["source_code"] for p in calculation.get("parameters") or []
          if p.get("source_type") == "CALC_RESULT"),
    ]
    for code in codes:
        definitions = by_code.get(str(code), [])
        if len(definitions) == 1:
            deps.add(definitions[0])
        else:
            unresolved.add(str(code))
    return deps, unresolved


def rule_dependencies(
    rule: dict[str, Any], *, all_rules: list[dict[str, Any]],
    indexes: tuple[dict[str, set[str]], dict[str, list[str]]] | None = None,
) -> set[str]:
    """本条规则直接依赖的规则键；报告与无法解析的引用由调用方补充。

    `indexes` 传入同一批规则构建好的 `dependency_indexes`，逐条调用时不必重复扫描全部规则。
    """
    producers, by_code = indexes or dependency_indexes(all_rules)
    deps, _ = _dependency_refs(rule, producers=producers, by_code=by_code)
    if rule.get("kind") == "EXTRACTION":
        deps.discard(rule["rule_key"])
    return deps & {item["rule_key"] for item in all_rules}


def execution_order(rules: list[dict[str, Any]], *, all_rules: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """只按显式输入/结果引用排序；无法满足的引用与环在执行项上暴露。"""
    rules = deepcopy(rules)
    selected = {r["rule_key"] for r in rules}
    known_by_key = {r["rule_key"]: r for r in all_rules}
    producers, by_code = dependency_indexes(all_rules)
    dependencies = {}
    for rule in rules:
        rule.pop("unavailable_dependencies", None)
        rule.pop("dependency_details", None)
        deps, unresolved = _dependency_refs(rule, producers=producers, by_code=by_code)
        # 报告消费任务结果集合，等该任务其他生效项执行完再生成。
        if rule.get("kind") == "REPORT":
            deps.update(r["rule_key"] for r in rules if r.get("kind") != "REPORT")
        if rule.get("kind") == "EXTRACTION":
            deps.discard(rule["rule_key"])
        missing_codes = sorted(unresolved)
        if deps - selected or missing_codes:
            rule["unavailable_dependencies"] = sorted(set(deps - selected) | set(missing_codes))
            rule["dependency_details"] = []
            for reference in rule["unavailable_dependencies"]:
                definitions = (
                    [known_by_key[reference]] if reference in known_by_key
                    else [known_by_key[key] for key in by_code.get(reference, []) if key in known_by_key]
                )
                definition = definitions[0] if len(definitions) == 1 else None
                rule["dependency_details"].append({
                    "reference": reference,
                    "name": definition.get("name") if definition is not None else None,
                    "status": (
                        "AMBIGUOUS" if len(definitions) > 1 else "MISSING" if definition is None else
                        "PENDING" if definition.get("config_status", definition.get("status")) == CONFIG_STATUS_PENDING else
                        "DISABLED" if not active(definition) else "NOT_SELECTED"
                    ),
                })
        dependencies[rule["rule_key"]] = deps & selected
    ordered, remaining, done = [], list(rules), set()
    position = {rule["rule_key"]: index for index, rule in enumerate(rules)}
    while remaining:
        ready = [r for r in remaining if dependencies[r["rule_key"]] <= done]
        # 依赖是硬约束；同一层内按执行动作排序，避免汇总/风险规则抢在来源规则之前。
        ready.sort(key=lambda item: (
            EXECUTION_MODE_ORDER.get(str(item.get("execution_mode") or MODE_UNKNOWN), 99),
            position.get(item["rule_key"], 0),
        ))
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
