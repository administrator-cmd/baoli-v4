from uuid import uuid4

from fastapi import Request


async def attach_request_id(request: Request, call_next):
    """为请求和响应统一附加可追踪标识。"""
    request.state.request_id = request.headers.get("X-Request-ID") or uuid4().hex
    response = await call_next(request)
    response.headers["X-Request-ID"] = request.state.request_id
    return response

