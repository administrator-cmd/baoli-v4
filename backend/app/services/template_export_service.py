"""把已发布模板版本导出成规则 Excel。

导出的是**已生效配置**，来源是发布时固化、之后再不可修改的 manifest，
不是最初上传的原始文件：用户此前的裁决（改过的字段类型、材料引用、说明等）
都已经体现在 manifest 里。

导出内容仍使用 v2 的中文业务列，因此可以直接改一改再作为新模板重新导入。
"""

from io import BytesIO
from typing import Any

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.worksheet.datavalidation import DataValidation

from ..domain.manifest import DATE_COMPARISON_LABELS, DECIMAL_COMPARISON_LABELS
from ..errors import BusinessError
from ..domain.profile import load_rulebook_profile


HEADER_FILL = PatternFill("solid", fgColor="174A5B")
REQUIRED_FILL = PatternFill("solid", fgColor="FFF0C2")

# 同一格里多个引用使用的分隔符：与导入端允许「展示字段」写多个引用的口径一致。
MULTI_REF_SEPARATOR = ", "


def _labels(column: dict[str, Any]) -> dict[str, str]:
    return {canonical: label for canonical, label in (column.get("value_labels") or {}).items()}


def _label_of(column: dict[str, Any], value: Any) -> str:
    if value is None:
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
        # 字段比较的方向完全由「检查方式」列确定，这里用同一口径复述，便于人工核对。
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
        # 检查要求是已发布配置的一部分，不能因为它落在别的列就丢掉。
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
        # 产物类型决定这条质量检查实际查什么，重新导入时必须能确定，不能被丢掉。
        texts.append(f"处理产物类型：{artifact_type}")
    return "\n".join(texts)


def _manifest_profile(manifest: dict[str, Any]) -> dict[str, Any]:
    """导出必须使用 manifest 自己的格式版本，不能回落到默认版本。"""
    version = manifest.get("profile_version")
    if not isinstance(version, str) or not version:
        raise BusinessError(
            "EXPORT_PROFILE_UNAVAILABLE",
            "导出的审核模板缺少格式版本，无法确定业务列定义",
            status_code=500,
            details={"profile_version": version},
        )
    try:
        return load_rulebook_profile(version)
    except BusinessError as exc:
        raise BusinessError(
            "EXPORT_PROFILE_UNAVAILABLE",
            f"导出所需的模板格式版本“{version}”当前不可加载",
            status_code=500,
            details={"profile_version": version, "reason": exc.code},
        ) from exc


def build_scenario_version_export(manifest: dict[str, Any]) -> bytes:
    profile = _manifest_profile(manifest)
    roles = {item["role"]: item for item in profile["sheet_roles"]}
    material_names = {item["material_key"]: item["name"] for item in manifest["materials"]}
    field_defs = {item["field_key"]: item for item in manifest["fields"]}
    rule_names = {item["rule_key"]: item["name"] for item in manifest["rules"]}

    def definition_of(role: str) -> dict[str, Any]:
        definition = roles.get(role)
        if definition is None:
            raise BusinessError(
                "PROFILE_ROLE_MISSING", "模板配置缺少工作表定义", status_code=500, details={"role": role}
            )
        return definition

    def column_of(role: str, key: str) -> dict[str, Any]:
        for column in definition_of(role)["columns"]:
            if column["key"] == key:
                return column
        raise BusinessError(
            "PROFILE_COLUMN_MISSING",
            "模板配置缺少业务列定义",
            status_code=500,
            details={"role": role, "column": key},
        )

    def field_ref(field_key: str | None) -> str:
        definition = field_defs.get(field_key or "")
        if not definition:
            return ""
        material_name = material_names.get(definition["material_key"], "")
        return f"{material_name}.{definition['name']}" if material_name else definition['name']

    def joined_material_names(inputs: list[dict[str, Any]]) -> str:
        """一格多个引用时按统一分隔符写出全部材料，不截断成第一个。"""
        return MULTI_REF_SEPARATOR.join(
            name for item in inputs if (name := material_names.get(item.get("material_key")))
        )

    def joined_field_refs(inputs: list[dict[str, Any]]) -> str:
        return MULTI_REF_SEPARATOR.join(
            ref for item in inputs if (ref := field_ref(item.get("field_key")))
        )

    # 行内容按「列 key → 单元格文本」组织，落盘顺序完全由 profile 的 sheet_roles 决定；
    # profile 没有声明的列在文件里没有位置，这里不会为它编造内容。
    rows_by_role: dict[str, list[dict[str, Any]]] = {role: [] for role in roles}

    def rows_of(role: str) -> list[dict[str, Any]]:
        """把行挂到 profile 声明的工作表上；profile 没有该角色说明这份配置本身不完整。"""
        definition_of(role)
        return rows_by_role[role]

    # 01 材料清单
    for material in manifest["materials"]:
        rows_of("MATERIAL_LIST").append(
            {
                "name": material["name"],
                "material_type": material["material_type"],
                "required": "是" if material["required"] else "否",
                "description": material.get("description") or "",
            }
        )

    # 02 材料质量
    quality_operator_column = column_of("MATERIAL_QUALITY", "operator")
    for rule in manifest["rules"]:
        if rule["node"] not in {"MATERIAL_CHECK", "DOCUMENT_PREPARE"}:
            continue
        if rule.get("operator") not in {"MATERIAL_EXISTS", "FILE_QUALITY_CHECK"}:
            continue
        # 规则的材料范围可能有多份材料：全部导出，不能只取第 0 个而静默丢掉其余材料。
        scope_names = [
            name for material_key in (rule.get("materials") or []) if (name := material_names.get(material_key))
        ]
        rows_of("MATERIAL_QUALITY").append(
            {
                "name": rule["name"],
                "material_ref": MULTI_REF_SEPARATOR.join(scope_names),
                "operator": _label_of(quality_operator_column, rule.get("operator")),
                "parameters": _quality_requirement_text(rule),
            }
        )

    # 03 字段抽取
    value_type_column = column_of("FIELD_EXTRACTION", "value_type")
    for field in manifest["fields"]:
        rows_of("FIELD_EXTRACTION").append(
            {
                "name": field["name"],
                "material_ref": material_names.get(field["material_key"], ""),
                "value_type": _label_of(value_type_column, field["value_type"]),
                "unit": field.get("unit") or "",
                "required": "是" if field["required"] else "否",
                "instruction": field.get("instruction") or "",
            }
        )

    # 04 / 05 校验规则
    for role in ("INTERNAL_VALIDATE", "CROSS_VALIDATE"):
        operator_column = column_of(role, "operator")
        for rule in manifest["rules"]:
            if rule["node"] != role:
                continue
            inputs = rule.get("inputs", [])
            # manifest 已保证比较双方单位一致，取任一已确认单位即可，系统不做单位换算。
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
                    "left_material_ref": joined_material_names(inputs[:1]),
                    "left_field_ref": joined_field_refs(inputs[:1]),
                    "right_material_ref": joined_material_names(inputs[1:]),
                    "right_field_ref": joined_field_refs(inputs[1:]),
                    "operator": _label_of(operator_column, rule.get("operator")),
                    "parameters": _requirement_text(
                        rule, unit, _label_of(operator_column, rule.get("operator"))
                    ),
                    "applicability": _applicability_label(rule),
                }
            )

    # 06 风险疑点
    # 「来源节点」是 profile 里的业务列：manifest 规则里的 source_node 存契约值，
    # 这里用该列的 value_labels 反查中文标签。旧 manifest 的规则没有 source_node
    # （该列出现之前发布的版本），此时该单元格留空 —— 不编造默认节点：猜一个来源节点
    # 会让这条风险看起来已经配置完整，而实际来源根本没确定，业务必须自己确认。
    risk_level_column = column_of("RISK", "risk_level")
    risk_source_node_column = next(
        (column for column in definition_of("RISK")["columns"] if column["key"] == "source_node"), None
    )
    for rule in manifest["rules"]:
        if rule["node"] != "RISK_RATING":
            continue
        parameters = rule.get("parameters") if isinstance(rule.get("parameters"), dict) else {}
        rows_of("RISK").append(
            {
                "name": rule["name"],
                "source_node": _label_of(risk_source_node_column, rule.get("source_node"))
                if risk_source_node_column
                else "",
                "source_rule_ref": rule_names.get(rule.get("source_rule_key") or "", ""),
                "risk_level": _label_of(risk_level_column, parameters.get("risk_level")),
                "action": parameters.get("action") or "",
            }
        )

    # 07 报告模板（只导出附加展示章节与内容要求）
    for section in manifest.get("report_template", []):
        refs = ", ".join(field_ref(key) for key in section.get("field_keys", []))
        rows_of("REPORT_TEMPLATE").append(
            {
                "section_name": section["section_name"],
                "field_refs": refs,
                "content_instruction": section.get("content_instruction") or "",
            }
        )

    workbook = Workbook()
    instruction_sheet_name = profile.get("instruction_sheet")
    if instruction_sheet_name:
        instruction = workbook.active
        instruction.title = instruction_sheet_name
        instruction.append([f"{manifest.get('scene_name', '审核模板')}（模板版本 V{manifest.get('manifest_version', '')}）"])
        instruction.append(["导出说明", "本文件来自已发布版本的有效配置，已包含已生效的人工裁决。"])
        instruction.append(["使用方式", "可直接修改后作为新模板重新导入；修改不会影响已发布版本和历史任务。"])
        # 说明页必须写出本次导出使用的目标格式版本，方便人工核对列定义与「审核要求」的写法。
        instruction.append(["模板格式版本", profile["profile_version"]])
        instruction.append(["适用条件", "填写“始终适用”或“所引用材料均已上传”，不要留空；其他适用条件当前不支持。"])
        instruction.append(
            [
                "审核要求",
                "已确认的比较方向与差额阈值会写在“审核要求”最前面（例如“字段一致”“日期不晚于”“金额绝对差不超过 100.00（元）”），"
                "原始要求附在其后；重新导入时系统按这段文字重建方向与阈值，请勿删改前面的确认参数。",
            ]
        )
        instruction.append(["风险触发", "风险疑点固定为“来源审核规则异常时触发”；证据不足不等于未触发风险。"])
        instruction.column_dimensions["A"].width = 20
        instruction.column_dimensions["B"].width = 92
        instruction["A1"].font = Font(size=16, bold=True, color="174A5B")

    for role_no, role in enumerate(profile["sheet_roles"]):
        definition = definition_of(role["role"])
        if role_no == 0 and not instruction_sheet_name:
            # 没有说明页时复用新建工作簿自带的工作表，避免留下空 Sheet。
            sheet = workbook.active
            sheet.title = definition["display_name"]
        else:
            sheet = workbook.create_sheet(definition["display_name"])
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
        for row_values in rows_by_role.get(role["role"], []):
            sheet.append([row_values.get(column["key"], "") for column in definition["columns"]])

    stream = BytesIO()
    workbook.save(stream)
    return stream.getvalue()
