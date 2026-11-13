from pydantic import BaseModel, Field


class PagedResponse[T](BaseModel):
    """后端分页视图：total 始终来自服务端同一查询作用域。"""

    total: int
    page: int
    page_size: int
    items: list[T] = Field(default_factory=list)


class ItemsResponse[T](BaseModel):
    """下拉选项列表；排序与筛选由查询层决定。"""

    items: list[T] = Field(default_factory=list)

