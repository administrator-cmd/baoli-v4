"""有限十进制数的精确加减与求和；精度按操作数确定，不修改全局上下文。

除法可能产生无限小数，其精度由计算求值器单独声明。
"""

from collections.abc import Iterable
from decimal import Decimal, InvalidOperation, localcontext


def _require_finite(left: Decimal, right: Decimal) -> None:
    if not left.is_finite() or not right.is_finite():
        raise InvalidOperation("十进制运算要求有限数值")


def _additive_precision(left: Decimal, right: Decimal) -> int:
    _require_finite(left, right)
    # 对齐到较小指数后，保留最高有效位到最低小数位，并给进位留一位。
    exponent = min(left.as_tuple().exponent, right.as_tuple().exponent)
    return max(left.adjusted(), right.adjusted()) - exponent + 2


def exact_add(left: Decimal, right: Decimal) -> Decimal:
    """精确相加，保留两个操作数的全部有效位。"""
    with localcontext() as context:
        context.prec = _additive_precision(left, right)
        return left + right


def exact_subtract(left: Decimal, right: Decimal) -> Decimal:
    """精确相减，包含长数值与小差额的组合。"""
    with localcontext() as context:
        context.prec = _additive_precision(left, right)
        return left - right


def exact_sum(values: Iterable[Decimal]) -> Decimal:
    """逐项精确累加，避免默认上下文在每次相加时舍入。"""
    total = Decimal("0")
    for value in values:
        total = exact_add(total, value)
    return total
