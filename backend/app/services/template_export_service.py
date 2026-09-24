"""把已发布模板版本导出成规则 Excel。

导出的是**已生效配置**：来源是发布时固化、之后再不可修改的 manifest，
不是最初上传的原始文件——用户此前的裁决（改过的字段类型、材料引用、说明等）
都已经体现在 manifest 里。

列协议来自**统一模板协议**（`backend/config/unified_profile.json`），
系统只有这一份，因此导出不再需要按 manifest 的「格式版本」选一份 Profile：
那种分派本身就是已删除的双轨 Profile 的产物。

导出内容使用协议里声明的中文业务列，因此可以直接改一改再作为新模板重新导入。
"""

from io import BytesIO
from typing import Any

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.worksheet.datavalidation import DataValidation

from ..domain import unified_profile
from ..domain.manifest import DATE_COMPARISON_LABELS, DECIMAL_COMPARISON_LABELS, is_system_rule, rule_node_type_of
from ..domain.rule_expression import LIST_SEPARATOR, SUM_OPERATOR
from ..errors import BusinessError


HEADER_FILL = PatternFill("solid", fgColor="174A5B")
REQUIRED_FILL = PatternFill("solid", fgColor="FFF0C2")

# 清单与字段加总分别复用导入解析器的确定语法。
MULTI_REF_SEPARATOR = LIST_SEPARATOR

# 节点类型 → 该节点的规则落在哪张 Sheet 上。
# 这是**导出侧**的归属表：manifest 的规则按 node（节点实例码或能力类型）归属，
# 而 Excel 按 Sheet 组织，两者必须显式对应，不能靠名称相似度猜。
# 内部校验一行可作用于多个材料，字段名在各材料作用域内解析；
# 交叉校验按各侧材料解析字段名，报告字段写成「材料名.字段名」。

NODE_TYPE_SHEETS: dict[str, tuple[str, ...]] = {
    "MATERIAL_CHECK": ("MATERIAL_LIST",),
    "DOCUMENT_PREPARE": ("MATERIAL_QUALITY",),
    "FIELD_EXTRACTION": ("FIELD_EXTRACTION",),
    "INTERNAL_VALIDATE": ("INTERNAL_VALIDATE",),
    "CROSS_VALIDATE": ("CROSS_VALIDATE",),
    "RISK_RATING": ("RISK",),
    "REPORT_OUTPUT": ("REPORT_TEMPLATE",),
}


# 执行算子 → 业务「合格要求」的**声明取值**。导出必须写回声明取值而不是执行算子：
# `DECIMAL_COMPARE` 覆盖等于/不大于/不小于三种声明，只写执行算子会让导出的文件
# 在「合格要求」列出现一个协议里不存在的值（DECIMAL_COMPARE 不在 allowed_values 里），
# 重新导入直接报 RULE_ENUM_VALUE_INVALID。方向取自规则的 `parameters.operator`。
_ENGINE_TO_DECLARED: dict[str, dict[str, str]] = {
    # 日期：DATE_COMPARE + LTE/GTE → DATE_LTE / DATE_GTE
    "DATE_COMPARE": {"LTE": "DATE_LTE", "GTE": "DATE_GTE"},
    "DECIMAL_COMPARE": {"EQ": "DECIMAL_EQ", "LTE": "DECIMAL_LTE", "GTE": "DECIMAL_GTE"},
    "DECIMAL_SUM_COMPARE": {"EQ": "DECIMAL_EQ", "LTE": "DECIMAL_LTE", "GTE": "DECIMAL_GTE"},
}
_ENGINE_DIRECT: dict[str, str] = {
    "FIELD_EQUAL": "FIELD_EQUAL",
    "FIELD_NOT_EQUAL": "FIELD_EQUAL",
    "DECIMAL_CN_EQUAL": "DECIMAL_CN_EQUAL",
    "FIELD_NOT_EMPTY": "NOT_EMPTY",
    "CLAUSE_ABSENT": "CLAUSE_ABSENT",
}


def _threshold_text(rule: dict[str, Any]) -> str:
    """规则的「允许差值」原文（`parameters.threshold`）。未声明时留空，不代填 0。"""
    parameters = rule.get("parameters") if isinstance(rule.get("parameters"), dict) else {}
    value = parameters.get("threshold")
    return "" if value is None else str(value)


def _threshold_unit_text(rule: dict[str, Any]) -> str:
    """规则的「差值单位」原文。未声明时留空，不代填。"""
    parameters = rule.get("parameters") if isinstance(rule.get("parameters"), dict) else {}
    return str(parameters.get("threshold_unit") or "")


def declared_operator_of(rule: dict[str, Any]) -> str | None:
    """规则的**声明算子**（Excel「合格要求」列那个取值）。

    已发布配置里存的是**执行算子**（`DECIMAL_COMPARE` 等），而 Excel 列声明的是
    业务取值（`DECIMAL_EQ` 等）：两者不是一回事，方向信息在 `parameters.operator`。
    解析不出来时返回 None，由调用方如实留空——不拿执行算子冒充声明取值。
    """
    engine = str(rule.get("operator") or "")
    if engine in _ENGINE_DIRECT:
        return _ENGINE_DIRECT[engine]
    mapping = _ENGINE_TO_DECLARED.get(engine)
    if mapping is None:
        return None
    parameters = rule.get("parameters") if isinstance(rule.get("parameters"), dict) else {}
    return mapping.get(str(parameters.get("operator") or ""))


def _labels(column: dict[str, Any]) -> dict[str, str]:
    return {canonical: label for canonical, label in (column.get("value_labels") or {}).items()}


def _label_of(column: dict[str, Any] | None, value: Any) -> str:
    if value is None or column is None:
        return ""
    return _labels(column).get(str(value), str(value))


def _applicability_label(rule: dict[str, Any]) -> str:
    """适用条件必须显式成文；不支持的取值明确报错，不回落成空串静默丢失。"""
    applicability = rule.get("applicability")
    kind = applicability.get("kind") if isinstance(applicability, dict) else None
    kind = kind or "ALWAYS"
    if kind == "ALWAYS":
        return "始终适用"
    if kind == "MATERIALS_UPLOADED":
        return "所引用材料均已上传"
    raise BusinessError(
        "EXPORT_APPLICABILITY_UNSUPPORTED",
        f"规则“{rule.get('name')}”的适用条件当前不支持导出（{kind}）",
        status_code=500,
        details={"rule": rule.get("name"), "kind": kind},
    )


def _enabled_text(config_status: Any) -> str:
    """内部配置状态 → Excel 的「是否生效」列。

    Excel 里只有「是 / 否」两种取值：业务不表达「系统配没配好」，只表达
    「这一行参不参与审核」。因此 `DISABLED` 导出成「否」，其余（含历史 `PENDING`）
    导出成「是」——`PENDING` 是系统内部状态，已经不再由 Excel 填写；
    把它导成「否」会让一条待确认规则在重新导入后变成停用，那是对业务口径的改写。
    """
    return "否" if str(config_status or "").upper() == "DISABLED" else "是"


def _number_text(value: Any) -> str:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return ""
    return f"{value:g}"


def _requirement_text(rule: dict[str, Any], unit: str | None, operator_label: str) -> str:
    """审核要求列：已确认的方向与阈值写在最前，原始 requirement 原文追加在后。

    导入端以「审核要求」原文重建方向与阈值，含糊的原文不能覆盖已确认的参数，
    因此确认参数必须在前，原文只作追溯。
    """
    operator = rule.get("operator")
    parameters = rule.get("parameters") if isinstance(rule.get("parameters"), dict) else {}
    confirmed: list[str] = []
    if operator in {"FIELD_EQUAL", "FIELD_NOT_EQUAL"}:
        confirmed.append(operator_label)
    elif operator == "DATE_COMPARE":
        direction = DATE_COMPARISON_LABELS.get(parameters.get("operator"))
        if direction:
            confirmed.append(f"日期{direction}")
    elif operator == "DECIMAL_COMPARE":
        comparison = parameters.get("operator")
        if comparison == "ABS_DIFF_LTE":
            unit_text = f"（{unit}）" if unit else ""
            confirmed.append(f"金额绝对差不超过 {_number_text(parameters.get('threshold'))}{unit_text}")
        elif isinstance(comparison, str) and comparison:
            confirmed.append(f"金额{DECIMAL_COMPARISON_LABELS.get(comparison, comparison)}")
    if parameters.get("requires_manual_review") is True:
        confirmed.append("需要人工复核")
    confirmed_text = "\n".join(text for text in confirmed if text)
    requirement = rule.get("requirement")
    original = requirement.strip() if isinstance(requirement, str) and requirement.strip() else ""
    if original and original != confirmed_text:
        return f"{confirmed_text}\n原始要求：{original}" if confirmed_text else original
    return confirmed_text or original


def _quality_requirement_text(rule: dict[str, Any]) -> str:
    """材料质量的「检查要求」：保留原文，并把已确认的处理产物类型显式写出。"""
    parameters = rule.get("parameters") if isinstance(rule.get("parameters"), dict) else {}
    texts: list[str] = []
    requirement = rule.get("requirement")
    if isinstance(requirement, str) and requirement.strip():
        texts.append(requirement.strip())
    artifact_type = parameters.get("artifact_type")
    if rule.get("operator") == "FILE_QUALITY_CHECK" and isinstance(artifact_type, str) and artifact_type:
        texts.append(f"处理产物类型：{artifact_type}")
    return "\n".join(texts)


def build_scenario_version_export(manifest: dict[str, Any]) -> bytes:
    """把 manifest 导出成 Excel。

    导出的 Sheet 集合是**本版本冻结的 `selected_sheets`**：场景选中某张表是配置事实，
    不能由「当前有没有业务行」反推——已选择但当前零行的 Sheet 也必须导出表头，
    否则业务会以为这张表不需要维护，而它其实已经在执行范围内。

    因此先按 `selected_sheets` 的确定顺序建出全部工作表，再往里面填业务行：
    顺序来自冻结的清单，**不用无序集合**决定导出顺序。
    """
    profile = unified_profile.load_unified_profile()
    index = unified_profile.sheet_index(profile)
    material_codes = {item["material_key"]: item.get("source_ref") for item in manifest["materials"]}
    material_names = {item["material_key"]: item["name"] for item in manifest["materials"]}
    missing_material_codes = [key for key, code in material_codes.items() if not code]
    if missing_material_codes:
        raise BusinessError(
            "TEMPLATE_MATERIAL_CODE_MISSING",
            "材料缺少编号，无法导出跨表引用",
            details={"material_keys": missing_material_codes},
        )
    field_defs = {item["field_key"]: item for item in manifest["fields"]}

    def definition_of(role: str) -> dict[str, Any]:
        definition = index.get(role)
        if definition is None:
            raise BusinessError(
                "PROFILE_ROLE_MISSING", "统一模板协议缺少该工作表定义", status_code=500, details={"role": role}
            )
        return definition

    def column_of(role: str, key: str) -> dict[str, Any] | None:
        return next((column for column in definition_of(role)["columns"] if column["key"] == key), None)

    def field_name(field_key: str | None) -> str:
        definition = field_defs.get(field_key or "")
        if not definition:
            raise BusinessError(
                "TEMPLATE_FIELD_REF_MISSING",
                "导出规则引用了不存在的字段",
                details={"field_key": field_key},
            )
        code = definition.get("source_ref")
        if not code:
            raise BusinessError(
                "TEMPLATE_FIELD_CODE_MISSING",
                f"字段“{definition['name']}”缺少编号，无法导出可重新导入的规则库",
                details={"field_key": field_key},
            )
        return str(definition["name"])

    def joined_material_names(inputs: list[dict[str, Any]]) -> str:
        return MULTI_REF_SEPARATOR.join(
            dict.fromkeys(material_names[item["material_key"]] for item in inputs)
        )

    def joined_field_names(inputs: list[dict[str, Any]]) -> str:
        """只写字段名，不带材料前缀（合同审核校验表的字段列口径）。"""
        return SUM_OPERATOR.join(field_name(item.get("field_key")) for item in inputs)

    # 检查规则的 key → 风险名称列表；自身编号仍留在 09_风险规则。
    risk_names_by_source_key: dict[str, list[str]] = {}
    for rule in manifest["rules"]:
        if rule.get("operator") != "RISK_TRIGGER":
            continue
        risk_code = rule.get("rule_code")
        if not risk_code:
            raise BusinessError(
                "TEMPLATE_RISK_CODE_MISSING",
                f"风险“{rule['name']}”缺少编号，无法导出可重新导入的规则库",
                details={"rule_key": rule.get("rule_key")},
            )
        for source_key in (rule.get("source_rule_keys") or []):
            risk_names_by_source_key.setdefault(source_key, []).append(rule["name"])

    rows_by_role: dict[str, list[dict[str, Any]]] = {}

    # 已发布版本的 Sheet 范围只读 manifest.selected_sheets。
    # 缺失或空列表直接失败，不按内容反推，也不回退到协议全集。
    raw_selected = manifest.get("selected_sheets")
    if not isinstance(raw_selected, list) or not raw_selected:
        raise BusinessError(
            "TEMPLATE_SELECTED_SHEETS_MISSING",
            "已发布版本没有确认的 Sheet 绑定，不能导出规则库",
            status_code=409,
        )
    # `selected_sheets` 是**确定顺序**的清单（发布时按草稿的 sort_no 固化）：
    # 导出顺序按它来，不用集合——集合会丢掉顺序，同一份配置每次导出结果不同。
    selected_roles: list[str] = []
    for item in raw_selected:
        if not isinstance(item, dict) or not item.get("sheet_role"):
            raise BusinessError(
                "TEMPLATE_SELECTED_SHEETS_MISSING",
                "已发布版本的 Sheet 绑定记录缺少角色，不能导出规则库",
                status_code=409,
            )
        role = str(item["sheet_role"])
        if role in selected_roles:
            raise BusinessError(
                "TEMPLATE_SELECTED_SHEETS_DUPLICATED",
                f"已发布版本把工作表角色“{role}”声明了多次，不能导出规则库",
                status_code=409,
                details={"sheet_role": role},
            )
        if role not in index:
            raise BusinessError(
                "PROFILE_ROLE_MISSING",
                f"已发布版本选用的工作表“{role}”不在统一模板协议里，不能导出规则库",
                status_code=409,
                details={"sheet_role": role},
            )
        selected_roles.append(role)

    def role_selected(role: str) -> bool:
        return role in selected_roles

    def rows_of(role: str) -> list[dict[str, Any]]:
        definition_of(role)
        return rows_by_role.setdefault(role, [])

    def node_type_of(rule: dict[str, Any]) -> str:
        """规则归属的能力类型。用 manifest 的**单一来源**实现。

        自己写一份 `rule.get("node_type") or rule.get("node") or ""` 会在两者都缺时
        静默返回空串，于是这条规则被静默丢出导出；manifest.rule_node_type_of 对同一输入
        明确报错，导出与发布的口径因此一致。
        """
        return rule_node_type_of(rule)

    def roles_for_node_type(node_type: str) -> tuple[str, ...]:
        """该能力类型可以写出的 Sheet 角色，**收窄到本版本选用的那些**。

        一种能力类型对应一张表（计算节点除外，它对应三张）。仍要按
        `role_selected` 过滤：本版本没有选用的表不能因为「有对应内容」而被写出来，
        否则重新导入会多出一张用户从没配过的表。
        """
        return tuple(
            role
            for role in NODE_TYPE_SHEETS.get(node_type, ())
            if role in index and role_selected(role)
        )

    # 材料位：一张表、一套列协议。
    for role in ("MATERIAL_LIST",):
        if role not in index or not role_selected(role):
            continue
        for material in manifest["materials"]:
            rows_of(role).append(
                {
                    "name": material["name"],
                    "required": "是" if material["required"] else "否",
                    "description": material.get("description") or "",
                    "source_id": material.get("source_ref") or "",
                    # 材料位的「是否生效」写**是**：已发布配置里不可能有停用材料
                    # （停用行在转换期就按 EXCLUDED 短路、不进候选，草稿也没有这一列）。
                    # 这里**不是**代填默认值——它是这份配置的真实状态。
                    # 曾经误写成 `_enabled_text(rule.get(...))`（上一轮循环泄漏的变量），
                    # 会让整份材料清单跟随最后一条规则的状态被写成「否」。
                    "enabled": "是",
                }
            )

    for rule in manifest["rules"]:
        # 系统派生规则（SYS-EXTRACT-* / SYS-REPORT-RENDER / SYS-MATERIAL-*）不导出：
        # 它们由字段、报告章节与「是否必传」在发布期确定性派生，不是业务维护的规则。
        # 尤其材料检查规则的能力类型是 MATERIAL_CHECK、会被写进 01_材料位——
        # 而 01 是**材料位定义**表，它的「是否必传」是行必填列，
        # 派生规则没有这个值，导出的文件重新导入会直接报 RULE_ROW_REQUIRED_CELL_EMPTY。
        if is_system_rule(rule):
            continue
        node_type = node_type_of(rule)
        if rule.get("operator") in {"MATERIAL_EXISTS", "FILE_QUALITY_CHECK"}:
            for role in roles_for_node_type(node_type):
                if role not in index:
                    continue
                operator_column = column_of(role, "operator")
                rows_of(role).append(
                    {
                        "name": rule["name"],
                        "source_id": rule.get("rule_code") or "",
                        "material_ref": MULTI_REF_SEPARATOR.join(
                            material_names[key] for key in (rule.get("materials") or [])
                        ),
                        "material_refs": MULTI_REF_SEPARATOR.join(
                            material_names[key] for key in (rule.get("materials") or [])
                        ),
                        # 「检查项目」与「合格要求」都是**枚举列**：必须按标签写回，
                        # 写内部编码会让重新导入在枚举校验处被直接拒。
                        "check_item": _label_of(
                            column_of(role, "check_item"), (rule.get("parameters") or {}).get("check_item")
                        ),
                        "requirement": _label_of(
                            column_of(role, "requirement"), (rule.get("parameters") or {}).get("requirement")
                        ),
                        "criterion": (rule.get("parameters") or {}).get("criterion") or "",
                        "standard_value": str((rule.get("parameters") or {}).get("standard_value") or ""),
                        "unit": str((rule.get("parameters") or {}).get("unit") or ""),
                        "operator": _label_of(operator_column, rule.get("operator")),
                        "risk_refs": MULTI_REF_SEPARATOR.join(
                            risk_names_by_source_key.get(rule["rule_key"], [])
                        ),
                        "enabled": _enabled_text(rule.get("config_status")),
                    }
                )
            continue
        if rule.get("operator") in {"FIELD_EQUAL", "FIELD_NOT_EQUAL", "DATE_COMPARE", "DECIMAL_COMPARE",
                                    "DECIMAL_SUM_COMPARE", "FIELD_NOT_EMPTY", "CLAUSE_ABSENT", "DECIMAL_CN_EQUAL"}:
            for role in roles_for_node_type(node_type):
                if role not in index:
                    continue
                operator_column = column_of(role, "operator")
                inputs = rule.get("inputs", [])
                unit = next(
                    (
                        definition["unit"]
                        for item in inputs
                        if (definition := field_defs.get(item.get("field_key"))) and definition.get("unit")
                    ),
                    None,
                )
                rows_of(role).append(
                    {
                        "name": rule["name"],
                        "source_id": rule.get("rule_code") or "",
                        "left_material_ref": joined_material_names(inputs[:1]),
                        # 内部校验的一行可作用于多个材料，字段名在各材料内解析；
                        # 左右材料各自限定字段作用域，可直接写字段名称。
                        "left_field_ref": joined_field_names(inputs[:1]),
                        "right_material_ref": joined_material_names(inputs[1:]),
                        "right_field_ref": joined_field_names(inputs[1:]),
                        # 「适用材料」是**行必填列**：它写的是这条检查适用的材料范围。
                        # 内部校验按材料逐份展开，导出时必须写回，否则重新导入直接报
                        # 「该行已填写业务内容，不能留空这些口径列」。
                        "material_refs": joined_material_names(
                            [{"material_key": key} for key in (rule.get("materials") or [])]
                        ),
                        "operator": _label_of(operator_column, declared_operator_of(rule)),
                        # 「允许差值」与「差值单位」是行必填口径列（加总比较要求必须给出阈值），
                        # 导出必须写回：丢了它们，重新导入会直接报 RULE_THRESHOLD_REQUIRED。
                        "threshold": _threshold_text(rule),
                        "threshold_unit": _threshold_unit_text(rule),
                        # 「检查等级」与「关联风险」是导入期必填的业务口径列：
                        # 检查等级缺失直接报 RULE_CHECK_LEVEL_REQUIRED。导出必须写回。
                        "check_level": _label_of(column_of(role, "check_level"), rule.get("check_level")),
                        # 「关联风险」：合同格式把风险↔检查的关系**只写在检查行**上
                        # （风险表不重复维护来源，见 RISK_SOURCE_MISSING 的口径）。
                        # 已发布配置里这段关系存在风险规则的 source_rule_keys 上，
                        # 因此这里反查：哪些风险把本条规则当作来源。
                        # 不写回它，重新导入时风险会因「没有任何关联检查」被拒。
                        "risk_refs": MULTI_REF_SEPARATOR.join(
                            risk_names_by_source_key.get(rule["rule_key"], [])
                        ),
                        "applicability": _applicability_label(rule),
                        "enabled": _enabled_text(rule.get("config_status")),
                    }
                )
            continue
        if rule.get("operator") == "RISK_TRIGGER":
            for role in roles_for_node_type(node_type):
                if role not in index:
                    continue
                parameters = rule.get("parameters") if isinstance(rule.get("parameters"), dict) else {}
                rows_of(role).append(
                    {
                        "name": rule["name"],
                        "source_id": rule.get("rule_code") or "",
                        "risk_level": _label_of(column_of(role, "risk_level"), parameters.get("risk_level")),
                        # 「成立条件」是**枚举列**（任一/全部关联检查不合格），写的是
                        # `risk_condition` 的声明取值并按该列的标签表转成中文；
                        # 写成业务原文会被导入期按枚举校验直接拒（RULE_ENUM_VALUE_INVALID）。
                        "condition": _label_of(column_of(role, "condition"), rule.get("risk_condition")),
                        # 「风险说明」是自由文本列：写规则的业务说明原文。
                        "description": rule.get("description") or rule.get("requirement") or "",
                        "action": parameters.get("action") or "",
                        "enabled": _enabled_text(rule.get("config_status")),
                    }
                )

    # 字段抽取
    for role in ("FIELD_EXTRACTION",):
        if role not in index or not role_selected(role):
            continue
        value_type_column = column_of(role, "value_type")
        for field in manifest["fields"]:
            rows_of(role).append(
                {
                    "name": field["name"],
                    "source_id": field.get("source_ref") or "",
                    "material_ref": material_names[field["material_key"]],
                    "value_type": _label_of(value_type_column, field["value_type"]),
                    "unit": field.get("unit") or "",
                    "currency": field.get("currency") or "",
                    "required": "是" if field["required"] else "否",
                    # 取数期间与报表口径：计算参数按这两项定位字段事实，导出必须写回，
                    # 否则重新导入时字段缺少口径、计算配置无法发布。
                    "period_scope": _label_of(column_of(role, "period_scope"), field.get("period_scope")),
                    "statement_scope": _label_of(
                        column_of(role, "statement_scope"), field.get("statement_scope")
                    ),
                    "instruction": field.get("instruction") or "",
                    "description": field.get("instruction") or "",
                    "enabled": _enabled_text(field.get("config_status")),
                }
            )

    # 报告章节
    for role in ("REPORT_TEMPLATE",):
        if role not in index or not role_selected(role):
            continue
        for section in manifest.get("report_template", []):
            rows_of(role).append(
                {
                    "report_code": section.get("report_code") or "",
                    "report_name": section.get("report_name") or "",
                    "section_name": section["section_name"],
                    "section_order": section.get("section_order") or 1,
                    "content_type": _label_of(column_of(role, "content_type"), section.get("content_type")),
                    "field_refs": MULTI_REF_SEPARATOR.join(
                        f"{material_names[field_defs[key]['material_key']]}.{field_name(key)}"
                        for key in section.get("field_keys", [])
                    ),
                    "content_instruction": section.get("content_instruction") or "",
                    "material_refs": MULTI_REF_SEPARATOR.join(
                        material_names[key] for key in (section.get("material_keys") or [])
                    ),
                    "formats": MULTI_REF_SEPARATOR.join(section.get("formats") or []),
                    "result_sort": _label_of(column_of(role, "result_sort"), section.get("result_sort")),
                    "enabled": _enabled_text(section.get("config_status")),
                }
            )

    # 计算三表：定义、参数、判定按规则编号关联，三张一起导出。
    # 没有独立的「勾稽表」：勾稽就是一条普通计算规则（差额 + 判定区间），
    # 因此它和别的计算规则写在同一张 06_计算定义 里，不按编号前缀分流。
    for role in ("CALC_DEFINITION", "CALC_PARAMETER", "CALC_JUDGEMENT"):
        if role not in index or not role_selected(role):
            continue
        definition_rows = rows_of(role)
        for calculation in manifest.get("calculations", []):
            if role in {"CALC_PARAMETER", "CALC_JUDGEMENT"}:
                continue
            definition_rows.append(
                {
                    "rule_code": calculation.get("rule_code"),
                    "rule_name": calculation.get("rule_name"),
                    "name": calculation.get("rule_name"),
                    "formula": calculation.get("formula"),
                    # 指标类别是**枚举列**：写回声明取值并按该列标签转成中文，
                    # 写业务原文会被导入期按枚举校验直接拒（CALC_ENUM_VALUE_INVALID）。
                    "calculation_type_name": _label_of(
                        column_of(role, "calculation_type_name"), calculation.get("calculation_type")
                    ),
                    "bands_text": calculation.get("bands_text") or "",
                    "qualitative": calculation.get("verification_logic") or "",
                    "risk_level": _label_of(
                        column_of(role, "risk_level"), calculation.get("risk_level_text")
                    ),
                    "description": calculation.get("description"),
                    # 「结果期间」与「报表口径」：派生事实按它们落库，下游引用按它们定位。
                    # 导出必须写回，否则重新导入时计算结果的口径无从确定。
                    "period_scope": _label_of(
                        column_of(role, "period_scope"), calculation.get("period_policy")
                    ),
                    "statement_scope": _label_of(
                        column_of(role, "statement_scope"), calculation.get("statement_scope")
                    ),
                    "enabled": _enabled_text(calculation.get("status")),
                }
            )
        if role == "CALC_PARAMETER":
            for calculation in manifest.get("calculations", []):
                for parameter in calculation.get("parameters", []):
                    definition_rows.append(
                        {
                            "rule_code": calculation.get("rule_code"),
                            "parameter_code": parameter.get("parameter_code"),
                            "parameter_name": parameter.get("parameter_name"),
                            # 取值来源是**枚举列**：写回声明取值并按该列标签转成中文。
                            "source_type": _label_of(
                                column_of(role, "source_type"), parameter.get("source_type")
                            ),
                            "source_code": parameter.get("source_code") or "",
                            "constant_value": str(parameter.get("constant_value") or ""),
                            # 主体范围是枚举列；期间与报表口径**不在这里**——
                            # 它们由来源自身声明（字段看 03、上游结果看 06），参数表没有这两列。
                            "entity_scope": _label_of(
                                column_of(role, "entity_scope"), parameter.get("entity_scope")
                            ),
                            "required": "是" if parameter.get("required") else "否",
                            "unit": parameter.get("unit") or "",
                            "currency": parameter.get("currency") or "",
                            "remark": parameter.get("remark") or "",
                        }
                    )
        if role == "CALC_JUDGEMENT":
            for calculation in manifest.get("calculations", []):
                for band in calculation.get("bands", []):
                    definition_rows.append(
                        {
                            "rule_code": calculation.get("rule_code"),
                            "condition_code": band.get("band_code") or band.get("condition_code"),
                            "lower_value": str(band.get("lower_value") or ""),
                            "lower_inclusive": "是" if band.get("lower_inclusive") else "否",
                            "upper_value": str(band.get("upper_value") or ""),
                            "upper_inclusive": "是" if band.get("upper_inclusive") else "否",
                            "comparison_basis": _label_of(
                                column_of(role, "comparison_basis"), band.get("comparison_basis") or "RESULT"
                            ),
                            "judgement": band.get("conclusion") or band.get("judgement"),
                            "risk_level": _label_of(column_of(role, "risk_level"), band.get("risk_level")),
                            "action": band.get("action") or "",
                            "remark": band.get("remark") or "",
                        }
                    )

    internal_rows = rows_by_role.get("INTERNAL_VALIDATE", [])
    if internal_rows:
        column_keys = [column["key"] for column in definition_of("INTERNAL_VALIDATE")["columns"]]
        rows_by_code: dict[str, dict[str, Any]] = {}
        for row in internal_rows:
            code = str(row.get("source_id") or "")
            if not code:
                raise BusinessError("TEMPLATE_RULE_CODE_MISSING", "内部校验规则缺少编号，无法导出")
            previous = rows_by_code.get(code)
            if previous is None:
                rows_by_code[code] = row
                continue
            conflicts = [
                key for key in column_keys
                if key != "material_refs" and previous.get(key) != row.get(key)
            ]
            if conflicts:
                raise BusinessError(
                    "TEMPLATE_RULE_SCOPE_CONFLICT",
                    f"内部校验规则“{code}”在不同材料下的配置不一致，无法合并为同一 Excel 行",
                    details={"rule_code": code, "columns": conflicts},
                )
            materials = dict.fromkeys(
                [
                    *str(previous.get("material_refs") or "").split(MULTI_REF_SEPARATOR),
                    *str(row.get("material_refs") or "").split(MULTI_REF_SEPARATOR),
                ]
            )
            previous["material_refs"] = MULTI_REF_SEPARATOR.join(key for key in materials if key)
        rows_by_role["INTERNAL_VALIDATE"] = list(rows_by_code.values())

    for source_row in manifest.get("disabled_source_rows") or []:
        role = source_row["sheet_role"]
        if role not in selected_roles:
            raise BusinessError(
                "TEMPLATE_DISABLED_ROW_SHEET_MISSING",
                "停用来源行所属工作表不在已发布版本的 Sheet 清单中",
                details={"sheet_role": role, "source": source_row.get("source")},
            )
        known_keys = {column["key"] for column in definition_of(role)["columns"]}
        values = {column["key"]: column["value"] for column in source_row["columns"]}
        unknown = sorted(set(values) - known_keys)
        if unknown:
            raise BusinessError(
                "TEMPLATE_DISABLED_ROW_COLUMN_UNKNOWN",
                "停用来源行包含当前模板协议没有的列",
                details={"sheet_role": role, "columns": unknown},
            )
        rows_by_role.setdefault(role, []).append(values)

    workbook = Workbook()
    instruction = workbook.active
    instruction.title = unified_profile.instruction_sheet_name(profile)
    instruction.append([f"{manifest.get('scene_name', '审核模板')}（已发布配置）"])
    instruction.append(["导出说明", "本文件来自已发布版本的有效配置，已包含已生效的人工裁决。"])
    instruction.append(["使用方式", "可直接修改后作为新模板重新导入；修改不会影响已发布版本和历史任务。"])
    instruction.append(["模板协议", str(profile.get("profile_version"))])
    instruction.append(["编号与名称", str((profile.get("notes") or {}).get("code_prefix") or "")])
    instruction.append(["字段引用", str((profile.get("notes") or {}).get("field_references") or "")])
    instruction.append(["多项清单", f"多个材料、风险或报告字段可用名称或编号，并用“{LIST_SEPARATOR}”分隔；字段加总用“{SUM_OPERATOR}”连接。"])
    instruction.append(["适用条件", "填写“始终适用”或“所引用材料均已上传”，不要留空；其他适用条件当前不支持。"])
    instruction.append(
        [
            "审核要求",
            "已确认的比较方向与差额阈值会写在“审核要求”最前面（例如“字段一致”“日期不晚于”“金额绝对差不超过 100.00（元）”），"
            "原始要求附在其后；重新导入时系统按这段文字重建方向与阈值，请勿删改前面的确认参数。",
        ]
    )
    instruction.append(["未用到的表", "本文件只导出本配置实际用到的 Sheet；其余表可以留空或不上传。"])
    instruction.column_dimensions["A"].width = 20
    instruction.column_dimensions["B"].width = 92
    instruction["A1"].font = Font(size=16, bold=True, color="174A5B")

    # 先按 frozen 的 `selected_sheets` 顺序建出全部工作表（含表头与下拉），
    # 再填业务行：**已选择但当前零行的表也导出表头**——它是配置事实，
    # 不能因为此刻没有业务行就消失。写 sheet 的顺序即 `selected_roles` 的顺序。
    written = 0
    for role in selected_roles:
        definition = definition_of(role)
        sheet = workbook.create_sheet(definition.get("display_name", role))
        sheet.append([column["name"] for column in definition["columns"]])
        sheet.freeze_panes = "A2"
        for column_no, column in enumerate(definition["columns"], start=1):
            cell = sheet.cell(1, column_no)
            cell.fill = REQUIRED_FILL if column.get("required") else HEADER_FILL
            cell.font = Font(color="222222" if column.get("required") else "FFFFFF", bold=True)
            cell.alignment = Alignment(horizontal="center", vertical="center")
            sheet.column_dimensions[cell.column_letter].width = max(14, min(30, len(column["name"]) * 2 + 6))
            values = list(_labels(column).values()) or list(column.get("allowed_values") or [])
            if values:
                validation = DataValidation(type="list", formula1='"' + ",".join(values) + '"', allow_blank=True)
                sheet.add_data_validation(validation)
                validation.add(f"{cell.column_letter}2:{cell.column_letter}500")
        for row_values in rows_by_role.get(role, []):
            sheet.append([row_values.get(column["key"], "") for column in definition["columns"]])
        written += 1
    if written == 0:
        raise BusinessError(
            "EXPORT_NO_SHEET",
            "这份配置没有可导出的业务内容，未生成文件",
            status_code=409,
        )
    # 业务行落在了没有被选用的角色上：导出范围与配置范围不一致，必须报出而不是静默丢弃。
    unexpected = sorted(set(rows_by_role) - set(selected_roles))
    if unexpected:
        raise BusinessError(
            "EXPORT_ROLE_NOT_SELECTED",
            "有业务内容被写到了本版本没有选用的工作表上，导出范围与配置范围不一致："
            + "、".join(f"“{role}”" for role in unexpected),
            status_code=500,
            details={"sheet_roles": unexpected},
        )

    stream = BytesIO()
    workbook.save(stream)
    return stream.getvalue()
