from uuid import uuid4

from fastapi import Request
from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send


def request_id_of(request: Request) -> str:
    """同一请求始终复用同一个编号，异常响应与日志也使用它。"""
    if not getattr(request.state, "request_id", None):
        request.state.request_id = request.headers.get("X-Request-ID") or uuid4().hex
    return request.state.request_id


class RequestContextMiddleware:
    """包在整个 HTTP 应用外层，让 500、预检、下载及流式响应都有请求编号。

    仅包装 ASGI 发送事件，不捕获异常、不读取响应体、不改变流式响应行为。
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = Headers(scope=scope).get("X-Request-ID") or uuid4().hex
        scope.setdefault("state", {})["request_id"] = request_id

        async def send_with_request_id(message: Message) -> None:
            if message["type"] == "http.response.start":
                MutableHeaders(scope=message)["X-Request-ID"] = request_id
            await send(message)

        await self.app(scope, receive, send_with_request_id)

