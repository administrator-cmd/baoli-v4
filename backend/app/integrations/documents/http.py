"""外部服务共用的 HTTP 调用与响应契约校验。

供应商返回的形状不符合预期时一律报 EXTERNAL_SERVICE_CONTRACT_INVALID，
不猜字段、不补默认值；读不到合法值就是契约变了。
"""

from __future__ import annotations

import logging
import math
from typing import Any

import httpx
import requests

from ...common.exceptions import BusinessError

logger = logging.getLogger(__name__)


def client(timeout: float) -> httpx.Client:
    return httpx.Client(timeout=timeout, follow_redirects=False)


def required_service_url(
    value: str | None,
    service_code: str,
    setting_name: str,
    *,
    strip_trailing_slash: bool = True,
) -> str:
    """取外部服务地址；未配置时明确 503，不回落成相对路径或空串。"""
    if not value:
        raise BusinessError(
            "EXTERNAL_CONFIG_MISSING",
            f"{service_code} 外部服务未配置",
            status_code=503,
            details={"service": service_code, "missing": [setting_name]},
        )
    normalized = value.strip()
    return normalized.rstrip("/") if strip_trailing_slash else normalized


def request(
    client: httpx.Client,
    method: str,
    url: str,
    *,
    service_code: str,
    operation: str,
    **kwargs: Any,
) -> httpx.Response:
    try:
        response = client.request(method, url, **kwargs)
        response.raise_for_status()
        return response
    except httpx.TimeoutException as exc:
        logger.warning("外部服务请求超时 service=%s operation=%s error_type=%s", service_code, operation, type(exc).__name__)
        raise BusinessError(
            "EXTERNAL_SERVICE_TIMEOUT",
            f"{service_code} 外部服务请求超时",
            status_code=504,
            details={"service": service_code, "operation": operation},
        ) from exc
    except httpx.HTTPStatusError as exc:
        logger.warning("外部服务返回 HTTP 错误 service=%s operation=%s status_code=%s", service_code, operation, exc.response.status_code)
        raise BusinessError(
            "EXTERNAL_SERVICE_FAILED",
            f"{service_code} 外部服务返回错误",
            status_code=502,
            details={"service": service_code, "operation": operation},
        ) from exc
    except httpx.RequestError as exc:
        logger.warning("外部服务网络请求失败 service=%s operation=%s error_type=%s", service_code, operation, type(exc).__name__)
        raise BusinessError(
            "EXTERNAL_SERVICE_FAILED",
            f"{service_code} 外部服务网络请求失败",
            status_code=502,
            details={"service": service_code, "operation": operation},
        ) from exc


def json_object(response: httpx.Response | requests.Response, service_code: str, operation: str) -> dict[str, Any]:
    try:
        payload = response.json()
    except ValueError as exc:
        raise contract_error(service_code, operation) from exc
    if not isinstance(payload, dict):
        raise contract_error(service_code, operation)
    return payload


def contract_error(service_code: str, operation: str) -> BusinessError:
    return BusinessError(
        "EXTERNAL_SERVICE_CONTRACT_INVALID",
        f"{service_code} 外部服务返回格式不符合预期",
        status_code=502,
        details={"service": service_code, "operation": operation},
    )


def response_content_type(response: httpx.Response, fallback: str) -> str:
    value = response.headers.get("content-type", "").split(";", 1)[0].strip()
    return value or fallback or "application/octet-stream"


def strict_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    raise ValueError


def non_empty_string(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError
    return value.strip()


def confidence(value: Any) -> float:
    if isinstance(value, bool):
        raise ValueError
    number = float(value)
    if not math.isfinite(number) or not 0 <= number <= 1:
        raise ValueError
    return number
