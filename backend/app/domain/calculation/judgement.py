"""计算判定：把计算结果落到判定区间上。

判定只做一件事：在**发布期已校验过不重叠**的区间里找到命中的那一条。
找不到命中区间时返回「未命中任何判定区间」，这是一个**显式状态**，不是合格：
样本里 `<1：预警；1~1.5：一般；1.5~2：良好；>2：优秀` 这样的区间串覆盖了整条数轴，
但真实配置完全可能留下空隙（例如只配了 `<1` 与 `>2`），空隙里的结果没有任何业务结论。
把它当成通过或当成异常都是系统替业务下结论。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from .schema import ComparisonBasis, JudgementBand


# 判定结果状态。它们是**业务判定结论**，与计算是否算得出来是两件事。
JUDGEMENT_MATCHED = "MATCHED"
JUDGEMENT_UNMATCHED = "UNMATCHED"


@dataclass(frozen=True)
class JudgementResult:
    status: str
    band: JudgementBand | None
    # 参与比较的值：RESULT 基准下就是计算结果，ABS_RESULT 基准下是它的绝对值。
    compared_value: Decimal | None
    reason: str | None = None

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "status": self.status,
            "compared_value": None if self.compared_value is None else format(self.compared_value, "f"),
        }
        if self.band is not None:
            payload.update(
                {
                    "condition_code": self.band.condition_code,
                    "band": self.band.describe(),
                    "lower_value": self.band.lower_value,
                    "lower_inclusive": self.band.lower_inclusive,
                    "upper_value": self.band.upper_value,
                    "upper_inclusive": self.band.upper_inclusive,
                    "comparison_basis": self.band.comparison_basis,
                    "judgement": self.band.judgement,
                    "risk_level": self.band.risk_level,
                    "action": self.band.action,
                }
            )
        if self.reason:
            payload["reason"] = self.reason
        return payload


def judge(value: Decimal, bands: list[JudgementBand]) -> JudgementResult:
    """按判定区间判定一个计算结果。

    区间不重叠已在发布期保证（见 schema.assert_bands_disjoint），因此这里最多命中一条；
    仍然遍历全部区间并收集全部命中项，一旦出现多条就说明发布校验被绕过，
    此时明确报错而不是取第一条。
    """
    matched: list[tuple[JudgementBand, Decimal]] = []
    for band in bands:
        compared = value if band.comparison_basis == "RESULT" else abs(value)
        if band.contains(compared):
            matched.append((band, compared))
    if not matched:
        return JudgementResult(
            status=JUDGEMENT_UNMATCHED,
            band=None,
            compared_value=value,
            reason=(
                "计算结果没有命中任何已配置的判定区间。"
                "这不等于合格，也不等于异常：本次配置的区间没有覆盖这个取值"
            ),
        )
    if len(matched) > 1:
        from ...errors import BusinessError

        raise BusinessError(
            "CALC_JUDGEMENT_BAND_AMBIGUOUS",
            "计算结果同时命中多条判定区间，无法确定业务结论："
            + "、".join(band.condition_code for band, _ in matched),
            status_code=500,
            details={"condition_codes": [band.condition_code for band, _ in matched]},
        )
    band, compared = matched[0]
    return JudgementResult(status=JUDGEMENT_MATCHED, band=band, compared_value=compared)


def basis_label(basis: ComparisonBasis | str) -> str:
    """判定比较基准的中文名；未知取值原样返回编码。"""
    from .schema import COMPARISON_BASIS_LABELS

    return COMPARISON_BASIS_LABELS.get(str(basis), str(basis))
