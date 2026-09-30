"""按统一模板协议生成规则库模板。

系统只有一份协议（`backend/config/unified_profile.json`），它定义全部 Sheet 与列协议，
也就是**一份 base Excel 的完整表清单**。模板按当前场景明确选择的 `sheet_roles` 生成表头：
勾选哪些表就生成哪些表，未勾选的不补进来。没有业务族这一层——合同场景与财报场景
用的是同一份模板，差异只在「选了哪些表、填了什么内容」。

说明页名称由协议的 `instruction_sheet` 声明，模板生成、导出与导入读同一声明。

模板里**不包含**任何内部标识列：场景 code、workflow 版本、schema 版本、profile code、
数据库 id 都不出现在业务表里。业务维护规则编号、字段编号、名称、公式、参数引用、
阈值与说明；跨表引用可填写名称或编号，版本由系统按当前场景与导入记录自动关联。

生成的模板只写表头与说明页：没有示例行，也没有默认取值。
"""

from __future__ import annotations

from io import BytesIO
from typing import Any

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.worksheet.datavalidation import DataValidation

from ..domain import unified_profile
from ..common.exceptions import BusinessError


HEADER_FILL = PatternFill("solid", fgColor="174A5B")
REQUIRED_FILL = PatternFill("solid", fgColor="FFF0C2")
HEADER_FONT = Font(color="FFFFFF", bold=True)

# 允许在下拉里出现的取值上限。超过上限时不下拉：几千个选项的 DataValidation
# 会让 Excel 打开变慢，而且业务根本选不过来。
MAX_DROPDOWN_OPTIONS = 200


def _instruction_rows(
    profile: dict[str, Any],
    scene_name: str,
    sheets: list[dict[str, Any]],
    *,
    download_only: bool,
    scenario_schema_identity: dict[str, str] | None = None,
) -> list[list[str]]:
    """说明页内容：模板、Sheet 与列的含义、系统不做什么。

    这些说明是模板的一部分，不是可选的装饰：业务据此知道哪些列必填、
    哪些写法会被拒绝、以及系统不做哪些猜测。
    """
    notes = profile.get("notes") if isinstance(profile.get("notes"), dict) else {}
    rows: list[list[str]] = [
        [f"{scene_name}规则库模板"],
        ["协议版本", profile.get("profile_version", "")],
        ["内部协议版本", profile.get("schema_version", "")],
        [
            "场景 Schema",
            (
                f"{scenario_schema_identity.get('schema_key')}@{scenario_schema_identity.get('schema_version')}"
                if scenario_schema_identity
                else "（未指定场景）"
            ),
        ],
        [],
        ["填写方式", "名称列填写中文业务名称；跨表引用列可填写业务名称或编号，不填写数据库 id。"],
        ["必填列", "表头为黄色的列必须填写；表头为白色的列可以留空。"],
        ["未知列", "模板里没有的列会被导入明确拒绝，不会被静默忽略；请不要自行增列。"],
        ["列顺序", "列顺序可以调整，系统按列名识别含义，不按位置。"],
        [
            "模板范围",
            "本次勾选只用于生成这一份文件，不会写入场景绑定，也不等于最终绑定。上传后仍要确认每张 Sheet 的角色和节点。"
            if download_only
            else "本文件只包含当前场景已经确认绑定的工作表。",
        ],
        ["空白字段", "表头必须存在，但当前业务行不适用的字段允许留空；系统不为空白字段补默认业务含义。"],
        [],
        ["系统不做什么", "不做单位换算、不做汇率换算、不提供任何默认值；缺少业务口径时明确报错，不代填。"],
    ]
    for key, label in (
        ("scope", "统一协议范围"),
        ("binding", "Sheet 绑定"),
        ("unknown_columns", "未知列"),
        ("code_prefix", "编号与名称"),
        ("field_references", "字段引用"),
        ("enabled_column", "是否生效"),
        ("rule_code", "规则编号"),
        ("calculation", "计算规则"),
    ):
        if notes.get(key):
            rows.append([label, notes[key]])
    rows.append([])
    rows.append(["Sheet", "可绑定的节点类型", "列（黄色为必填）", "说明"])
    for sheet in sheets:
        required = [column["name"] for column in sheet["columns"] if column.get("required")]
        rows.append(
            [
                sheet.get("display_name", sheet["sheet_role"]),
                "、".join(sheet.get("node_types", [])),
                "、".join(required),
                "；".join(
                    f"{column['name']}："
                    + ("必填" if column.get("required") else "可选")
                    + (
                        "（" + " / ".join(str(item) for item in column.get("allowed_values", [])) + "）"
                        if column.get("allowed_values")
                        else ""
                    )
                    for column in sheet["columns"]
                ),
            ]
        )
    return rows


def build_scene_template(
    profile: dict[str, Any],
    *,
    scene_name: str,
    sheet_roles: list[str],
    download_only: bool = False,
    scenario_schema_identity: dict[str, str] | None = None,
) -> bytes:
    """按统一模板协议生成模板。

    `sheet_roles` 是当前场景明确选用的 Sheet。调用方必须提供，生成器不存在
    “缺少场景上下文就导出协议全集”的回退路径。
    """
    if not isinstance(profile, dict) or not profile.get("sheets"):
        raise BusinessError(
            "TEMPLATE_SCENE_PROFILE_INVALID",
            "统一模板协议没有可用的 Sheet 定义，无法生成模板",
            status_code=500,
        )
    selected = set(sheet_roles)
    sheets = [
        sheet
        for sheet in profile["sheets"]
        if sheet["sheet_role"] in selected
    ]
    if not sheets:
        raise BusinessError(
            "TEMPLATE_NO_SHEET",
            "当前场景没有选用任何 Sheet，无法生成模板",
            details={"scene_name": scene_name},
        )
    workbook = Workbook()
    workbook.remove(workbook.active)

    instruction = workbook.create_sheet(unified_profile.instruction_sheet_name(profile))
    instruction.column_dimensions["A"].width = 22
    instruction.column_dimensions["B"].width = 22
    instruction.column_dimensions["C"].width = 48
    instruction.column_dimensions["D"].width = 90
    for row in _instruction_rows(
        profile,
        scene_name,
        sheets,
        download_only=download_only,
        scenario_schema_identity=scenario_schema_identity,
    ):
        instruction.append(row)
    for cell in instruction["A"]:
        cell.font = Font(bold=True)

    for sheet in sheets:
        worksheet = workbook.create_sheet(sheet.get("display_name", sheet["sheet_role"]))
        columns = sheet["columns"]
        worksheet.append([column["name"] for column in columns])
        for index, column in enumerate(columns, start=1):
            cell = worksheet.cell(row=1, column=index)
            cell.fill = REQUIRED_FILL if column.get("required") else HEADER_FILL
            cell.font = Font(bold=True) if column.get("required") else HEADER_FONT
            cell.alignment = Alignment(vertical="center", wrap_text=True)
            worksheet.column_dimensions[cell.column_letter].width = max(
                12, min(28, len(column["name"]) * 2 + 6)
            )
            # 下拉里放**中文标签**（`value_labels` 的取值），不是内部编码：
            # 业务在模板里看到并选择的是中文，导入端按同一份声明把标签映射回内部取值。
            # 没有声明标签的枚举列直接放允许取值（它们本身就是业务写法）。
            labels = list((column.get("value_labels") or {}).values())
            allowed = [str(item) for item in (labels or column.get("allowed_values") or []) if str(item).strip()]
            if allowed and len(allowed) <= MAX_DROPDOWN_OPTIONS:
                validation = DataValidation(
                    type="list",
                    formula1='"' + ",".join(allowed) + '"',
                    allow_blank=not column.get("required"),
                    showDropDown=False,
                )
                worksheet.add_data_validation(validation)
                validation.add(
                    f"{cell.column_letter}2:{cell.column_letter}2000"
                )
        worksheet.freeze_panes = "A2"
    stream = BytesIO()
    workbook.save(stream)
    return stream.getvalue()
