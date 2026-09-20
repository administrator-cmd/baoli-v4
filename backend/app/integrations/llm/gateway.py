"""远程大模型 HTTP 网关：把请求发给 OpenAI 兼容的 chat/completions 端点。

这里是**唯一**与远程模型通信的出口。调用方只提供业务 payload 与系统提示词，
拿回已解析成 dict 的 JSON；供应商的地址、密钥、超时与响应格式细节都不外泄。

供应商限制（不直观，改动前先读）：

- 端点可能是 baseurl 本身（已带 `/chat/completions`），也可能是 baseurl 下的
  标准路径，两种写法都必须接受。
- 响应可能是纯 JSON，也可能被 Markdown 代码块包裹；后者不是契约变化，
  而是模型常见的输出习惯，按同一份内容解析。
- 配置缺失（baseurl / apikey / model）时明确报 503，**不**退回本地规则或空结果：
  一次配置错误必须暴露成配置错误，不能伪装成「转换完成但没有内容」。

本模块不依赖 domain 的规则契约，只做「发请求、取 JSON」这件事。
"""

import json
import logging
from typing import Any

import httpx

from ...config import get_settings
from ...errors import BusinessError


logger = logging.getLogger(__name__)


def _endpoint(baseurl: str) -> str:
    normalized = baseurl.rstrip("/")
    return normalized if normalized.endswith("/chat/completions") else f"{normalized}/chat/completions"


def call_json(payload: dict[str, Any], system_prompt: str) -> dict[str, Any]:
    settings = get_settings()
    api_key = settings.llm_api_key_value
    missing = [
        name
        for name, value in {
            "BAOLI_LLM_BASEURL": settings.llm_baseurl,
            "BAOLI_LLM_APIKEY": api_key,
            "BAOLI_LLM_MODEL_NAME": settings.llm_model_name,
        }.items()
        if not value
    ]
    if missing:
        raise BusinessError(
            "LLM_CONFIG_MISSING",
            "远程大模型配置不完整，无法转换规则",
            status_code=503,
            details={"missing": missing},
        )
    request_body = {
        "model": settings.llm_model_name,
        "temperature": 0,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ],
        "response_format": {"type": "json_object"},
    }
    try:
        with httpx.Client(timeout=settings.request_timeout_seconds) as client:
            response = client.post(
                _endpoint(settings.llm_baseurl),
                headers={"Authorization": f"Bearer {api_key}"},
                json=request_body,
            )
            response.raise_for_status()
    except httpx.TimeoutException as exc:
        logger.warning("远程大模型请求超时 error_type=%s", type(exc).__name__)
        raise BusinessError(
            "LLM_TIMEOUT",
            "远程大模型请求超时",
            status_code=504,
            details={"service": "LLM"},
        ) from exc
    except httpx.HTTPStatusError as exc:
        status_code = exc.response.status_code
        logger.warning("远程大模型返回 HTTP 错误 status_code=%s", status_code)
        if status_code in {401, 403}:
            raise BusinessError(
                "LLM_AUTH_FAILED",
                "远程大模型鉴权失败",
                status_code=502,
                details={"service": "LLM"},
            ) from exc
        raise BusinessError(
            "LLM_EXTERNAL_FAILED",
            "远程大模型服务返回错误",
            status_code=502,
            details={"service": "LLM"},
        ) from exc
    except httpx.RequestError as exc:
        logger.warning("远程大模型网络请求失败 error_type=%s", type(exc).__name__)
        raise BusinessError(
            "LLM_NETWORK_FAILED",
            "远程大模型网络请求失败",
            status_code=502,
            details={"service": "LLM"},
        ) from exc
    try:
        envelope = response.json()
        content = envelope["choices"][0]["message"]["content"]
        if not isinstance(content, str):
            raise TypeError("LLM content must be text")
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        logger.warning("远程大模型响应结构无效 error_type=%s", type(exc).__name__)
        raise BusinessError(
            "LLM_CONTRACT_INVALID",
            "远程大模型返回内容不符合规则转换契约",
            status_code=502,
        ) from exc
    try:
        return json.loads(content)
    except (TypeError, json.JSONDecodeError) as exc:
        logger.warning("远程大模型返回内容不是有效 JSON error_type=%s", type(exc).__name__)
        raise BusinessError(
            "LLM_CONTRACT_INVALID",
            "远程大模型返回内容不符合规则转换契约",
            status_code=502,
        ) from exc

