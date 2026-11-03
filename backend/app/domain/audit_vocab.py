"""审核状态词汇的查询函数。

状态码、标签、分档与说明的唯一定义在 `constants/status.py`；本模块按语境
（规则、风险、字段）组装词表，供目录接口与页面共用，避免各处重复解释状态。
"""

from typing import Any

from app.constants.status import (
    FIELD_STATUS_LABELS,
    MATERIAL_TYPE_UNAVAILABLE_STATUSES,
    RISK_STATUS_LABELS,
    RULE_STATUS_LABELS,
    RULE_STATUSES,
    STATUS_CATEGORIES,
    STATUS_CATEGORY_LABELS,
    STATUS_NOTES,
)

# 页面上同一状态码在不同语境使用不同文案；域就是这套语境的名字。
STATUS_DOMAINS: tuple[str, ...] = ("rule", "risk", "field")

_DOMAIN_LABELS: dict[str, dict[str, str]] = {
    "rule": RULE_STATUS_LABELS,
    "risk": RISK_STATUS_LABELS,
    "field": FIELD_STATUS_LABELS,
}


def is_type_unavailable(status: str | None) -> bool:
    """材料位的类型是否不可用（待复核或已确认不符）。"""
    return status in MATERIAL_TYPE_UNAVAILABLE_STATUSES


def domain_status_terms(domain: str) -> list[dict[str, Any]]:
    """一个语境下的全部状态码、文案、分档与说明。

    未知域直接报错，不返回空表——静默空表会让页面把全部结果显示成未识别。
    """
    labels = _DOMAIN_LABELS.get(domain)
    if labels is None:
        raise ValueError(f"未知状态域：{domain}")
    codes = RULE_STATUSES if domain in ("rule", "risk") else tuple(labels)
    terms: list[dict[str, Any]] = []
    for code in codes:
        label = labels.get(code)
        if label is None:
            continue
        category = STATUS_CATEGORIES.get(code) if domain in ("rule", "risk") else None
        terms.append({
            "code": code,
            "label": label,
            "category": category,
            "category_label": STATUS_CATEGORY_LABELS.get(category) if category else None,
            "note": STATUS_NOTES.get(code) if domain in ("rule", "risk") else None,
        })
    return terms
