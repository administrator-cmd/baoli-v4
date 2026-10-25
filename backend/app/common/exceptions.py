import logging
import os
import re
import traceback
from typing import Any, Mapping

from fastapi import Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

from ..middleware.request_context import request_id_of

logger = logging.getLogger(__name__)


class ErrorBody(BaseModel):
    code: str
    message: str
    details: dict[str, Any] = Field(default_factory=dict)
    request_id: str


class BusinessError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        status_code: int = 400,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.details = details or {}


# 未预期异常的定位信息必须能被事后读到：只写一句通用文案会让一次失败彻底无法诊断，
# 日志没有落盘时只能靠重启复现碰运气。这里把异常类型、消息与最后一层业务代码栈帧
# 一起落库到 failure_message——只取最后一层自己的代码（排除 site-packages 与标准库），
# 那是业务代码真正抛错的位置，也正是需要修的那一行；完整堆栈仍由 logger.exception 记录。
MAX_ERROR_SUMMARY = 2000


def _is_own_frame(filename: str) -> bool:
    normalized = filename.replace("\\", "/")
    if "/site-packages/" in normalized or "/lib/python" in normalized.lower():
        return False
    return "/app/" in normalized


def exception_summary(exc: BaseException) -> str:
    """未预期异常的定位摘要：类型 + 消息 + 最深层业务栈帧的位置。

    用于服务端日志与后台失败记录，不作为未知 HTTP 异常的客户端详情。
    原始异常消息可能包含 SQL 或外部服务信息，不能宣称其天然不含敏感信息。
    """
    parts = [f"{type(exc).__name__}: {exc}".strip()]
    for frame in reversed(traceback.extract_tb(exc.__traceback__)):
        if _is_own_frame(frame.filename):
            filename = os.path.basename(frame.filename)
            parts.append(f"位置 {filename}:{frame.lineno} 于 {frame.name}()")
            break
    return "；".join(parts)[:MAX_ERROR_SUMMARY]



# 对业务详情按敏感键脱敏，不用业务键白名单，也不截断正常定位信息。
# 按键名处理原始载荷与凭据；无法识别自由文本中的密钥，生产端须使用明确的业务消息。
UNSAFE_KEY_PATTERN = re.compile(
    r"token|secret|password|passwd|apikey|api_key|authorization|credential|dsn|"
    r"connection_string|database_url|cookie|"
    r"(?:^|_)(?:headers?|raw_response|raw_request|payload|body)(?:_json|_text)?$",
    re.IGNORECASE,
)
REDACTED_VALUE = "[已脱敏]"


def _safe_detail_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            key: REDACTED_VALUE if UNSAFE_KEY_PATTERN.search(str(key)) else _safe_detail_value(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_safe_detail_value(item) for item in value]
    return value


def safe_error_details(details: dict[str, Any] | None) -> dict[str, Any]:
    """保留完整业务详情，仅显式脱敏敏感键；不丢弃空值、未知业务键或深层定位信息。"""
    return jsonable_encoder(_safe_detail_value(details if details is not None else {}))


def _error_response(
    request: Request,
    *,
    status_code: int,
    code: str,
    message: str,
    details: dict[str, Any] | None = None,
    headers: Mapping[str, str] | None = None,
) -> JSONResponse:
    body = ErrorBody(
        code=code,
        message=message,
        details=safe_error_details(details),
        request_id=request_id_of(request),
    )
    response = JSONResponse(
        status_code=status_code,
        content=body.model_dump(mode="json"),
        # 响应体已转换为 JSON，其类型与长度必须由 JSONResponse 重新计算。
        headers={
            key: value for key, value in (headers or {}).items()
            if key.lower() not in {"content-type", "content-length"}
        },
    )
    response.headers["X-Request-ID"] = body.request_id
    return response


def _log_error(
    request: Request, exc: Exception, *, code: str, message: str,
    status_code: int, details: dict[str, Any],
) -> None:
    logger.log(
        logging.ERROR if status_code >= 500 else logging.WARNING,
        "请求失败 request_id=%s method=%s path=%s status_code=%s code=%s message=%s details=%r",
        request_id_of(request), request.method, request.url.path, status_code, code, message, details,
        exc_info=(type(exc), exc, exc.__traceback__) if status_code >= 500 else None,
    )


def business_error_handler(request: Request, exc: BusinessError) -> JSONResponse:
    details = safe_error_details(exc.details)
    _log_error(
        request, exc, code=exc.code, message=exc.message, status_code=exc.status_code, details=details,
    )
    return _error_response(
        request, status_code=exc.status_code, code=exc.code, message=exc.message, details=details,
    )


def unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
    # 完整异常链与堆栈留在日志，通过请求编号定位；不向客户端公开内部异常文本。
    _log_error(
        request, exc, code="INTERNAL_ERROR", message="未处理的应用异常", status_code=500, details={},
    )
    return _error_response(
        request, status_code=500, code="INTERNAL_ERROR", message="系统处理失败，请提供请求编号排查",
    )


def request_validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    # 请求结构校验不涉及规则业务合理性；排除校验条目的 input、body 和 ctx。
    fields = [
        {
            "field": ".".join(str(part) for part in error["loc"]),
            "reason": error["type"],
            "message": error["msg"],
        }
        for error in exc.errors()
    ]
    details = {"fields": fields}
    _log_error(
        request, exc, code="REQUEST_VALIDATION_FAILED", message="请求参数不符合要求",
        status_code=422, details=details,
    )
    return _error_response(
        request, status_code=422, code="REQUEST_VALIDATION_FAILED",
        message="请求参数不符合要求", details=details,
    )


def http_error_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    # 状态码直接进入错误码；保留框架/接口声明的说明及 Allow、WWW-Authenticate 等响应头。
    code = f"HTTP_{exc.status_code}"
    details = safe_error_details(exc.detail) if isinstance(exc.detail, dict) else (
        safe_error_details({"detail": exc.detail})
        if exc.detail is not None and not isinstance(exc.detail, str) else {}
    )
    message = exc.detail if isinstance(exc.detail, str) else f"HTTP 请求失败（{exc.status_code}）"
    _log_error(request, exc, code=code, message=message, status_code=exc.status_code, details=details)
    return _error_response(
        request, status_code=exc.status_code, code=code, message=message,
        details=details, headers=exc.headers,
    )
