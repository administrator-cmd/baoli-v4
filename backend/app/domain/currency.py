"""金额字段使用的确定性币种规范化。"""

from __future__ import annotations

from typing import Any

_ALIASES: dict[str, str] = {
    "人民币": "CNY",
    "CNY": "CNY",
    "RMB": "CNY",
    "¥": "CNY",
    "￥": "CNY",
    "美元": "USD",
    "USD": "USD",
    "欧元": "EUR",
    "EUR": "EUR",
    "€": "EUR",
    "英镑": "GBP",
    "GBP": "GBP",
    "£": "GBP",
}


def canonical_currency(value: Any) -> str:
    """把已知别名转换为稳定代码；未知值保持原样，不猜测。"""
    text = str(value or "").strip()
    if not text:
        return ""
    if text in _ALIASES:
        return _ALIASES[text]
    upper = text.upper() if text.isascii() else text
    return _ALIASES.get(upper, text)
