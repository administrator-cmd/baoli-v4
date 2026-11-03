"""统一响应信封：成功与失败共用同一形状。

成功时 data 承载业务 VO；失败时 data 承载已脱敏的 details，见 common/exceptions.py。
文件下载、流式响应与 204 无响应体不走这里。
"""

from fastapi import Request
from pydantic import BaseModel

from app.middleware.request_context import request_id_of


class ResponseSchema[T](BaseModel):
    """所有 JSON 接口的统一响应信封。"""

    code: str = "OK"
    message: str = "成功"
    data: T | None = None
    request_id: str
    success: bool = True


def ok[T](data: T | None, request: Request, message: str = "成功") -> ResponseSchema[T]:
    """用当前请求编号构造成功信封。"""
    return ResponseSchema[T](data=data, request_id=request_id_of(request), message=message)
