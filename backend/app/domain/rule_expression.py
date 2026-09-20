"""规则表达式的确定性解析。

本轮 Excel 契约允许两类表达式，都必须是**确定性**解析，不能交给模型：

1. 多项清单：`主合同；补充协议` —— 统一使用中文分号，忽略每项前后空白；空项、重复项、
   未知引用一律明确报错，空白不表示全部。
2. 加总式：`已付款金额 + 未付款金额 + 其他应付款金额` —— 只允许「字段 + 字段 + …」，
   不执行 Excel 公式、Python 表达式或任意脚本；加数只追加，不增加列。

名称本身不能包含引用语法占用的字符（`.`、`；`、`;`、`+`），否则无法区分名称与表达式，
这里在解析前先做检查并指出具体单元格。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from ..errors import BusinessError


# 多项清单分隔符：填写规范只认中文分号。英文分号与逗号是业务名称里可能出现的字符，
# 不在这里当作分隔符，避免把名称拆坏。
LIST_SEPARATOR = "；"
# 加总式的连接符：前后必须有空格，与填写规范完全一致。
SUM_OPERATOR = " + "
# 报告展示字段的材料-字段分隔符。
FIELD_SCOPE_SEPARATOR = "."
# 名称里不允许出现的引用语法字符。
NAME_FORBIDDEN_CHARS = (".", "；", ";", "+")


@dataclass(frozen=True)
class CellLocation:
    """一个业务单元格的定位信息，用于把错误指回到具体 Sheet/行/列/原值。"""

    sheet_key: str
    sheet_name: str
    row_no: int
    cell: str | None
    column_label: str
    value: str | None = None

    def detail(self, **extra: Any) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "sheet_key": self.sheet_key,
            "sheet_name": self.sheet_name,
            "row_no": self.row_no,
            "column": self.column_label,
        }
        if self.cell:
            payload["cell"] = self.cell
        if self.value not in (None, ""):
            payload["value"] = self.value
        payload.update(extra)
        return payload


def _location_text(location: CellLocation) -> str:
    where = f"工作表「{location.sheet_name}」第 {location.row_no} 行"
    if location.cell:
        where += f"（{location.cell}）"
    return f"{where}的「{location.column_label}」"


def check_business_name(text: str, *, location: CellLocation) -> str:
    """业务名称不能包含引用语法占用的字符，否则名称与表达式无法区分。"""
    normalized = text.strip()
    if not normalized:
        raise BusinessError(
            "RULE_NAME_EMPTY",
            f"{_location_text(location)}为空，请填写后重新上传",
            details=location.detail(),
        )
    forbidden = [char for char in NAME_FORBIDDEN_CHARS if char in normalized]
    if forbidden:
        raise BusinessError(
            "RULE_NAME_FORBIDDEN_CHAR",
            f"{_location_text(location)}的名称“{normalized}”包含引用语法字符 "
            + "、".join(f"“{char}”" for char in forbidden)
            + "，无法与引用表达式区分，请改名后重新上传",
            details=location.detail(forbidden=forbidden),
        )
    return normalized


def split_list(text: str | None, *, location: CellLocation) -> list[str]:
    """按中文分号拆分多项清单。

    空白表示「未配置」，不表示全部；调用方必须先判断单元格是否为空。
    空项（连续分号、末尾分号）与重复项明确报错，不静默丢弃。
    """
    if text is None or not text.strip():
        return []
    raw_items = text.split(LIST_SEPARATOR)
    items: list[str] = []
    for raw in raw_items:
        item = raw.strip()
        if not item:
            raise BusinessError(
                "RULE_LIST_EMPTY_ITEM",
                f"{_location_text(location)}的清单“{text.strip()}”含空白项（多余的“{LIST_SEPARATOR}”），"
                "请删除空项后重新上传",
                details=location.detail(),
            )
        if item in items:
            raise BusinessError(
                "RULE_LIST_DUPLICATE_ITEM",
                f"{_location_text(location)}的清单“{text.strip()}”重复填写了“{item}”，请删除重复项后重新上传",
                details=location.detail(item=item),
            )
        items.append(item)
    return items


def parse_field_expression(text: str | None, *, location: CellLocation) -> list[str]:
    """解析单个字段引用或「字段 + 字段 + …」加总式，返回按书写顺序排列的字段名称。

    只接受 ` + ` 作为连接符；出现其他运算符号、Excel 函数、以 = 开头的公式一律明确报错。
    返回的每一项都是原始字段名称，材料作用域由调用方按工作表语义补齐。
    """
    if text is None or not text.strip():
        return []
    normalized = text.strip()
    if normalized.startswith("="):
        raise BusinessError(
            "RULE_CELL_FORMULA_UNSUPPORTED",
            f"{_location_text(location)}是公式而不是业务内容（“{normalized}”），请直接填写字段名称或“字段 + 字段”后重新上传",
            details=location.detail(),
        )
    # 先按加号切分，再逐项校验：任何非 ` + ` 形式的加号都会在下面被明确报出。
    parts = normalized.split("+")
    if len(parts) > 1:
        for index, part in enumerate(parts):
            if index == 0:
                if not part.endswith(" ") or part.strip() != part[:-1]:
                    _raise_sum_syntax(location, normalized)
            elif index == len(parts) - 1:
                if not part.startswith(" ") or part.strip() != part[1:]:
                    _raise_sum_syntax(location, normalized)
            elif not (part.startswith(" ") and part.endswith(" ")) or part.strip() != part[1:-1]:
                _raise_sum_syntax(location, normalized)
    items = [part.strip() for part in parts]
    if any(not item for item in items):
        _raise_sum_syntax(location, normalized)
    if len(items) > 1 and any(char in item for item in items for char in ("*", "/", "%", "(", ")")):
        raise BusinessError(
            "RULE_EXPRESSION_UNSUPPORTED",
            f"{_location_text(location)}的表达式“{normalized}”包含乘除、百分比或函数，"
            "当前只支持“字段 + 字段 + …”的加法，请改写后重新上传",
            details=location.detail(),
        )
    seen: list[str] = []
    for item in items:
        if item in seen:
            raise BusinessError(
                "RULE_EXPRESSION_DUPLICATE_ADDEND",
                f"{_location_text(location)}的表达式“{normalized}”重复引用了字段“{item}”，请删除重复加数后重新上传",
                details=location.detail(item=item),
            )
        seen.append(item)
    return seen


def _raise_sum_syntax(location: CellLocation, normalized: str) -> None:
    raise BusinessError(
        "RULE_SUM_SYNTAX_INVALID",
        f"{_location_text(location)}的表达式“{normalized}”写法不符合契约：加法必须写成"
        f"“字段{SUM_OPERATOR}字段”（加号两侧各一个空格）。不支持减法、乘除、百分比公式或 Excel 函数。",
        details=location.detail(),
    )


def parse_report_field_reference(text: str, *, location: CellLocation) -> tuple[str, str]:
    """解析报告展示字段“材料名称.字段名称”，返回 (材料名称, 字段名称)。"""
    normalized = text.strip()
    if FIELD_SCOPE_SEPARATOR not in normalized:
        raise BusinessError(
            "REPORT_FIELD_SCOPE_MISSING",
            f"{_location_text(location)}的展示字段“{normalized}”没有写明所属材料，"
            f"请按“材料名称{FIELD_SCOPE_SEPARATOR}字段名称”填写后重新上传",
            details=location.detail(),
        )
    material_name, _, field_name = normalized.partition(FIELD_SCOPE_SEPARATOR)
    material_name = material_name.strip()
    field_name = field_name.strip()
    if not material_name or not field_name or FIELD_SCOPE_SEPARATOR in field_name:
        raise BusinessError(
            "REPORT_FIELD_SCOPE_INVALID",
            f"{_location_text(location)}的展示字段“{normalized}”格式不符合契约，"
            f"请按“材料名称{FIELD_SCOPE_SEPARATOR}字段名称”填写后重新上传",
            details=location.detail(),
        )
    return material_name, field_name


def parse_non_negative_decimal(text: str | None, *, location: CellLocation) -> Decimal | None:
    """解析允许差值：非负数值，0 也是有效配置。

    空白表示本规则不需要差值（文本一致、非空、条款判断），由调用方按检查类型决定是否必填。
    """
    if text is None or not str(text).strip():
        return None
    raw = str(text).strip()
    try:
        value = Decimal(raw)
    except (InvalidOperation, ValueError) as exc:
        raise BusinessError(
            "RULE_THRESHOLD_INVALID",
            f"{_location_text(location)}填写的“{raw}”不是有效数值，请填写不小于 0 的数值后重新上传",
            details=location.detail(),
        ) from exc
    if not value.is_finite() or value < 0:
        raise BusinessError(
            "RULE_THRESHOLD_INVALID",
            f"{_location_text(location)}填写的“{raw}”不是不小于 0 的数值，"
            "0 表示原条件不允许偏差，不能留空或填写负数",
            details=location.detail(),
        )
    return value


def parse_positive_int(text: str | None, *, location: CellLocation) -> int | None:
    """解析章节顺序一类的正整数字段。"""
    if text is None or not str(text).strip():
        return None
    raw = str(text).strip()
    try:
        value = Decimal(raw)
    except (InvalidOperation, ValueError) as exc:
        raise BusinessError(
            "RULE_INTEGER_INVALID",
            f"{_location_text(location)}填写的“{raw}”不是正整数，请修正后重新上传",
            details=location.detail(),
        ) from exc
    if not value.is_finite() or value != value.to_integral_value() or value < 1:
        raise BusinessError(
            "RULE_INTEGER_INVALID",
            f"{_location_text(location)}填写的“{raw}”不是正整数，请修正后重新上传",
            details=location.detail(),
        )
    return int(value)


def format_decimal(value: Decimal) -> str:
    """把 Decimal 写成不丢精度的十进制文本（不使用浮点）。"""
    return format(value.normalize(), "f") if value == value.normalize() else format(value, "f")
