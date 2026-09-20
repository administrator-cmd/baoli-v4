import logging
import os
import re
import traceback
from typing import Any

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field


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


# 未预期异常的定位信息必须能被事后读到。此前这类失败只写一句通用文案
# （「发生未处理异常」），异常类型与出错位置只进 stdout 日志；日志没有落盘时，
# 一次失败就彻底无法诊断，只能靠重启复现碰运气。
# 这里把异常类型、消息与**最后一层业务代码栈帧**一起落库到 failure_message。
# 只取最后一层自己的代码（排除 site-packages 与标准库）：那是业务代码真正抛错的位置，
# 也正是需要修的那一行；完整堆栈仍然由 logger.exception 记录在日志里。
MAX_ERROR_SUMMARY = 2000


def _is_own_frame(filename: str) -> bool:
    normalized = filename.replace("\\", "/")
    if "/site-packages/" in normalized or "/lib/python" in normalized.lower():
        return False
    return "/app/" in normalized


def exception_summary(exc: BaseException) -> str:
    """未预期异常的定位摘要：类型 + 消息 + 最深层业务栈帧的位置。

    不回传任何密钥或外部载荷：异常消息本身来自本系统代码与 Python 运行时，
    这里也只截取最后一个业务栈帧的文件名与行号。
    """
    parts = [f"{type(exc).__name__}: {exc}".strip()]
    for frame in reversed(traceback.extract_tb(exc.__traceback__)):
        if _is_own_frame(frame.filename):
            filename = os.path.basename(frame.filename)
            parts.append(f"位置 {filename}:{frame.lineno} 于 {frame.name}()")
            break
    return "；".join(parts)[:MAX_ERROR_SUMMARY]



# 只有这些键是可安全回传的业务定位信息：工作表 / 行 / 列、字段 / 规则 / 材料、
# 操作编号与错误码。它们让用户能定位并修正问题，不包含内部载荷。
SAFE_DETAIL_KEYS = frozenset(
    {
        "sheet_key",
        "sheet_name",
        "row_no",
        "column",
        "column_no",
        "cells",
        "cell",
        "field_key",
        "field_name",
        "field_keys",
        "rule_key",
        "rule_name",
        "rule_keys",
        "material_key",
        "material_name",
        "material_keys",
        "node_code",
        "node",
        "operator",
        "kind",
        "input_kind",
        "issue_no",
        "review_no",
        "review_type",
        "evidence_no",
        "artifact_no",
        "artifact_type",
        "document_no",
        "upload_no",
        "operation_no",
        "object_no",
        "object_type",
        "operation_type",
        "task_no",
        "job_no",
        "scenario_code",
        "scene_code",
        "version_no",
        "profile_version",
        # 计划/方案、报告与操作日志的定位键：让用户能指明是哪一个对象出错。
        "plan_code",
        "plan_name",
        "report_no",
        "log_no",
        "source_scenario_code",
        "section_names",
        "max_bytes",
        "allowed_extensions",
        "missing_tables",
        "existing_table_count",
        "database",
        "script",
        "count",
        "expected",
        "actual",
        # 现有校验代码实际使用的顶层键。它们都是业务定位信息，不含密钥或外部载荷。
        "rule",
        "section",
        "reference",
        "operator",
        "allowed",
        "missing",
        "parameter",
        "property",
        "role",
        "filename",
        "reason",
        "field",
        "material",
        "capability",
        "matched",
        "detail",
        "blockers",
        "mismatches",
        "conflicts",
        # 嵌套结构（例如 blockers 列表项）使用的业务键。message 与顶层 message 同级安全。
        "code",
        "message",
        "name",
        "status",
        "value",
    }
)
# 键名里出现这些片段的字段一律不回传（密钥、令牌、连接串、地址）。
UNSAFE_KEY_PATTERN = re.compile(
    r"token|secret|password|passwd|apikey|api_key|authorization|credential|dsn|conn|url|uri|host|header",
    re.IGNORECASE,
)
MAX_DETAIL_STRING = 300


def _safe_detail_value(value: Any, depth: int = 0) -> Any:
    if depth > 3:
        return None
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        trimmed = value.strip()
        if not trimmed:
            return None
        return trimmed[:MAX_DETAIL_STRING]
    if isinstance(value, (list, tuple)):
        items = [_safe_detail_value(entry, depth + 1) for entry in value[:50]]
        cleaned = [item for item in items if item is not None]
        return cleaned or None
    if isinstance(value, dict):
        nested: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str) or key not in SAFE_DETAIL_KEYS or UNSAFE_KEY_PATTERN.search(key):
                continue
            safe = _safe_detail_value(item, depth + 1)
            if safe is not None:
                nested[key] = safe
        return nested or None
    return None


def safe_error_details(details: dict[str, Any] | None) -> dict[str, Any]:
    """把业务错误详情裁剪成可安全回传的定位信息。

    删除密钥、令牌、连接串与外部服务载荷，但保留 Sheet/行/列、字段、规则、
    材料、节点、操作编号这类能让用户修正问题的键。不能用统一的空 details 让用户无从下手。
    """
    if not details:
        return {}
    safe: dict[str, Any] = {}
    for key, value in details.items():
        if not isinstance(key, str) or UNSAFE_KEY_PATTERN.search(key):
            continue
        if key not in SAFE_DETAIL_KEYS:
            continue
        cleaned = _safe_detail_value(value)
        if cleaned is not None:
            safe[key] = cleaned
    return safe


def business_error_handler(request: Request, exc: BusinessError) -> JSONResponse:
    request_id = getattr(request.state, "request_id", "unknown")
    logger.warning(
        "业务请求失败 request_id=%s code=%s status_code=%s details=%r",
        request_id,
        exc.code,
        exc.status_code,
        exc.details,
    )
    body = ErrorBody(
        code=exc.code,
        message=exc.message,
        # 完整 details 只进服务端日志；回传给客户端的是裁剪后的安全业务定位信息。
        details=safe_error_details(exc.details),
        request_id=request_id,
    )
    return JSONResponse(status_code=exc.status_code, content=body.model_dump())


def unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
    request_id = getattr(request.state, "request_id", "unknown")
    # 摘要同时进日志与响应：用户凭请求编号能直接看到异常类型与出错位置，
    # 不必再去翻日志；这条信息不含密钥与外部载荷（见 exception_summary）。
    summary = exception_summary(exc)
    logger.exception("未处理的应用异常 request_id=%s 摘要=%s", request_id, summary, exc_info=exc)
    body = ErrorBody(
        code="INTERNAL_ERROR",
        message="系统处理失败，请稍后重试或联系系统管理员",
        details={"reason": summary},
        request_id=request_id,
    )
    return JSONResponse(status_code=500, content=body.model_dump())


def request_validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    request_id = getattr(request.state, "request_id", "unknown")
    logger.warning("请求参数校验失败 request_id=%s errors=%s", request_id, exc.errors())
    # 字段路径与校验类型是安全的定位信息；不返回用户提交的值本身。
    fields = [
        {"field": ".".join(str(part) for part in error.get("loc", ())), "reason": str(error.get("type", ""))}
        for error in exc.errors()[:20]
    ]
    body = ErrorBody(
        code="REQUEST_VALIDATION_FAILED",
        message="请求参数不符合要求",
        details={"fields": fields} if fields else {},
        request_id=request_id,
    )
    return JSONResponse(status_code=422, content=body.model_dump())


def http_error_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    request_id = getattr(request.state, "request_id", "unknown")
    logger.warning("HTTP 请求失败 request_id=%s status_code=%s", request_id, exc.status_code)
    body = ErrorBody(
        code="HTTP_REQUEST_FAILED",
        message="请求的接口不存在或当前操作不被允许",
        details={},
        request_id=request_id,
    )
    return JSONResponse(status_code=exc.status_code, content=body.model_dump())
