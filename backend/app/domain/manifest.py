import hashlib
import json
from copy import deepcopy
from datetime import datetime
from typing import Any


from . import tool_catalog, field_strategy
from .audit_vocab import SCOPE_NOT_APPLICABLE
from ..common.exceptions import BusinessError
from .identity import stable_key
from .report_data import report_data_refs
from .rule_library import active, is_risk_rule
from .proposals import MaterialProposal, FieldProposal, RuleProposal, ReportSectionProposal


def _proposal_context(item: dict[str, Any], model: type) -> dict[str, Any]:
    """补充业务属性进入执行上下文，不覆盖编译器维护的身份与类型。"""
    return deepcopy({**item.get("business_context", {}),
                     **{key: value for key, value in item.items() if key not in model.model_fields}})


# 参数契约常量见文件顶部从 `parameter_contract` 的导入（叶子模块，避免成环）。
# 历史条件码仅用于展示标签；当前条件可为完整业务原文，不限制为这两种。
RISK_CONDITION_LABELS = {
    "ANY_ABNORMAL": "任一关联检查不合格",
    "ALL_ABNORMAL": "全部关联检查不合格",
}
# 算子与比较方式的业务中文口径，供页面展示"实际比较对象和比较方式"。
# 工具标签取自统一工具目录。
OPERATOR_LABELS = tool_catalog.OPERATOR_LABELS

DATE_COMPARISON_LABELS = {"EQ": "同一天", "GTE": "不早于", "LTE": "不晚于", "GT": "晚于", "LT": "早于"}
DECIMAL_COMPARISON_LABELS = {"EQ": "等于", "GTE": "不小于", "LTE": "不大于", "ABS_DIFF_LTE": "绝对差不超过"}
DECIMAL_SUM_COMPARISON_LABELS = {"EQ": "等于", "GTE": "不小于", "LTE": "不大于"}
APPLICABILITY_LABELS = {"ALWAYS": "始终适用", "MATERIALS_UPLOADED": "所引用材料均已上传"}
def comparison_label(operator: str, comparison: Any) -> str | None:
    """把比较方式译成业务中文，供页面与报告说明「实际是怎么比的」；非比较取值原样返回。"""
    if not isinstance(comparison, str):
        return None
    if operator == "DATE_COMPARE":
        return DATE_COMPARISON_LABELS.get(comparison, comparison)
    if operator == "DECIMAL_COMPARE":
        return DECIMAL_COMPARISON_LABELS.get(comparison, comparison)
    if operator == "DECIMAL_SUM_COMPARE":
        return DECIMAL_SUM_COMPARISON_LABELS.get(comparison, comparison)
    return comparison


def operator_uses_addends(operator: str) -> bool:
    """该算子是否由「对照内容」的加总式展开成多个输入。"""
    return operator == "DECIMAL_SUM_COMPARE"


def applicability_view(rule: dict[str, Any]) -> dict[str, Any]:
    """把适用条件统一成结构化形式并附中文口径，供预览与执行使用。"""
    applicability = rule.get("applicability")
    if not isinstance(applicability, dict) or not applicability.get("kind"):
        applicability = {"kind": "ALWAYS"}
    return {
        "kind": applicability["kind"],
        "label": APPLICABILITY_LABELS.get(applicability["kind"], applicability["kind"]),
        "material_keys": list(applicability.get("material_keys", [])),
    }


def _compiled_applicability(
    rule: dict[str, Any],
    inputs: list[dict[str, Any]],
    material_scope: list[str],
) -> dict[str, Any]:
    """把适用条件固化成已发布配置里的结构化字段。

    “所引用材料均已上传”的判定材料来自该规则明确引用的输入字段所属材料与规则声明的材料范围，
    去重后保持稳定顺序；不按文件名或中文名相似度猜材料。
    """
    applicability = applicability_view(rule)
    material_keys: list[str] = []
    for item in inputs:
        if item["material_key"] not in material_keys:
            material_keys.append(item["material_key"])
    for material_key in material_scope:
        if material_key not in material_keys:
            material_keys.append(material_key)
    return {**deepcopy(rule.get("applicability") or {}),
            "kind": applicability["kind"], "material_keys": material_keys}




def rule_comparison_view(
    rule: dict[str, Any],
    material_names: dict[str, str],
    field_labels: dict[str, str],
    rule_names: dict[str, str],
    field_meta: dict[str, dict[str, Any]] | None = None,
    *,
    calculation_meta: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """把规则的真实比较对象、比较方式、参数和来源组织成业务可读结构。

    预览必须能一次看清四件事：引用原文、实际比较对象、方向与阈值、单位和适用条件，
    否则用户只能凭模型改写后的措辞判断这条规则是否符合业务要求。
    """
    parameters = rule.get("parameters") if isinstance(rule.get("parameters"), dict) else {}
    meta = field_meta or {}
    inputs = [
        {
            "material_name": material_names.get(item.get("material_ref")),
            "field_name": field_labels.get(item.get("field_ref")),
            "field_ref": item.get("field_ref"),
            "value_type": meta.get(item.get("field_ref"), {}).get("value_type"),
            "unit": meta.get(item.get("field_ref"), {}).get("unit"),
            "currency": meta.get(item.get("field_ref"), {}).get("currency"),
        }
        for item in rule.get("inputs", [])
    ]
    applicability = applicability_view(rule)
    requirement = rule.get("requirement") or None
    direction = comparison_label(rule.get("operator", ""), parameters.get("operator"))
    threshold = parameters.get("threshold")
    source_rule_refs = list(dict.fromkeys([
        *(rule.get("source_rule_refs") or []),
        *([rule["source_rule_ref"]] if rule.get("source_rule_ref") else []),
    ]))
    return {
        "operator_label": OPERATOR_LABELS.get(rule.get("operator"), rule.get("operator")),
        "left": inputs[0] if inputs else None,
        "right": inputs[1] if len(inputs) > 1 else None,
        # 加总比较的右侧是多个加数：它们必须全部展示。"只显示两个输入"会让预览与执行不一致，
        # 也会让用户以为第三个加数没有被采用。单字段比较保持原有 extra_inputs 语义。
        "addends": inputs[1:] if operator_uses_addends(rule.get("operator", "")) else [],
        "extra_inputs": [] if operator_uses_addends(rule.get("operator", "")) else inputs[2:],
        "comparison_label": direction,
        "parameters": parameters,
        # 审核要求原文：页面展示的必须是用户实际填写的内容，不是模型的改写结果。
        "requirement": requirement,
        "applicability": {"kind": applicability["kind"], "material_keys": applicability["material_keys"]},
        "applicability_label": applicability["label"],
        "source_rule_name": rule_names.get(rule.get("source_rule_ref")) if rule.get("source_rule_ref") else None,
        "source_rule_refs": source_rule_refs,
        "source_rule_names": [rule_names.get(ref) or ref for ref in source_rule_refs],
        "calculation_sources": [
            {
                "result_code": code,
                "rule_name": calculation_meta.get(code, {}).get("rule_name"),
                "node_name": calculation_meta.get(code, {}).get("node_name"),
            }
            for code in dict.fromkeys(rule.get("source_rule_codes") or [])
        ],
        "scope_material_names": [material_names.get(ref) for ref in rule.get("material_refs", [])],
        "scope_material_refs": list(rule.get("material_refs", [])),
        "output_field_names": [field_labels.get(ref) for ref in rule.get("output_field_refs", [])],
        # 方向、阈值、单位与支持状态必须显式可见，不能只靠 operator/parameters 原始编码。
        "direction_label": direction,
        "threshold": threshold,
        "input_units": [item.get("unit") for item in inputs],
        "input_currencies": [item.get("currency") for item in inputs],
        # 风险成立条件与等级原文：等级差异必须显式可见，不能用较低风险等级覆盖来源异常等级。
        "risk_condition": rule.get("risk_condition"),
        "risk_condition_label": RISK_CONDITION_LABELS.get(rule.get("risk_condition") or "", rule.get("risk_condition")),
        "check_level": rule.get("check_level"),
        "risk_level_value": rule.get("risk_level_value"),
        "config_status": rule.get("config_status"),
    }


# 系统派生规则的算子：字段抽取与报告生成由编译期无条件补齐，业务不能手工创建。
SYSTEM_OPERATORS: frozenset[str] = frozenset({"LLM_EXTRACT", "REPORT_RENDER"})
# 系统派生规则的编号前缀。字段抽取、报告生成与材料检查的派生规则都带它，
# 但业务来源编号可能同形，分类时还须先看 `source_ref`；派生的材料检查规则算子
# 是 `MATERIAL_EXISTS`，业务也可能手工建一条同算子的规则。
SYSTEM_RULE_CODE_PREFIX = "SYS-"


def is_system_rule(rule: dict[str, Any]) -> bool:
    """只按实际派生来源识别，不因用户规则使用某个算子而丢弃它。"""
    return rule.get("generated_from") in {"FIELD", "REPORT"}


def refresh_calculation_references(calculations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """使用显式参数清单刷新计算依赖，不解析业务方法。"""
    refreshed = []
    for item in calculations:
        if not active(item):
            refreshed.append(deepcopy(item))
            continue
        parameters = item.get("parameters") or []
        refreshed.append({
            **item,
            "depends_on_result_codes": sorted({
                parameter["source_code"] for parameter in parameters
                if parameter.get("source_type") == "CALC_RESULT" and parameter.get("source_code")
            }),
        })
    return refreshed


def draft_config_of_import(
    *,
    proposals: dict[str, Any],
    namespace: str,
) -> dict[str, Any]:
    """将模型业务配置投影为草稿，保留原始业务编号与来源。"""
    from .rule_library import EXECUTION_VERSION, execution_container

    resolved = deepcopy(proposals)
    materials = resolved["materials"]
    fields = resolved["fields"]
    rules = resolved["rules"]
    sections = resolved["report_sections"]
    calculations = resolved["calculations"]
    # 同一 Excel 业务行可以拆出多个材料位，共享 source_id。内部身份使用候选引用，
    # source_id 仅保留为来源编号，不能让资产负债表、利润表等不同材料生成同一个键。
    material_keys = {
        item["candidate_ref"]: stable_key("mat", namespace, item["candidate_ref"])
        for item in materials
    }
    field_keys = {
        item["candidate_ref"]: stable_key("fld", namespace, item["candidate_ref"]) for item in fields
    }
    # 执行引用映射到内部字段键；同一原编号拆出的材料/期间不能互相覆盖。
    for calculation in calculations:
        for parameter in calculation["parameters"]:
            if parameter["source_type"] == "FIELD" and parameter["source_code"] in field_keys:
                parameter["source_code"] = field_keys[parameter["source_code"]]
    rule_keys = {item["candidate_ref"]: stable_key("rule", namespace, item["candidate_ref"]) for item in rules}
    for target_type, keys in (("MATERIAL", material_keys), ("FIELD", field_keys), ("RULE", rule_keys)):
        if len(set(keys.values())) != len(keys):
            owners: dict[str, list[str]] = {}
            for reference, key in keys.items():
                owners.setdefault(key, []).append(reference)
            raise BusinessError(
                "IMPORT_BUSINESS_CODE_DUPLICATED",
                "业务编号在当前场景内无法唯一确定配置对象",
                details={"target_type": target_type, "conflicting_refs": [
                    refs for refs in owners.values() if len(refs) > 1
                ]},
            )
    def resolve(
        reference: str, owner: dict[str, Any], target: str, *, multiple: bool = False
    ) -> str | list[str]:
        keys = {"MATERIAL": material_keys, "FIELD": field_keys, "RULE": rule_keys}[target]
        resolved = keys.get(reference, reference)
        return [resolved] if multiple else resolved


    manifest_like: dict[str, Any] = {
        "execution_contract_version": EXECUTION_VERSION,
        "nodes": [execution_container()],
        "materials": [
            {
                "material_key": material_keys[item["candidate_ref"]],
                "business_context": _proposal_context(item, MaterialProposal),
                "name": item["name"],
                "required": item["required"],
                "description": item.get("description", ""),
                "config_status": item.get("config_status"),
                "source_ref": item.get("source_id"),
                "origin": deepcopy(item.get("origin")),
            }
            for item in materials
        ],
        "fields": [],
        "rules": [],
        "report_template": [],
        "calculation_inputs": calculations,
    }
    for item in fields:
        material_key = resolve(item["material_ref"], item, "MATERIAL")
        field_key = field_keys[item["candidate_ref"]]
        manifest_like["fields"].append({
            "field_key": field_key,
            "business_context": _proposal_context(item, FieldProposal),
            "enabled": active(item),
            "config_status": item.get("config_status"),
            "material_key": material_key,
            "name": item["name"],
            "value_type": item["value_type"],
            "node_code": "RULE_EXECUTION",
            "unit": item.get("unit"),
            "currency": item.get("currency"),
            "artifact_source": item.get("artifact_source"),
            "extraction_strategy": item.get("extraction_strategy"),
            "structure_schema": item.get("structure_schema"),
            "period_scope": item.get("period_scope") or SCOPE_NOT_APPLICABLE,
            "statement_scope": item.get("statement_scope") or SCOPE_NOT_APPLICABLE,
            "required": item["required"],
            "instruction": item["instruction"],
            "source_ref": item.get("source_id"),
            "origin": deepcopy(item.get("origin")),
        })
    for item in rules:
        inputs = [
            {
                **deepcopy(entry),
                "field_key": resolve(entry["field_ref"], item, "FIELD"),
                "material_key": resolve(entry["material_ref"], item, "MATERIAL"),
            }
            for entry in item.get("inputs", [])
        ]
        source_keys: list[str] = []
        for reference in dict.fromkeys([
            *(item.get("source_rule_refs") or []),
            *([item["source_rule_ref"]] if item.get("source_rule_ref") else []),
        ]):
            for key in resolve(reference, item, "RULE", multiple=True):
                if key not in source_keys:
                    source_keys.append(key)
        material_scope = [resolve(ref, item, "MATERIAL") for ref in item.get("material_refs", [])]
        output_keys = [resolve(ref, item, "FIELD") for ref in item.get("output_field_refs", [])]
        manifest_like["rules"].append({
            "rule_key": rule_keys[item["candidate_ref"]],
            "business_context": _proposal_context(item, RuleProposal),
            "rule_code": item.get("rule_code") or item.get("source_id"),
            "name": item["name"],
            "node": "RULE_EXECUTION",
            "operator": item["operator"],
            "rule_role": "RISK" if is_risk_rule(item) else "CHECK",
            "enabled": active(item),
            "inputs": inputs,
            "outputs": output_keys,
            "materials": material_scope,
            "parameters": item.get("parameters") or {},
            "requirement": item.get("requirement") or "",
            "description": (item.get("requirement") or "") if is_risk_rule(item) else "",
            "applicability": _compiled_applicability(item, inputs, material_scope),
            "source_rule_key": source_keys[0] if source_keys else None,
            "source_rule_keys": source_keys,
            "source_rule_codes": list(item.get("source_rule_codes") or []),
            "check_level": item.get("check_level"),
            "check_operator": item.get("check_operator"),
            "risk_level_value": item.get("risk_level_value"),
            "risk_condition": item.get("risk_condition"),
            "config_status": item.get("config_status"),
            "source_ref": item.get("rule_code") or item.get("source_id"),
            "origin": deepcopy(item.get("origin")),
        })
    # 报告可消费字段、计算与规则结果；未知引用如实保留给执行阶段说明。
    report_reference_keys = {
        **field_keys, **rule_keys,
        **{item["candidate_ref"]: item["result_code"] for item in calculations},
    }
    known_field_keys = set(field_keys.values())
    for item in sections:
        data_refs = list(dict.fromkeys(
            report_reference_keys.get(ref, ref) for ref in item.get("field_refs", [])
        ))
        field_scope = [ref for ref in data_refs if ref in known_field_keys]
        material_scope = [resolve(ref, item, "MATERIAL") for ref in item.get("material_refs", [])]
        section_identity = item["candidate_ref"]
        manifest_like["report_template"].append({
            "section_key": stable_key("sec", namespace, section_identity),
            "business_context": _proposal_context(item, ReportSectionProposal),
            "section_name": item["section_name"],
            "content_instruction": item["content_instruction"],
            "field_keys": field_scope,
            "data_refs": data_refs,
            "material_keys": material_scope,
            "report_code": item.get("report_code"),
            "report_name": item.get("report_name"),
            "formats": list(item.get("formats") or []),
            "section_order": item.get("section_order"),
            "content_type": item.get("content_type"),
            "result_sort": item.get("result_sort"),
            "risk_level_order": list(item.get("risk_level_order") or []),
            "config_status": item.get("config_status"),
            "origin": deepcopy(item.get("origin")),
        })
    return editable_config_of(manifest_like, namespace=namespace)


# 字段「取数期间 / 报表口径」两个列的业务中文名。发布编译、运行期写事实、
# 草稿表单校验三处都要用它组报错文案，只维护一份。
FIELD_SCOPE_LABELS = {"period_scope": "取数期间", "statement_scope": "报表口径"}


def normalize_field_scope(
    field: dict[str, Any],
    key: str,
    *,
    status_code: int = 400,
) -> str:
    """字段口径的内部归一化。

    业务字段不必属于财报期间或报表口径体系；缺省只表示该口径不适用，
    不猜测本期、上期、合并或母公司。数据库事实定位仍使用显式 sentinel。
    """
    value = field.get(key)
    if value is None or (isinstance(value, str) and not value.strip()):
        return SCOPE_NOT_APPLICABLE
    if not isinstance(value, str):
        raise BusinessError(
            "MANIFEST_FIELD_SCOPE_INVALID",
            f"字段“{field.get('name')}”的{FIELD_SCOPE_LABELS.get(key, key)}取值无效",
            status_code=status_code,
            details={"field_key": field.get("field_key"), "column": key, "value": value},
        )
    return value.strip()


def editable_config_of(manifest: dict[str, Any], *, namespace: str) -> dict[str, Any]:
    """已发布 manifest → 可编辑配置行的唯一映射口径。

    两个调用方都从这里取：场景缺草稿时用最新已发布版本初始化、创建方案时把场景版本
    复制成方案自己的配置。只允许一处口径——各写一份必然漂移，表现是同一个版本经两条
    路径回来的配置不一样，而且不报错。

    只接收当前规则库契约，原始定义与身份直接保留，不转换旧执行格式。

    返回的每项都是不含归属 id 的字典；`rules` 不跳过系统派生规则，是否保留由调用方决定。
    """
    from .rule_library import execution_container_of
    execution_container_of(manifest)
    applicability_of = lambda rule: rule.get("applicability") if isinstance(rule.get("applicability"), dict) else {}
    optional = lambda key, value: {key: value} if value is not None else {}

    nodes = [
        {
            "node_code": str(node["node_code"]),
            "node_type": str(node.get("node_type") or node["node_code"]),
            "name": str(node.get("name") or node["node_code"]),
            "sequence_no": int(node.get("sequence_no") or 1),
            "depends_on": [str(code) for code in (node.get("depends_on") or [])],
            "config_json": node.get("config_json"),
            "enabled": bool(node.get("enabled", True)),
        }
        for node in manifest.get("nodes", [])
    ]
    materials = [
        {
            "material_key": material["material_key"],
            "business_context": deepcopy(material.get("business_context", {})),
            "name": material["name"],
            "required": bool(material.get("required")),
            "description": str(material.get("description") or ""),
            "config_status": material.get("config_status"),
            # 来源业务编号：导出时要按它写回 Excel 的「材料编号」，丢掉会让导出缺必填编号。
            "source_ref": material.get("source_ref"),
            "origin_json": material.get("origin"),
        }
        for material in manifest.get("materials", [])
    ]
    fields = [
        {
            "field_key": field["field_key"],
            "business_context": deepcopy(field.get("business_context", {})),
            "enabled": active(field),
            "config_status": field.get("config_status"),
            "material_key": field["material_key"],
            "name": field["name"],
            "value_type": field["value_type"],
            "node_code": field.get("node_code"),
            "extraction_strategy": field_strategy.strategy_of(field),
            "structure_schema_json": field.get("structure_schema"),
            "unit": field.get("unit"),
            # 币种是金额比较与加总的口径之一，不能只保留单位。
            "currency": field.get("currency"),
            # 产物来源：主体核对据此确定字段取自哪种产物，必须保留。
            "artifact_source": field.get("artifact_source"),
            # 取数期间与报表口径：计算参数按它们定位字段事实。
            "period_scope": normalize_field_scope(field, "period_scope"),
            "statement_scope": normalize_field_scope(field, "statement_scope"),
            "required": bool(field.get("required")),
            "instruction": str(field.get("instruction") or ""),
            "source_ref": field.get("source_ref"),
            "origin_json": field.get("origin"),
        }
        for field in manifest.get("fields", [])
    ]
    rules = []
    for rule in manifest.get("rules", []):
        applicability = applicability_of(rule)
        rules.append(
            {
                "rule_key": rule["rule_key"],
                "name": rule["name"],
                "node_code": rule["node"],
                "operator_code": rule["operator"],
                "enabled": active(rule),
                # 是不是系统派生规则。判据只有一处（`is_system_rule`），调用方据此决定去留：
                # 场景草稿丢掉它们（发布时重新派生），方案保留它们（用户可逐条取消）。
                "is_system": is_system_rule(rule),
                "rule_json": {
                    **deepcopy(rule),
                    "rule_code": rule.get("rule_code"),
                    "name": rule["name"],
                    "node": rule["node"],
                    "operator": rule["operator"],
                    "enabled": active(rule),
                    "inputs": [
                        deepcopy(item)
                        for item in rule.get("inputs", [])
                    ],
                    "outputs": list(rule.get("outputs") or []),
                    "materials": list(rule.get("materials", [])),
                    "parameters": dict(rule.get("parameters") or {}),
                    "requirement": str(rule.get("requirement") or ""),
                    "description": str(rule.get("description") or ""),
                    "applicability": {
                        **deepcopy(applicability),
                        "kind": applicability.get("kind", "ALWAYS"),
                        **(
                            {"material_keys": list(applicability.get("material_keys") or [])}
                            if applicability.get("material_keys")
                            else {}
                        ),
                    },
                    "source_rule_key": rule.get("source_rule_key"),
                    # 以下业务配置事实是人工维护的审核口径，页面表单不一定每一项都能编辑，
                    # 整块覆盖 rule_json 时不能丢：检查等级与「合格要求」原文、风险等级原文与
                    # 成立条件、风险的多来源、业务配置状态与能力缺口标记。
                    **optional("check_level", rule.get("check_level")),
                    **optional("check_operator", rule.get("check_operator")),
                    **optional("risk_level_value", rule.get("risk_level_value")),
                    **optional("risk_condition", rule.get("risk_condition")),
                    **(
                        {"source_rule_keys": list(rule["source_rule_keys"])}
                        if rule.get("source_rule_keys")
                        else {}
                    ),
                    **(
                        {"source_rule_codes": list(rule["source_rule_codes"])}
                        if rule.get("source_rule_codes")
                        else {}
                    ),
                    **optional("config_status", rule.get("config_status")),
                },
                "source_ref": rule.get("source_ref"),
                "origin_json": rule.get("origin"),
            }
        )
    sections = []
    for index, section in enumerate(manifest.get("report_template", []), start=1):
        section_key = section["section_key"]
        sections.append({
            "section_key": section_key,
            "business_context": deepcopy(section.get("business_context", {})),
            "title": section.get("section_name", f"章节{index}"),
            "content_instruction": str(section.get("content_instruction") or ""),
            "field_keys": list(section.get("field_keys", [])),
            "data_refs": report_data_refs(section),
            # 报告分组属性：少了它们，页面保存一次就会把按编号分组的报告
            # 退回成旧的「附加展示章节」。
            "report_code": section.get("report_code"),
            "report_name": section.get("report_name"),
            "material_keys": list(section.get("material_keys") or []) or None,
            "formats": list(section.get("formats") or []) or None,
            "section_order": section.get("section_order"),
            "content_type": section.get("content_type"),
            "result_sort": section.get("result_sort"),
            "risk_level_order": list(section.get("risk_level_order") or []),
            "config_status": section.get("config_status"),
            "origin_json": section.get("origin"),
        })
    calculation_json = deepcopy(manifest["calculation_inputs"])
    return {
        # 键名必须与 `compile_form_manifest` 的参数一致：调用方用 `**inputs` 展开，
        # 写成 `nodes` 会让编译在缺 `node_instances` 处直接 TypeError。
        "node_instances": nodes,
        "materials": materials,
        "fields": fields,
        "rules": rules,
        "sections": sections,
        "calculation_json": calculation_json,
    }


def _config_value(item: Any, key: str, default: Any = None) -> Any:
    """从配置行取一个列值。行可以是 ORM 行，也可以是字典。

    方案的节点与选用 Sheet 以只读 JSON 冻结（不是表行），场景的则是 `scenario_draft_*` 行；
    两者的列名完全一致，因此同一份映射能服务两边。
    """
    if isinstance(item, dict):
        return item.get(key, default)
    return getattr(item, key, default)


def compile_inputs_of_config(
    *,
    materials: list[Any],
    fields: list[Any],
    rules: list[Any],
    sections: list[Any],
    nodes: list[Any],
    profile_version: str | None,
    scenario_schema_config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """可编辑配置行 → `compile_form_manifest` 的入参。唯一映射口径。

    场景草稿发布与方案保存都从这里取 —— 各写一份必然漂移，表现是「同一个版本经两条
    路径编译出的产物不一样」，而且不会报错。行的类型允许混用（ORM 行 / 字典），
    见 `_config_value`。
    """
    # 规则的节点实例码 → 能力类型：编译期比的是能力类型，实例码是用户自己起的名字。
    node_type_by_code = {
        str(_config_value(node, "node_code")): _config_value(node, "node_type") for node in nodes
    }
    rule_items = [
        {
            **(_config_value(item, "rule_json") or {}),
            "rule_key": _config_value(item, "rule_key"),
            "name": _config_value(item, "name"),
            "node": _config_value(item, "node_code"),
            "node_type": node_type_by_code.get(_config_value(item, "node_code")),
            "operator": _config_value(item, "operator_code"),
            "enabled": bool(_config_value(item, "enabled")),
            "source_ref": _config_value(item, "source_ref"),
            "origin": _config_value(item, "origin_json"),
        }
        for item in rules
    ]
    # 风险规则通过来源规则触发，不直接维护材料范围。编译前把来源规则的材料、
    # 输入字段所属材料和输出字段所属材料汇总进风险规则，保证展示与执行口径一致。
    field_materials = {
        str(_config_value(item, "field_key")): str(_config_value(item, "material_key"))
        for item in fields
    }
    rules_by_key = {str(item["rule_key"]): item for item in rule_items}
    for rule in rule_items:
        if not active(rule) or not is_risk_rule(rule):
            continue
        material_keys = list(dict.fromkeys(str(key) for key in (rule.get("materials") or []) if str(key)))
        source_keys = list(dict.fromkeys([
            *(rule.get("source_rule_keys") or []),
            *([rule["source_rule_key"]] if rule.get("source_rule_key") else []),
        ]))
        for source_key in source_keys:
            source = rules_by_key.get(str(source_key))
            if source is None:
                continue
            for material_key in source.get("materials") or []:
                if material_key not in material_keys:
                    material_keys.append(material_key)
            for source_input in source.get("inputs") or []:
                material_key = source_input.get("material_key")
                if material_key and material_key not in material_keys:
                    material_keys.append(material_key)
            for field_key in source.get("outputs") or []:
                material_key = field_materials.get(str(field_key))
                if material_key and material_key not in material_keys:
                    material_keys.append(material_key)
        rule["materials"] = material_keys
    return {
        "materials": [
            {
                "material_key": _config_value(item, "material_key"),
                "business_context": deepcopy(_config_value(item, "business_context", {})),
                "name": _config_value(item, "name"),
                "required": bool(_config_value(item, "required")),
                "description": str(_config_value(item, "description") or ""),
                "config_status": _config_value(item, "config_status"),
                # 来源业务编号进入编译结果，导出时按它写回 Excel 对应列。
                "source_ref": _config_value(item, "source_ref"),
                "origin": _config_value(item, "origin_json"),
            }
            for item in materials
        ],
        "fields": [
            {
                "field_key": _config_value(item, "field_key"),
                "business_context": deepcopy(_config_value(item, "business_context", {})),
                "enabled": bool(_config_value(item, "enabled", True)),
                "config_status": _config_value(item, "config_status"),
                "material_key": _config_value(item, "material_key"),
                "name": _config_value(item, "name"),
                "value_type": _config_value(item, "value_type"),
                "node_code": _config_value(item, "node_code"),
                "extraction_strategy": _config_value(item, "extraction_strategy"),
                "structure_schema": _config_value(item, "structure_schema_json"),
                "unit": _config_value(item, "unit"),
                # 币种是金额比较与加总的口径之一；产物来源决定主体核对从哪种产物取值。
                "currency": _config_value(item, "currency"),
                "artifact_source": _config_value(item, "artifact_source"),
                # 口径只在业务实际涉及时由配置声明；缺省在进入 manifest 前归一为不适用。
                "period_scope": _config_value(item, "period_scope") or SCOPE_NOT_APPLICABLE,
                "statement_scope": _config_value(item, "statement_scope") or SCOPE_NOT_APPLICABLE,
                "required": bool(_config_value(item, "required")),
                "instruction": str(_config_value(item, "instruction") or ""),
                "source_ref": _config_value(item, "source_ref"),
                "origin": _config_value(item, "origin_json"),
            }
            for item in fields
        ],
        "rules": rule_items,
        "sections": [
            {
                "section_key": _config_value(item, "section_key"),
                "business_context": deepcopy(_config_value(item, "business_context", {})),
                "section_name": _config_value(item, "title"),
                "content_instruction": _config_value(item, "content_instruction"),
                "field_keys": list(_config_value(item, "field_keys") or []),
                "data_refs": report_data_refs({
                    "data_refs": _config_value(item, "data_refs"),
                    "field_keys": _config_value(item, "field_keys"),
                }),
                "report_code": _config_value(item, "report_code"),
                "report_name": _config_value(item, "report_name"),
                "material_keys": list(_config_value(item, "material_keys") or []),
                "formats": list(_config_value(item, "formats") or []),
                "section_order": _config_value(item, "section_order"),
                "content_type": _config_value(item, "content_type"),
                "result_sort": _config_value(item, "result_sort"),
                "risk_level_order": list(_config_value(item, "risk_level_order") or []),
                "config_status": _config_value(item, "config_status"),
                "origin": _config_value(item, "origin_json"),
            }
            for item in sections
        ],
        # 规则执行记录的技术容器。
        "node_instances": [
            {
                "node_code": _config_value(node, "node_code"),
                "node_type": _config_value(node, "node_type"),
                "name": _config_value(node, "name"),
                "sequence_no": _config_value(node, "sequence_no"),
                "depends_on": list(_config_value(node, "depends_on") or []),
                "config_json": _config_value(node, "config_json"),
                "enabled": bool(_config_value(node, "enabled")),
            }
            for node in nodes
        ],
        # 执行契约版本身份，记录生成配置时的当前协议；
        # 由调用方解析（没记来源时用当前版本），不在这里默认——那会让本函数反向依赖协议模块。
        "profile_version": profile_version,
        "scenario_schema_config": scenario_schema_config,
    }


def enabled_extraction_field_keys(
    manifest: dict[str, Any], enabled_rule_keys: list[str],
) -> set[str]:
    """实际字段取数范围来自生效规则的输出，不要求固定抽取算子。"""
    selected_keys = set(enabled_rule_keys)
    return {
        field_key
        for rule in manifest["rules"]
        if rule["rule_key"] in selected_keys and active(rule)
        for field_key in rule.get("outputs", [])
    }


def build_task_schema(manifest: dict[str, Any], enabled_rule_keys: list[str] | None, *, allow_empty: bool = False) -> dict[str, Any]:
    """冻结用户实际选中的生效规则；待确认和暂停项不参与执行。"""
    known = {r["rule_key"]: r for r in manifest["rules"]}
    requested = set(known) if enabled_rule_keys is None else set(enabled_rule_keys)
    if requested - known.keys():
        raise BusinessError("TASK_RULE_UNKNOWN", "任务选择了不存在的规则身份", details={"rule_keys": sorted(requested - known.keys())})
    selected = {key for key in requested if active(known[key])}
    field_keys = {key for r in manifest["rules"] if r["rule_key"] in selected for key in [*(i["field_key"] for i in r.get("inputs") or []), *(r.get("outputs") or [])]}
    extracted = enabled_extraction_field_keys(manifest, sorted(selected))
    return {"schema_version": "2.0", "enabled_rule_keys": sorted(selected), "unenabled_rule_keys": sorted(known.keys() - selected),
            "fields": [{**f, "extraction_enabled": f["field_key"] in extracted} for f in manifest["fields"] if f["field_key"] in field_keys]}


# 场景字段维护与导入候选共用的数据类型，包含大写金额（DECIMAL_CN）。
FORM_VALUE_TYPES = {"STRING", "DECIMAL", "AMOUNT", "DECIMAL_CN", "DATE", "BOOLEAN", "CLAUSE"}


def manifest_scope_hash(manifest: dict[str, Any]) -> str:
    """范围指纹：忽略编译时间等非业务字段，用于判断“两个版本内容是否一致”。

    仅比较规则库业务定义，与编译和发布时间无关。
    """
    payload = {
        key: value
        for key, value in manifest.items()
        if key not in {"compiled_at", "published_at", "source_workbook_id"}
    }
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def compile_form_manifest(
    *,
    scene_code: str,
    scene_name: str,
    namespace: str,
    materials: list[dict[str, Any]],
    fields: list[dict[str, Any]],
    rules: list[dict[str, Any]],
    sections: list[dict[str, Any]],
    node_instances: list[dict[str, Any]],
    profile_version: str,
    compiled_at: datetime,
    scenario_schema_config: dict[str, Any] | None = None,
    calculation_candidates: list[dict[str, Any]] | None = None,
    system_rule_enabled: dict[str, bool] | None = None,
    import_proposals: dict[str, Any] | None = None,
    source_kind: str = "FORM",
) -> dict[str, Any]:
    """只组织规则库执行项，不编译九类业务节点或固定算子路径。"""
    from .rule_library import compile_library
    return compile_library(**locals())
