"""把节点版（contract_review_v3）的已发布配置导出成同一格式的七张业务表。

节点版的列与 v2 完全不同（编号列、配置状态、检查项目/合格要求、允许差值、章节顺序、
结果排序等）。导出必须写出该格式自己的列，不能拿 v2 列硬套，也不能把新格式静默降级成
v2 后再声称“已导出”。

导出内容来自已生效的 manifest（包含已确认的裁决与风险组合），因此可以按同一 profile
读回等价语义；旧格式的 manifest 不走这里，旧任务也不会被这次导出改写。
"""

from __future__ import annotations

from io import BytesIO
from typing import Any

from openpyxl import Workbook
from openpyxl.worksheet.datavalidation import DataValidation

from ..errors import BusinessError
from ..domain.profile import load_rulebook_profile


NODE_PROFILE_VERSION = "contract_review_v3"
# 节点版包含 v3 与 v4：两者用同一套七表列导出，v4 多一列「材料识别类型」。
NODE_PROFILE_VERSIONS = frozenset({"contract_review_v3", "contract_review_v4"})

LIST_SEPARATOR = "；"

CONFIG_STATUS_LABELS = {
    "CLEAR": "已明确",
    "PENDING": "待确认",
    "DISABLED": "停用",
}
CONTENT_TYPE_LABELS = {
    "FIELD_DETAIL": "字段明细",
    "AUDIT_RESULT": "审核结果",
    "FIELD_AND_RESULT": "字段及审核结果",
    "RISK_LIST": "风险清单",
    "CONCLUSION": "审核结论",
}
RESULT_SORT_LABELS = {
    "RISK_LEVEL_DESC": "风险等级降序",
    "MATERIAL_ORDER": "材料顺序",
    "MATERIAL_ORDER_RISK_LEVEL_DESC": "材料顺序；风险等级降序",
}


def _labels(column: dict[str, Any]) -> dict[str, str]:
    """契约取值 → 业务标签。

    既有 profile 里 `value_labels` 的书写方向并不统一（v2 是「契约值 → 标签」，
    v3 的等级列是「标签 → 契约值」）。这里按同一套方向判定还原成「契约值 → 标签」，
    否则 v3 的等级与产物来源会导出成内部契约码，业务在 Excel 里看到的是
    自己从没填过的取值。
    """
    allowed = [str(item).strip() for item in (column.get("allowed_values") or []) if str(item).strip()]
    allowed_set = set(allowed)
    labels: dict[str, str] = {item: item for item in allowed}
    for canonical, label in (column.get("value_labels") or {}).items():
        canonical_text = str(canonical).strip()
        label_text = str(label).strip()
        if not label_text:
            continue
        if canonical_text in allowed_set:
            # 「契约值 → 标签」：标签即显示文本。
            labels[canonical_text] = label_text
        elif label_text in allowed_set:
            # 「标签 → 契约值」：键是显示文本，值是契约取值。
            labels[label_text] = canonical_text
    return labels


def _merge_name_list(left: str | None, right: str | None) -> str:
    """合并两个以中文分号分隔的名称清单，去重并保持原顺序。"""
    items: list[str] = []
    for value in (left, right):
        for item in str(value or "").split(LIST_SEPARATOR):
            name = item.strip()
            if name and name not in items:
                items.append(name)
    return LIST_SEPARATOR.join(items)


def _label_of(column: dict[str, Any] | None, value: Any) -> str:
    if value in (None, ""):
        return ""
    if column is None:
        return str(value)
    return _labels(column).get(str(value).strip(), str(value).strip())


def _config_status(value: Any) -> str:
    return CONFIG_STATUS_LABELS.get(str(value or ""), "已明确")


def _manifest_profile(manifest: dict[str, Any]) -> dict[str, Any]:
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


def build_node_rulebook_export(manifest: dict[str, Any]) -> bytes:
    profile = _manifest_profile(manifest)
    if profile.get("profile_version") not in NODE_PROFILE_VERSIONS:
        raise BusinessError(
            "EXPORT_PROFILE_UNSUPPORTED",
            "该版本不是节点版规则库格式，不能用节点版列导出",
            status_code=500,
            details={"profile_version": profile.get("profile_version")},
        )
    roles = {item["role"]: item for item in profile["sheet_roles"]}
    material_names = {item["material_key"]: item["name"] for item in manifest["materials"]}
    field_defs = {item["field_key"]: item for item in manifest["fields"]}

    def column_of(role: str, key: str) -> dict[str, Any]:
        for column in roles[role]["columns"]:
            if column["key"] == key:
                return column
        raise BusinessError(
            "PROFILE_COLUMN_MISSING",
            "模板配置缺少业务列定义",
            status_code=500,
            details={"role": role, "column": key},
        )

    def field_reference(field_key: str) -> str:
        definition = field_defs.get(field_key)
        if not definition:
            return ""
        material_name = material_names.get(definition["material_key"], "")
        return f"{material_name}.{definition['name']}" if material_name else definition["name"]

    def material_list(material_keys: list[str]) -> str:
        return LIST_SEPARATOR.join(name for key in material_keys if (name := material_names.get(key)))

    def require_source_id(value: Any, *, kind: str, name: str) -> str:
        """编号列是导入时的必填项：导出写出空编号会产生一份无法回导的文件。

        页面新建的对象确实没有来源编号，这时必须明确报错并说明缺哪些对象，
        而不是静默写出空列、让业务在“导入失败”时才发现。
        """
        text = str(value or "").strip()
        if not text:
            raise BusinessError(
                "EXPORT_SOURCE_ID_MISSING",
                f"{kind}“{name}”缺少业务编号，导出的规则库会缺少必填编号、无法重新导入；"
                "请先在页面上补全编号（或从带编号的规则库导入）后重新导出",
                status_code=409,
                details={"kind": kind, "name": name},
            )
        return text

    business_rules = [
        rule for rule in manifest["rules"] if rule.get("operator") not in {"LLM_EXTRACT", "REPORT_RENDER"}
    ]
    # 风险按来源检查反向组织：关联风险只在检查行维护，风险表不重复录入来源。
    risks_by_rule: dict[str, list[dict[str, Any]]] = {}
    for rule in business_rules:
        if rule.get("node") != "RISK_RATING":
            continue
        source_keys = list(rule.get("source_rule_keys") or [])
        if not source_keys and rule.get("source_rule_key"):
            source_keys = [rule["source_rule_key"]]
        for source_key in source_keys:
            risks_by_rule.setdefault(source_key, []).append(rule)

    def risk_names_of(rule_key: str) -> str:
        return LIST_SEPARATOR.join(item["name"] for item in risks_by_rule.get(rule_key, []))

    rows_by_role: dict[str, list[list[Any]]] = {role: [] for role in roles}

    # 01_材料检查
    for material in manifest["materials"]:
        rows_by_role["MATERIAL_LIST"].append(
            {
                "source_id": require_source_id(material.get("source_ref"), kind="材料", name=material["name"]),
                "name": material["name"],
                "required": "是" if material["required"] else "否",
                "config_status": _config_status(material.get("config_status")),
                "remark": material.get("description") or "",
            }
        )

    # 02_文件处理
    for rule in business_rules:
        if rule.get("operator") != "FILE_QUALITY_CHECK":
            continue
        parameters = rule.get("parameters") if isinstance(rule.get("parameters"), dict) else {}
        rows_by_role["MATERIAL_QUALITY"].append(
            {
                "source_id": require_source_id(rule.get("source_ref"), kind="规则", name=rule["name"]),
                "name": rule["name"],
                "material_refs": material_list(rule.get("materials") or []),
                "check_item": _label_of(column_of("MATERIAL_QUALITY", "check_item"), parameters.get("check_item")),
                "requirement": _label_of(column_of("MATERIAL_QUALITY", "requirement"), parameters.get("requirement")),
                "criterion": parameters.get("criterion") or "",
                "standard_value": parameters.get("standard_value") or "",
                "unit": parameters.get("unit") or "",
                "check_level": rule.get("check_level") or "",
                "risk_refs": risk_names_of(rule["rule_key"]),
                "config_status": _config_status(rule.get("config_status")),
                "remark": rule.get("requirement") or "",
            }
        )

    # 03_字段抽取
    for field in manifest["fields"]:
        rows_by_role["FIELD_EXTRACTION"].append(
            {
                "source_id": require_source_id(field.get("source_ref"), kind="字段", name=field["name"]),
                "material_ref": material_names.get(field["material_key"], ""),
                "name": field["name"],
                "value_type": _label_of(column_of("FIELD_EXTRACTION", "value_type"), field["value_type"]),
                "unit": field.get("unit") or "",
                "currency": field.get("currency") or "",
                "required": "是" if field["required"] else "否",
                # 产物来源：主体核对据此确定字段取自哪种产物；留空表示常规字段抽取。
                # 导出按 profile 的标签写出（「印章识别」），与模板下拉一致；
                # 写出内部契约码 SEAL 会让业务在 Excel 里看到一个不认识的取值。
                "artifact_source": _label_of(
                    column_of("FIELD_EXTRACTION", "artifact_source"), field.get("artifact_source")
                ),
                "instruction": field.get("instruction") or "",
                "config_status": _config_status(field.get("config_status")),
                "remark": "",
            }
        )

    # 04 / 05 校验规则
    #
    # 多材料内部检查在编译期按材料展开成多条规则，但它们共用同一个业务编号：
    # 一行规则覆盖多种材料是**同一行业务记录**。导出必须还原成一行（材料范围合并），
    # 否则同一编号会被写成多行重复编号，导出文件再导入时会被编号唯一性检查拒绝。
    for role in ("INTERNAL_VALIDATE", "CROSS_VALIDATE"):
        operator_column = column_of(role, "operator")
        grouped: dict[str, dict[str, Any]] = {}
        for rule in business_rules:
            if rule.get("node") != role:
                continue
            inputs = rule.get("inputs") or []
            parameters = rule.get("parameters") if isinstance(rule.get("parameters"), dict) else {}
            # 已确认的业务口径优先：check_operator 是业务填写的「合格要求」原文取值。
            operator_label = _label_of(operator_column, rule.get("check_operator"))
            left_name = field_defs.get(inputs[0]["field_key"], {}).get("name", "") if inputs else ""
            right_names = " + ".join(
                field_defs.get(item["field_key"], {}).get("name", "") for item in inputs[1:]
            )
            source_id = require_source_id(rule.get("source_ref"), kind="规则", name=rule["name"])
            row: dict[str, Any] = {
                "source_id": source_id,
                "name": rule["name"],
                "left_field_ref": left_name,
                "operator": operator_label,
                "right_field_ref": right_names,
                "threshold": parameters.get("threshold", "") if "threshold" in parameters else "",
                "threshold_unit": parameters.get("threshold_unit") or "",
                "check_level": rule.get("check_level") or "",
                "risk_refs": risk_names_of(rule["rule_key"]),
                "config_status": _config_status(rule.get("config_status")),
                "remark": rule.get("requirement") or "",
            }
            if role == "CROSS_VALIDATE":
                right_material = inputs[1]["material_key"] if len(inputs) > 1 else None
                # 交叉校验的列顺序是「材料、检查字段、合格要求、对照材料、对照内容…」，
                # 对照材料必须写在对照内容之前。
                row["material_ref"] = material_names.get(inputs[0]["material_key"], "") if inputs else ""
                row["right_material_ref"] = material_names.get(right_material, "") if right_material else ""
            else:
                row["material_refs"] = material_list(rule.get("materials") or [])
            existing = grouped.get(source_id) if source_id else None
            if existing is None:
                if source_id:
                    grouped[source_id] = row
                rows_by_role[role].append(row)
                continue
            # 同一编号的其余展开行：合并材料范围，其余业务列必须一致（不一致属于配置冲突）。
            for key in ("name", "left_field_ref", "operator", "right_field_ref", "threshold", "threshold_unit", "check_level", "remark"):
                if existing.get(key) != row.get(key):
                    raise BusinessError(
                        "EXPORT_RULE_GROUP_CONFLICT",
                        f"规则编号 {source_id} 的多条展开记录在「{key}」上不一致，无法还原成一行业务记录",
                        status_code=500,
                        details={"source_id": source_id, "column": key},
                    )
            if role == "CROSS_VALIDATE":
                # 交叉校验一行就是一对材料；同一编号出现多条展开行说明配置本身有冲突。
                raise BusinessError(
                    "EXPORT_RULE_GROUP_CONFLICT",
                    f"交叉校验规则编号 {source_id} 对应多条展开记录，无法还原成一行业务记录",
                    status_code=500,
                    details={"source_id": source_id},
                )
            existing["material_refs"] = _merge_name_list(existing.get("material_refs"), row.get("material_refs"))
            existing["risk_refs"] = _merge_name_list(existing.get("risk_refs"), row.get("risk_refs"))

    # 06_风险评定
    for rule in business_rules:
        if rule.get("node") != "RISK_RATING":
            continue
        parameters = rule.get("parameters") if isinstance(rule.get("parameters"), dict) else {}
        rows_by_role["RISK"].append(
            {
                "source_id": require_source_id(rule.get("source_ref"), kind="风险", name=rule["name"]),
                "name": rule["name"],
                "risk_level": rule.get("risk_level_value") or parameters.get("risk_level") or "",
                "condition": _label_of(column_of("RISK", "condition"), rule.get("risk_condition")),
                "description": rule.get("requirement") or "",
                "action": parameters.get("action") or "",
                "config_status": _config_status(rule.get("config_status")),
                "remark": "",
            }
        )

    # 07_报告输出
    report_groups = manifest.get("report_groups") or {}
    if isinstance(report_groups, dict) and report_groups.get("source_kind") == "REPORT_CODES":
        for group in report_groups.get("reports") or []:
            for section in group.get("sections") or []:
                rows_by_role["REPORT_TEMPLATE"].append(
                    {
                        "report_code": group.get("report_code") or "",
                        "report_name": group.get("report_name") or "",
                        "material_refs": material_list(group.get("material_keys") or []),
                        "formats": LIST_SEPARATOR.join(group.get("formats") or []),
                        "section_order": section.get("section_order"),
                        "section_name": section.get("section_name") or "",
                        "content_type": CONTENT_TYPE_LABELS.get(section.get("content_type"), ""),
                        "field_refs": LIST_SEPARATOR.join(
                            reference
                            for key in section.get("field_keys") or []
                            if (reference := field_reference(key))
                        ),
                        "content_instruction": section.get("content_instruction") or "",
                        "result_sort": RESULT_SORT_LABELS.get(group.get("result_sort"), ""),
                        "config_status": CONFIG_STATUS_LABELS.get(
                            str(section.get("config_status") or ""), "已明确"
                        ),
                        "remark": "",
                    }
                )

    workbook = Workbook()
    for role_no, role in enumerate(roles):
        definition = roles[role]
        if role_no == 0:
            sheet = workbook.active
            sheet.title = definition["display_name"]
        else:
            sheet = workbook.create_sheet(definition["display_name"])
        # 普通业务表：表头 + 数据行，只保留必要的下拉数据验证。
        # 不加填充、字体、冻结窗格一类的装饰，也不添加辅助或隐藏 Sheet：
        # 导出文件是给业务继续维护的规则库，不是展示稿。
        sheet.append([column["name"] for column in definition["columns"]])
        for column_no, column in enumerate(definition["columns"], start=1):
            values = list(_labels(column).values()) or list(column.get("allowed_values") or [])
            if not values:
                continue
            letter = sheet.cell(1, column_no).column_letter
            validation = DataValidation(type="list", formula1='"' + ",".join(values) + '"', allow_blank=True)
            sheet.add_data_validation(validation)
            validation.add(f"{letter}2:{letter}500")
        for row_values in rows_by_role.get(role, []):
            # 按列 key 取值再落盘：列顺序变化或增删列都不会让内容错位。
            sheet.append([row_values.get(column["key"], "") for column in definition["columns"]])

    stream = BytesIO()
    workbook.save(stream)
    return stream.getvalue()
