"""审核状态词汇的查询函数。

状态码、标签和分档的唯一定义在 `constants/status.py`；本模块只保留按状态查询的辅助函数，
避免 domain、services 与页面各自解释状态。
"""

from typing import Any

from ..constants.status import (
    MATERIAL_TYPE_UNAVAILABLE_STATUSES,
    RULE_STATUS_LABELS,
    RULE_STATUSES,
    STATUS_CATEGORIES,
    STATUS_CATEGORY_LABELS,
    STATUS_CATEGORY_UNKNOWN,
    STATUS_NOTES,
)

__all__ = [
    "is_type_unavailable", "status_category_label_of", "status_category_of",
    "status_label_of", "status_note", "status_view",
]


def is_type_unavailable(status: str | None) -> bool:
    """材料位的类型是否不可用（待复核或已确认不符）。"""
    return status in MATERIAL_TYPE_UNAVAILABLE_STATUSES


def status_category_of(status: str | None) -> str:
    """规则结果状态的分档。未知状态码返回 UNKNOWN，不归入任何已知分档。"""
    if not isinstance(status, str):
        return STATUS_CATEGORY_UNKNOWN
    return STATUS_CATEGORIES.get(status, STATUS_CATEGORY_UNKNOWN)


def status_label_of(status: str | None) -> str | None:
    """状态的标准中文含义；未知状态码返回 None（调用方必须显式显示原始码）。"""
    if not isinstance(status, str):
        return None
    return RULE_STATUS_LABELS.get(status)


def status_category_label_of(status: str | None) -> str | None:
    """状态分档的中文名；未知状态码归入「未识别状态」。"""
    category = status_category_of(status)
    return STATUS_CATEGORY_LABELS.get(category)


def status_note(status: str | None) -> str | None:
    """某个「未判定」状态到底缺什么；未知状态码返回 None。"""
    if not isinstance(status, str):
        return None
    return STATUS_NOTES.get(status)


def status_view(status: str | None) -> dict[str, Any]:
    """一个状态码的标准解释，供接口与页面共用。

    未知状态码 `known=False` 且 `label=None`：页面必须显示原始码并标注「未识别」，
    不能用「未开始」这类兜底文案把它伪装成正常状态。
    """
    known = isinstance(status, str) and status in RULE_STATUSES
    return {
        "status": status,
        "known": known,
        "label": status_label_of(status),
        "category": status_category_of(status),
        "category_label": status_category_label_of(status),
        "note": status_note(status),
    }
