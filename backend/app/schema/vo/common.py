from typing import Any

from pydantic import Field

from ...common.api import ApiResponse


class PagedResponse(ApiResponse):
    """后端分页视图：total 始终来自服务端同一查询作用域。"""

    total: int
    page: int
    page_size: int
    items: list[dict[str, Any]] = Field(default_factory=list)


class OperationLogPageResponse(PagedResponse):
    pass


class RiskResultPageResponse(PagedResponse):
    """风险结果分页：命中数仅统计 ABNORMAL。"""

    hit_count: int = 0
    risk_levels: list[str] = Field(default_factory=list)

