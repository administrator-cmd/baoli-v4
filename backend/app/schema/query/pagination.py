"""管理端分页参数。"""

from pydantic import BaseModel, Field


class PaginationQueryParam(BaseModel):
    """所有分页接口共用的页码与每页条数。"""

    page: int = Field(default=1, ge=1, description="页码，从 1 开始")
    page_size: int = Field(default=20, ge=1, le=200, description="每页条数")
