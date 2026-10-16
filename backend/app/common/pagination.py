"""分页结果结构；筛选、计数和排序由业务查询负责。"""

from typing import TypedDict


class PageResult[T](TypedDict):
    items: list[T]
    total: int
    page: int
    page_size: int


def page_result[T](items: list[T], total: int, page: int, page_size: int) -> PageResult[T]:
    return {"items": items, "total": int(total), "page": page, "page_size": page_size}
