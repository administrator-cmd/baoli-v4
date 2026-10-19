"""远程大模型 HTTP 网关：把请求发给 OpenAI 兼容的 chat/completions 端点。

唯一与远程模型通信的出口。调用方只提供业务 payload 与系统提示词，拿回已解析成 dict 的
JSON；供应商的地址、密钥、超时与响应格式细节都不外泄。

两种调用方式：`call_json()` 只收发 JSON 文本，既有三条路径（规则转换、字段抽取、语义判断）
继续用它；`call_chat()` 是完整的 Chat Completions 契约，支持 `tools` / `tool_choice` 与
`tool_calls` 解析，工具循环由审核图显式编排。

供应商限制：端点可能是 baseurl 本身（已带 `/chat/completions`）或 baseurl 下的标准路径，
两种写法都要接受；请求使用 SSE 流式输出，完整组装后再校验 JSON 或工具调用；
配置缺失（baseurl / apikey / model）明确报 503，不退回本地规则或空结果；
`tool_calls[].function.arguments` 是 JSON 字符串，需要二次解析，不能当普通文本用。

本模块不依赖 domain 的规则契约，只做「发请求、取响应」。
"""

import json
import logging
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx

from ...config import get_settings
from ...common.exceptions import BusinessError


logger = logging.getLogger(__name__)

# 模型响应的种类。它们是互斥的四种结果，调用方必须逐类处理：
# 把 TOOL_CALLS 当成 CONTENT、或把 ERROR 当成 CONTENT，都会让「模型没能给出结论」
# 被读成「模型给出了结论」。
TURN_TOOL_CALLS = "TOOL_CALLS"
TURN_CONTENT = "CONTENT"
TURN_REFUSAL = "REFUSAL"
TURN_ERROR = "ERROR"

# 首次请求之外最多再重试三次。只覆盖明确的临时故障；鉴权、请求契约和
# 响应契约错误不会因重复请求而恢复，必须直接暴露。
LLM_RETRY_DELAYS_SECONDS = (1.0, 2.0, 4.0)
LLM_RETRYABLE_HTTP_STATUSES = frozenset({429, 502, 503, 504})


def _request_result_unknown(exc: BaseException) -> BusinessError:
    """把请求已可能送达、但响应没有可靠返回的故障明确标记。

    读超时、写超时和连接被远端提前关闭都不能证明模型没有处理请求。
    这类故障不能盲目再次 POST，否则可能重复生成并重复计费；由业务层把本次
    操作标记为失败，用户确认后再用新的业务操作重新提交。
    """
    logger.warning(
        "远程大模型请求结果未知 error_type=%s",
        type(exc).__name__,
    )
    return BusinessError(
        "LLM_RESULT_UNKNOWN",
        "远程大模型请求结果未知，系统未自动重复提交；请确认服务状态后重新提交",
        status_code=502,
        details={"service": "LLM", "error_type": type(exc).__name__},
    )


@dataclass(frozen=True)
class ToolCall:
    """一次工具调用请求。

    `arguments_raw` 与 `arguments` 都保留：原始字符串是模型的真实输出，
    解析后的对象是本系统的解释结果。两者都留着，事后才能判断
    「是模型给错了，还是我们解析错了」。
    """

    tool_call_id: str
    tool_name: str
    arguments_raw: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class ModelTurn:
    """一次模型响应的完整契约。

    `raw_message` 保存流式 delta 完整组装的消息，保留原始工具标识、参数字符串和推理内容。
    回填工具结果时使用这份消息，不重新生成标识或重新序列化参数。
    """

    kind: str
    content: str | None
    tool_calls: tuple[ToolCall, ...]
    finish_reason: str | None
    model: str | None
    response_id: str | None
    requested_at: str
    responded_at: str
    raw_message: dict[str, Any]
    refusal: str | None = None

def _endpoint(baseurl: str) -> str:
    normalized = baseurl.rstrip("/")
    return normalized if normalized.endswith("/chat/completions") else f"{normalized}/chat/completions"


def _require_settings() -> tuple[str, str, str]:
    """取出并校验模型配置；缺任一项都明确报 503，不退回任何默认值。"""
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
    return settings.llm_baseurl, api_key, settings.llm_model_name


def _stream_invalid(reason: str) -> BusinessError:
    logger.warning("远程大模型流式响应结构无效 reason=%s", reason)
    return BusinessError(
        "LLM_CONTRACT_INVALID",
        f"远程大模型流式响应不符合调用契约：{reason}",
        status_code=502,
    )


def _sse_data(response: httpx.Response) -> Iterator[str]:
    """按 SSE 事件边界读取 data，保留多行数据，忽略协议定义的注释与控制字段。"""
    data_lines: list[str] = []
    for line in response.iter_lines():
        if not line:
            if data_lines:
                yield "\n".join(data_lines)
                data_lines.clear()
            continue
        if line.startswith(":"):
            continue
        name, _, value = line.partition(":")
        if name == "data":
            data_lines.append(value[1:] if value.startswith(" ") else value)
    if data_lines:
        yield "\n".join(data_lines)


def _merge_stream_delta(
    message: dict[str, Any], tool_calls: dict[int, dict[str, Any]], delta: dict[str, Any],
) -> None:
    """拼接文本及按 index 分段的工具调用；参数保持模型原始字符串。"""
    for key, value in delta.items():
        if value is None:
            continue
        if key == "tool_calls":
            if not isinstance(value, list):
                raise _stream_invalid("tool_calls 不是数组")
            for entry in value:
                if not isinstance(entry, dict):
                    raise _stream_invalid("工具调用片段不是对象")
                index = entry.get("index")
                if type(index) is not int or index < 0:
                    raise _stream_invalid("工具调用片段缺少有效 index")
                call = tool_calls.setdefault(index, {})
                for part_key, part_value in entry.items():
                    if part_key == "index" or part_value is None:
                        continue
                    if part_key == "function":
                        if not isinstance(part_value, dict):
                            raise _stream_invalid("工具调用 function 不是对象")
                        function = call.setdefault("function", {})
                        for function_key, fragment in part_value.items():
                            if fragment is None:
                                continue
                            if not isinstance(fragment, str):
                                raise _stream_invalid("工具函数片段不是字符串")
                            function[function_key] = function.get(function_key, "") + fragment
                    elif part_key == "type":
                        if not isinstance(part_value, str) or (
                            "type" in call and call["type"] != part_value
                        ):
                            raise _stream_invalid("工具调用 type 无效或发生变化")
                        call["type"] = part_value
                    else:
                        if not isinstance(part_value, str):
                            raise _stream_invalid("工具调用片段包含不支持的数据类型")
                        call[part_key] = call.get(part_key, "") + part_value
        elif key == "role":
            if not isinstance(value, str) or ("role" in message and message["role"] != value):
                raise _stream_invalid("消息 role 无效或发生变化")
            message["role"] = value
        else:
            # content、refusal、reasoning_content 等字符串逐段保留，不把推理文本并入正文。
            if not isinstance(value, str):
                raise _stream_invalid("消息片段包含不支持的数据类型")
            message[key] = message.get(key, "") + value


def _read_stream(
    response: httpx.Response, *, attempt: int, started: float, deadline: float,
    timeout_seconds: float,
) -> dict[str, Any]:
    """接收完整流并还原 Chat Completions 响应；未正常结束的流不能作为业务结果。"""
    if response.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "text/event-stream":
        raise _stream_invalid("接口未返回 text/event-stream")
    envelope: dict[str, Any] = {}
    choices: dict[int, dict[str, Any]] = {}
    tool_calls: dict[int, dict[int, dict[str, Any]]] = {}
    chunks = 0
    done = False
    for data in _sse_data(response):
        if time.monotonic() >= deadline:
            logger.warning(
                "远程大模型流式请求超过总时限 attempt=%s chunks=%s timeout_seconds=%s elapsed_seconds=%.3f",
                attempt, chunks, timeout_seconds, time.monotonic() - started,
            )
            raise BusinessError(
                "LLM_TIMEOUT", "远程大模型请求超过总时限", status_code=504,
                details={"service": "LLM", "timeout_seconds": timeout_seconds, "chunks": chunks},
            )
        if data == "[DONE]":
            done = True
            break
        try:
            chunk = json.loads(data)
        except ValueError as exc:
            raise _stream_invalid("data 不是有效 JSON") from exc
        if not isinstance(chunk, dict):
            raise _stream_invalid("data 不是 JSON 对象")
        chunks += 1
        if chunks == 1:
            logger.info(
                "远程大模型收到首个流式数据块 attempt=%s elapsed_seconds=%.3f",
                attempt, time.monotonic() - started,
            )
        if "error" in chunk:
            logger.warning("远程大模型流中返回错误 attempt=%s", attempt)
            raise BusinessError("LLM_EXTERNAL_FAILED", "远程大模型流式响应返回错误", status_code=502)
        for key in ("id", "model", "created", "system_fingerprint", "usage"):
            if key in chunk and chunk[key] is not None:
                envelope[key] = chunk[key]
        entries = chunk.get("choices")
        if not isinstance(entries, list):
            raise _stream_invalid("数据块缺少 choices 数组")
        for entry in entries:
            if not isinstance(entry, dict):
                raise _stream_invalid("choice 不是对象")
            index = entry.get("index")
            if type(index) is not int or index < 0:
                raise _stream_invalid("choice 缺少有效 index")
            choice = choices.setdefault(index, {"index": index, "message": {}, "finish_reason": None})
            delta = entry.get("delta")
            if not isinstance(delta, dict):
                raise _stream_invalid("choice 缺少 delta 对象")
            if choice["finish_reason"] is not None and any(value is not None for value in delta.values()):
                raise _stream_invalid("choice 结束后仍返回消息片段")
            _merge_stream_delta(choice["message"], tool_calls.setdefault(index, {}), delta)
            finish_reason = entry.get("finish_reason")
            if finish_reason is not None:
                if not isinstance(finish_reason, str) or (
                    choice["finish_reason"] is not None and choice["finish_reason"] != finish_reason
                ):
                    raise _stream_invalid("finish_reason 无效或发生变化")
                choice["finish_reason"] = finish_reason
    if not done or not choices or any(choice["finish_reason"] is None for choice in choices.values()):
        logger.warning(
            "远程大模型流未完整结束 attempt=%s chunks=%s done=%s elapsed_seconds=%.3f",
            attempt, chunks, done, time.monotonic() - started,
        )
        raise BusinessError(
            "LLM_STREAM_INCOMPLETE", "远程大模型流式响应未完整结束，未保存本次生成结果", status_code=502,
        )
    if any(choice["finish_reason"] == "length" for choice in choices.values()):
        logger.warning("远程大模型输出被截断 attempt=%s finish_reason=length", attempt)
        raise BusinessError("LLM_OUTPUT_TRUNCATED", "远程大模型输出被截断，未保存本次生成结果", status_code=502)
    for index, choice in choices.items():
        if tool_calls[index]:
            choice["message"]["tool_calls"] = [tool_calls[index][key] for key in sorted(tool_calls[index])]
    envelope["choices"] = [choices[index] for index in sorted(choices)]
    logger.info(
        "远程大模型流式接收完成 attempt=%s chunks=%s elapsed_seconds=%.3f finish_reasons=%s",
        attempt, chunks, time.monotonic() - started,
        [choice["finish_reason"] for choice in envelope["choices"]],
    )
    return envelope


def _post(body: dict[str, Any], *, idempotency_key: str | None = None) -> dict[str, Any]:
    """流式调用 chat/completions；沿用临时故障重试，每次独立组装响应。

    ``idempotency_key`` 标识一次逻辑模型请求，而不是某一次网络尝试；
    同一逻辑请求的自动重试始终复用它，供应商支持该契约时可避免重复生成。
    """
    baseurl, api_key, _ = _require_settings()
    settings = get_settings()
    max_attempts = len(LLM_RETRY_DELAYS_SECONDS) + 1
    # LLM 使用独立的长响应时限；普通外部服务的 request_timeout_seconds 不应截断
    # 仍在持续返回流式数据的规则库解析。
    deadline = time.monotonic() + settings.llm_timeout_seconds
    with httpx.Client(timeout=settings.llm_timeout_seconds) as client:
        for attempt in range(1, max_attempts + 1):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise BusinessError(
                    "LLM_TIMEOUT", "远程大模型请求超过总时限", status_code=504,
                    details={"service": "LLM", "timeout_seconds": settings.llm_timeout_seconds},
                )
            started = time.monotonic()
            logger.info(
                "远程大模型开始流式请求 attempt=%s max_attempts=%s timeout_seconds=%s",
                attempt, max_attempts, settings.llm_timeout_seconds,
            )
            try:
                headers = {
                    "Authorization": f"Bearer {api_key}",
                    "Accept": "text/event-stream",
                }
                if idempotency_key:
                    headers["Idempotency-Key"] = idempotency_key
                with client.stream(
                    "POST",
                    _endpoint(baseurl),
                    headers=headers,
                    json={**body, "stream": True},
                    timeout=remaining,
                ) as response:
                    response.raise_for_status()
                    return _read_stream(
                        response,
                        attempt=attempt,
                        started=started,
                        deadline=deadline,
                        timeout_seconds=settings.llm_timeout_seconds,
                    )
            except httpx.TimeoutException as exc:
                # 只有尚未建立连接、尚未把请求交给远端的超时才自动重试。
                # 读/写超时都可能发生在服务端已经接收请求之后，不能盲目再次 POST。
                safe_to_retry = isinstance(exc, (httpx.ConnectTimeout, httpx.PoolTimeout))
                if safe_to_retry and attempt < max_attempts and time.monotonic() < deadline:
                    delay = LLM_RETRY_DELAYS_SECONDS[attempt - 1]
                    logger.warning(
                        "远程大模型请求超时，将重试 attempt=%s max_attempts=%s delay_seconds=%s error_type=%s",
                        attempt,
                        max_attempts,
                        delay,
                        type(exc).__name__,
                    )
                    time.sleep(delay)
                    continue
                if not safe_to_retry:
                    raise _request_result_unknown(exc) from exc
                logger.warning(
                    "远程大模型请求超时，重试已用尽 attempt=%s retry_count=%s timeout_seconds=%s error_type=%s",
                    attempt,
                    len(LLM_RETRY_DELAYS_SECONDS),
                    settings.llm_timeout_seconds,
                    type(exc).__name__,
                )
                raise BusinessError(
                    "LLM_TIMEOUT",
                    "远程大模型请求超时",
                    status_code=504,
                    details={"service": "LLM", "timeout_seconds": settings.llm_timeout_seconds},
                ) from exc
            except httpx.HTTPStatusError as exc:
                status_code = exc.response.status_code
                if status_code in LLM_RETRYABLE_HTTP_STATUSES and attempt < max_attempts:
                    delay = LLM_RETRY_DELAYS_SECONDS[attempt - 1]
                    logger.warning(
                        "远程大模型返回可重试 HTTP 错误，将重试 status_code=%s attempt=%s max_attempts=%s delay_seconds=%s",
                        status_code,
                        attempt,
                        max_attempts,
                        delay,
                    )
                    time.sleep(delay)
                    continue
                logger.warning(
                    "远程大模型返回 HTTP 错误 status_code=%s attempt=%s retry_count=%s",
                    status_code,
                    attempt,
                    attempt - 1,
                )
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
                # ConnectError / ProxyError 发生在请求建立前，可以安全重试；
                # ReadError、WriteError、RemoteProtocolError 等都无法证明远端未处理。
                safe_to_retry = isinstance(exc, (httpx.ConnectError, httpx.PoolTimeout))
                if safe_to_retry and attempt < max_attempts and time.monotonic() < deadline:
                    delay = LLM_RETRY_DELAYS_SECONDS[attempt - 1]
                    logger.warning(
                        "远程大模型网络请求失败，将重试 attempt=%s max_attempts=%s delay_seconds=%s error_type=%s",
                        attempt,
                        max_attempts,
                        delay,
                        type(exc).__name__,
                    )
                    time.sleep(delay)
                    continue
                if not safe_to_retry:
                    raise _request_result_unknown(exc) from exc
                logger.warning(
                    "远程大模型网络请求失败，重试已用尽 attempt=%s retry_count=%s error_type=%s",
                    attempt,
                    len(LLM_RETRY_DELAYS_SECONDS),
                    type(exc).__name__,
                )
                raise BusinessError(
                    "LLM_NETWORK_FAILED",
                    "远程大模型网络请求失败",
                    status_code=502,
                    details={"service": "LLM"},
                ) from exc


def call_json(
    payload: dict[str, Any], system_prompt: str,
    *, on_content: Callable[[str], None] | None = None, reasoning_effort: str | None = None,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    """单轮请求 JSON 对象，完整接收流式文本后解析；不带工具或工具循环。

    ``idempotency_key`` 在自动重试期间保持不变，标识这一次逻辑请求。
    """
    _, _, model = _require_settings()
    request_body = {
        "model": model,
        "temperature": 0,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ],
        "response_format": {"type": "json_object"},
    }
    if reasoning_effort is not None:
        request_body["reasoning_effort"] = reasoning_effort
    envelope = _post(request_body, idempotency_key=idempotency_key)
    try:
        content = envelope["choices"][0]["message"]["content"]
        if not isinstance(content, str):
            raise TypeError("LLM content must be text")
    except (KeyError, IndexError, TypeError) as exc:
        logger.warning("远程大模型响应结构无效 error_type=%s", type(exc).__name__)
        raise BusinessError(
            "LLM_CONTRACT_INVALID",
            "远程大模型返回内容不符合规则转换契约",
            status_code=502,
        ) from exc
    # 规则库调用方在 JSON 解析之前保存原始正文；保存失败也必须显式失败。
    if on_content is not None:
        on_content(content)
    if envelope["choices"][0]["finish_reason"] != "stop":
        logger.warning("远程大模型未正常完成 JSON 输出 finish_reason=%s", envelope["choices"][0]["finish_reason"])
        raise BusinessError("LLM_CONTRACT_INVALID", "远程大模型未正常完成 JSON 输出", status_code=502)
    try:
        return json.loads(content)
    except (TypeError, json.JSONDecodeError) as exc:
        logger.warning("远程大模型返回内容不是有效 JSON error_type=%s", type(exc).__name__)
        raise BusinessError(
            "LLM_CONTRACT_INVALID",
            "远程大模型返回内容不符合规则转换契约",
            status_code=502,
        ) from exc


def _parse_tool_call(entry: Any, *, index: int) -> ToolCall:
    """解析一个 tool_call。结构不符契约时明确报错，不做任何补救性猜测。"""
    if not isinstance(entry, dict):
        raise BusinessError(
            "LLM_TOOL_RESPONSE_INVALID",
            "远程大模型返回的工具调用结构无效",
            status_code=502,
            details={"index": index, "reason": "工具调用不是对象"},
        )
    tool_call_id = entry.get("id")
    function = entry.get("function")
    if not isinstance(tool_call_id, str) or not tool_call_id:
        raise BusinessError(
            "LLM_TOOL_RESPONSE_INVALID",
            "远程大模型返回的工具调用缺少 id，无法与结果关联",
            status_code=502,
            details={"index": index},
        )
    if not isinstance(function, dict):
        raise BusinessError(
            "LLM_TOOL_RESPONSE_INVALID",
            "远程大模型返回的工具调用缺少 function 定义",
            status_code=502,
            details={"tool_call_id": tool_call_id},
        )
    tool_name = function.get("name")
    if not isinstance(tool_name, str) or not tool_name:
        raise BusinessError(
            "LLM_TOOL_RESPONSE_INVALID",
            "远程大模型返回的工具调用缺少函数名",
            status_code=502,
            details={"tool_call_id": tool_call_id},
        )
    arguments_raw = function.get("arguments")
    if not isinstance(arguments_raw, str):
        # arguments 必须是 JSON 字符串。已经是对象说明供应商契约与预期不同，
        # 这里不「顺手」接受它——那会让两种形状在不同供应商下产生不同行为。
        raise BusinessError(
            "LLM_TOOL_RESPONSE_INVALID",
            "远程大模型返回的工具调用参数不是 JSON 字符串",
            status_code=502,
            details={"tool_call_id": tool_call_id, "type": type(arguments_raw).__name__},
        )
    try:
        arguments = json.loads(arguments_raw)
    except json.JSONDecodeError as exc:
        raise BusinessError(
            "LLM_TOOL_ARGUMENTS_INVALID",
            "远程大模型返回的工具调用参数不是有效 JSON",
            status_code=502,
            details={"tool_call_id": tool_call_id, "tool_name": tool_name},
        ) from exc
    if not isinstance(arguments, dict):
        raise BusinessError(
            "LLM_TOOL_ARGUMENTS_INVALID",
            "远程大模型返回的工具调用参数不是对象",
            status_code=502,
            details={"tool_call_id": tool_call_id, "tool_name": tool_name, "type": type(arguments).__name__},
        )
    return ToolCall(
        tool_call_id=tool_call_id,
        tool_name=tool_name,
        arguments_raw=arguments_raw,
        arguments=arguments,
    )


def call_chat(
    messages: list[dict[str, Any]],
    *,
    tools: list[dict[str, Any]] | None = None,
    tool_choice: Any = "auto",
    response_format: dict[str, Any] | None = None,
    temperature: float = 0,
    idempotency_key: str | None = None,
) -> ModelTurn:
    """完整接收一次流式 Chat Completions 响应，返回明确的响应契约。

    与 `call_json` 的关键差别：不把响应当成 JSON 文本。`tool_calls` 是结构化字段，
    按它自己的契约解析；只有 `content` 才可能是 JSON 文本。

    请求带 `tools` 时，响应必须能归类为四种结果之一。归类不了就报
    `LLM_TOOL_CALL_UNSUPPORTED`——不退回按普通文本解析，
    因为那会把「供应商不支持工具调用」伪装成「模型给了一段文字」。
    """
    _, _, model = _require_settings()
    body: dict[str, Any] = {
        "model": model,
        "temperature": temperature,
        "messages": messages,
    }
    if tools is not None:
        body["tools"] = tools
        body["tool_choice"] = tool_choice
    if response_format is not None:
        body["response_format"] = response_format
    requested_at = datetime.now(UTC).isoformat()
    envelope = _post(body, idempotency_key=idempotency_key)
    responded_at = datetime.now(UTC).isoformat()
    try:
        choice = envelope["choices"][0]
        message = choice["message"]
    except (KeyError, IndexError, TypeError) as exc:
        logger.warning("远程大模型响应结构无效 error_type=%s", type(exc).__name__)
        raise BusinessError(
            "LLM_CONTRACT_INVALID",
            "远程大模型返回内容不符合调用契约",
            status_code=502,
        ) from exc
    if not isinstance(message, dict):
        raise BusinessError(
            "LLM_CONTRACT_INVALID",
            "远程大模型返回的消息不是对象",
            status_code=502,
        )
    finish_reason = choice.get("finish_reason")
    content = message.get("content")
    refusal = message.get("refusal")
    raw_calls = message.get("tool_calls")

    if raw_calls is not None:
        if not isinstance(raw_calls, list):
            raise BusinessError(
                "LLM_TOOL_RESPONSE_INVALID",
                "远程大模型返回的 tool_calls 不是数组",
                status_code=502,
            )
        calls = tuple(_parse_tool_call(entry, index=index) for index, entry in enumerate(raw_calls))
        if calls:
            return ModelTurn(
                kind=TURN_TOOL_CALLS,
                content=content if isinstance(content, str) else None,
                tool_calls=calls,
                finish_reason=finish_reason,
                model=envelope.get("model") or model,
                response_id=envelope.get("id"),
                requested_at=requested_at,
                responded_at=responded_at,
                raw_message=message,
            )
    if isinstance(refusal, str) and refusal.strip():
        return ModelTurn(
            kind=TURN_REFUSAL,
            content=content if isinstance(content, str) else None,
            tool_calls=(),
            finish_reason=finish_reason,
            model=envelope.get("model") or model,
            response_id=envelope.get("id"),
            requested_at=requested_at,
            responded_at=responded_at,
            raw_message=message,
            refusal=refusal,
        )
    if finish_reason == "stop" and isinstance(content, str):
        return ModelTurn(
            kind=TURN_CONTENT,
            content=content,
            tool_calls=(),
            finish_reason=finish_reason,
            model=envelope.get("model") or model,
            response_id=envelope.get("id"),
            requested_at=requested_at,
            responded_at=responded_at,
            raw_message=message,
        )
    if tools is not None:
        # 带 tools 却既没有 tool_calls、也不是 stop+content：
        # 供应商可能根本不支持工具调用。明确报出，绝不降级成文本解析。
        raise BusinessError(
            "LLM_TOOL_CALL_UNSUPPORTED",
            "远程大模型没有返回可用的工具调用，且响应不符合文本结论契约；"
            "当前模型或供应商可能不支持工具调用，系统不会降级为普通文本解析",
            status_code=502,
            details={"finish_reason": finish_reason, "has_content": isinstance(content, str)},
        )
    return ModelTurn(
        kind=TURN_ERROR,
        content=content if isinstance(content, str) else None,
        tool_calls=(),
        finish_reason=finish_reason,
        model=envelope.get("model") or model,
        response_id=envelope.get("id"),
        requested_at=requested_at,
        responded_at=responded_at,
        raw_message=message,
    )


