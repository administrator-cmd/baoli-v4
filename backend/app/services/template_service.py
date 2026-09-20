from io import BytesIO
from typing import Any

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.worksheet.datavalidation import DataValidation

from ..config import get_settings
from ..errors import BusinessError
from ..domain.profile import load_rulebook_profile


HEADER_FILL = PatternFill("solid", fgColor="174A5B")
REQUIRED_FILL = PatternFill("solid", fgColor="FFF0C2")
HEADER_FONT = Font(color="FFFFFF", bold=True)

# 只有这两个取值代表"空白模板"；其余取值必须命中 profile 里真实存在的示例。
BLANK_EXAMPLE_KEYS = {None, "", "blank"}


def available_examples(profile: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """当前格式版本**真实存在**的模板示例。

    只有 profile 里声明了对应 examples 内容时才列出。节点版 v3/v4 没有真实示例
    （profile 无 examples 键），因此只提供 blank：让页面继续展示必然下载失败的
    示例 A/B，用户点下去只会拿到一个报错。
    """
    selected = profile if profile is not None else load_rulebook_profile()
    examples = selected.get("examples")
    items: list[dict[str, Any]] = [
        {"key": "blank", "label": "空白模板", "available": True, "description": "只含表头，不含任何示例行"}
    ]
    if isinstance(examples, dict):
        for key in sorted(examples):
            content = examples[key]
            if not isinstance(content, dict) or not isinstance(content.get("sheets"), dict):
                continue
            items.append(
                {
                    "key": key,
                    "label": content.get("display_name") or key,
                    "available": True,
                    "description": content.get("description", ""),
                }
            )
    return items


def _example_content(profile: dict[str, Any], example: str) -> dict[str, Any]:
    examples = profile.get("examples")
    if not isinstance(examples, dict) or example not in examples:
        raise BusinessError(
            "TEMPLATE_EXAMPLE_UNKNOWN",
            "该模板格式没有这个示例，请使用空白模板或该格式真实提供的示例",
            details={
                "example": example,
                "supported": [item["key"] for item in available_examples(profile)],
            },
        )
    content = examples[example]
    if not isinstance(content, dict) or not isinstance(content.get("sheets"), dict):
        raise BusinessError(
            "PROFILE_INVALID",
            "合同审核模板配置中的示例内容不完整",
            status_code=500,
            details={"example": example},
        )
    return content


def _instruction_rows(profile: dict[str, Any], example: dict[str, Any] | None) -> list[list[str]]:
    """说明页内容。

    适用条件、风险触发方式、未支持的能力和读取范围必须写在模板说明里，
    不能让业务用户从示例行里猜。
    """
    settings = get_settings()
    max_megabytes = settings.max_rulebook_bytes / (1024 * 1024)
    rows: list[list[str]] = [
        [profile["display_name"]],
        ["模板格式版本", profile["profile_version"]],
    ]
    if example is not None:
        rows.extend(
            [
                ["示例内容", f"本文件包含「{example['display_name']}」的示例内容：{example.get('description', '')}"],
                ["示例可删除", "示例行只是演示业务，可以直接删除；删除后系统不会自动补回，也不代表系统会补出任何隐含规则。"],
                ["示例限制", "示例的风险等级、金额要求和字段只是示例内容，不是平台默认规则。"],
            ]
        )
    else:
        rows.append(["空白模板", "本文件只有表头与说明，不含任何示例行；请按下面的说明填写业务规则。"])
    rows.extend(
        [
            ["填写方式", "业务列使用中文名称和下拉选项填写，不需要填写内部编码、英文算子、JSON 或 SQL。"],
            ["必填列", "表头为黄色的列必须填写；其余列按需填写。表头为白色的列可以留空。"],
            [
                "最少填写",
                "至少需要“材料清单”和“字段抽取”。固定事实报告由系统生成，“报告模板”是可选的附加章节，"
                "需要附加展示时才填写，且章节引用的字段必须已定义。"
                "文件内部校验、文件交叉校验、风险疑点、报告模板都可以按需留空；留空的检查在审核任务里会显式显示为“本模板未配置此类检查”，不等于检查通过。",
            ],
            [
                "材料位",
                "每个材料位在审核任务里只有一个当前文件位：再次上传同一材料位是替换该材料位的文件版本，不会追加成第二份同类材料。",
            ],
            [
                "引用方式",
                "材料引用填写材料清单中的材料名称；字段引用填写字段名称，并同时填写所属材料，避免同名字段混淆。名称重复或含分隔符时必须补全材料范围，系统无法唯一确定时会直接报错，不会猜。",
            ],
            [
                "适用条件",
                "留空表示“始终适用”；填写“所引用材料均已上传”表示仅当该规则引用的材料都已上传时才适用。其他适用条件当前不支持。",
            ],
            [
                "风险触发",
                "风险疑点固定为“来源审核规则异常时触发”，没有触发条件列；证据不足不等于未触发风险，风险结果会保留未能判断的状态。",
            ],
            [
                "日期/数值比较",
                "日期方向、金额比较方式与允许差额必须写在“审核要求”里，系统不会自动采用默认阈值；缺少必要参数时会作为业务问题要求补充，不会猜一个值。",
            ],
            [
                "条款审核",
                "“条款审核”可以只填写左侧材料与左侧字段，右侧留空表示按“审核要求”对左侧条款做语义判断。",
            ],
            [
                "不支持的能力",
                "全部发票求和、多份文件合计、跨期间汇总、多合同自动分组等汇总取数当前不支持，字段与固定常量比较也不支持。请改为两个明确字段之间的比较；系统不会取第一份或最后一份文件代替，也不会让大模型估算。",
            ],
            [
                "读取范围",
                f"单个文件最大 {max_megabytes:g} MB（取自本系统上传策略，可配置）；"
                f"每个工作表最多读取前 {settings.workbook_preview_rows} 行、"
                f"前 {settings.workbook_preview_columns} 列，每格最多 {settings.workbook_cell_chars} 字。"
                "该行数与列数只限制**页面预览摘要**；业务转换读取的是完整业务行，"
                "不按此范围截断，也不代表整份文件已完整转换。",
            ],
        ]
    )
    return rows


def build_contract_review_template(example: str | None = None) -> bytes:
    """生成规则库下载文件。

    example 为 None 或 "blank" 时只输出表头（以及说明页，若该版本声明了说明页），
    不含任何示例行；其他取值读取 profile 里真实配置的示例（single_contract /
    contract_and_invoice），把示例行写进对应 Sheet。示例是真实进入导入器的演示配置，
    不是执行结果。

    说明页由 profile 决定：只有 profile 声明了 ``instruction_sheet``（v1 遗留形态）
    才生成说明页；v2 没有该键时只生成 profile 声明的业务工作表，不再生成任何
    00 开头的说明 Sheet。工作表的列与顺序完全由 profile 的 ``sheet_roles`` 驱动。
    """
    profile = load_rulebook_profile()
    example_content = None if example in BLANK_EXAMPLE_KEYS else _example_content(profile, str(example))
    example_sheets: dict[str, list[list[Any]]] = (
        example_content["sheets"] if example_content is not None else {}
    )
    role_names = {role["display_name"] for role in profile["sheet_roles"]}
    unknown_sheets = sorted(set(example_sheets) - role_names)
    if unknown_sheets:
        raise BusinessError(
            "PROFILE_INVALID",
            "合同审核模板配置中的示例引用了不存在的工作表",
            status_code=500,
            details={"sheet_names": unknown_sheets},
        )

    workbook = Workbook()
    instruction_sheet_name = profile.get("instruction_sheet")
    if instruction_sheet_name:
        instruction = workbook.active
        instruction.title = instruction_sheet_name
        for row in _instruction_rows(profile, example_content):
            instruction.append(row)
        instruction.column_dimensions["A"].width = 18
        instruction.column_dimensions["B"].width = 96
        instruction["A1"].font = Font(size=16, bold=True, color="174A5B")

    for role_no, role in enumerate(profile["sheet_roles"]):
        if role_no == 0 and not instruction_sheet_name:
            # 没有说明页时复用新建工作簿自带的工作表，避免留下空 Sheet。
            sheet = workbook.active
            sheet.title = role["display_name"]
        else:
            sheet = workbook.create_sheet(role["display_name"])
        headers = [column["name"] for column in role["columns"]]
        sheet.append(headers)
        for example_row in example_sheets.get(role["display_name"], []):
            if len(example_row) > len(headers):
                raise BusinessError(
                    "PROFILE_INVALID",
                    "合同审核模板配置中的示例行超过了该工作表的列数",
                    status_code=500,
                    details={"sheet_name": role["display_name"], "column_count": len(headers)},
                )
            sheet.append(list(example_row))
        sheet.freeze_panes = "A2"
        sheet.auto_filter.ref = f"A1:{sheet.cell(1, len(headers)).coordinate}"
        for column_no, column in enumerate(role["columns"], start=1):
            cell = sheet.cell(1, column_no)
            cell.fill = REQUIRED_FILL if column.get("required") else HEADER_FILL
            cell.font = Font(color="222222" if column.get("required") else "FFFFFF", bold=True)
            cell.alignment = Alignment(horizontal="center", vertical="center")
            sheet.column_dimensions[cell.column_letter].width = max(14, min(30, len(column["name"]) * 2 + 6))
            value_labels = column.get("value_labels")
            values = list(value_labels.values()) if value_labels else column.get("allowed_values")
            if values:
                validation = DataValidation(
                    type="list", formula1='"' + ",".join(values) + '"', allow_blank=not column.get("required")
                )
                validation.error = "请选择模板允许的值"
                validation.errorTitle = "填写值无效"
                sheet.add_data_validation(validation)
                validation.add(f"{cell.column_letter}2:{cell.column_letter}500")

    stream = BytesIO()
    workbook.save(stream)
    return stream.getvalue()
