from pydantic import BaseModel


class ApiResponse(BaseModel):
    """所有 HTTP 响应共用的请求追踪字段。"""

    request_id: str

