"""远程大模型 HTTP 网关：把请求发给 OpenAI 兼容的 chat/completions 端点。

这里是**唯一**与远程模型通信的出口。调用方只提供业务 payload 与系统提示词，
拿回已解析成 dict 的 JSON；供应商的地址、密钥、超时与响应格式细节都不外泄。

两种调用方式
------------
- `call_json()`：只收发 JSON 文本。既有三条路径（规则转换、字段抽取、语义判断）
  继续用它，行为不变。
- `call_chat()` / `call_with_tools()`：完整的 Chat Completions 契约，
  支持 `tools` / `tool_choice` 与 `tool_calls` 解析，以及工具结果回填后的继续推理。
  **两者不互相降级**：`call_with_tools` 拿不到工具调用时明确报错，
  绝不退回把 tool call 当普通 JSON 文本解析。

供应商限制（不直观，改动前先读）：

- 端点可能是 baseurl 本身（已带 `/chat/completions`），也可能是 baseurl 下的
  标准路径，两种写法都必须接受。
- 响应可能是纯 JSON，也可能被 Markdown 代码块包裹；后者不是契约变化，
  而是模型常见的输出习惯，按同一份内容解析。
- 配置缺失（baseurl / apikey / model）时明确报 503，**不**退回本地规则或空结果：
  一次配置错误必须暴露成配置错误，不能伪装成「转换完成但没有内容」。
- `tool_calls[].function.arguments` 是 **JSON 字符串**，需要二次解析；
  它不是对象，也不能当作普通文本直接使用。

本模块不依赖 domain 的规则契约，只做「发请求、取响应」这件事。
"""

import json
import logging
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Callable

import httpx

from ...config import get_settings
from ...errors import BusinessError


logger = logging.getLogger(__name__)

# 模型响应的种类。它们是**互斥**的四种结果，调用方必须逐类处理：
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


@dataclass(frozen=True)
class ToolCall:
    """一次工具调用请求。

    `arguments_raw` 与 `arguments` 都保留：原始字符串是**模型的真实输出**，
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

    `raw_message` 是模型原样返回的 message 对象：回填工具结果时必须**原样**把它
    放回 assistant 消息，不能重新构造——重新构造会让 `tool_call_id` 对不上，
    供应商会拒绝或按另一条调用处理。
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

    @property
    def has_tool_calls(self) -> bool:
        return bool(self.tool_calls)


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


def _post(body: dict[str, Any]) -> dict[str, Any]:
    """调用 chat/completions；临时故障最多重试三次，最终失败明确抛出。"""
    baseurl, api_key, _ = _require_settings()
    settings = get_settings()
    max_attempts = len(LLM_RETRY_DELAYS_SECONDS) + 1
    with httpx.Client(timeout=settings.request_timeout_seconds) as client:
        for attempt in range(1, max_attempts + 1):
            try:
                response = client.post(
                    _endpoint(baseurl),
                    headers={"Authorization": f"Bearer {api_key}"},
                    json=body,
                )
                response.raise_for_status()
                break
            except httpx.TimeoutException as exc:
                if attempt < max_attempts:
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
                logger.warning(
                    "远程大模型请求超时，重试已用尽 attempt=%s retry_count=%s error_type=%s",
                    attempt,
                    len(LLM_RETRY_DELAYS_SECONDS),
                    type(exc).__name__,
                )
                raise BusinessError(
                    "LLM_TIMEOUT",
                    "远程大模型请求超时",
                    status_code=504,
                    details={"service": "LLM"},
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
                if attempt < max_attempts:
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
    try:
        return response.json()
    except ValueError as exc:
        logger.warning("远程大模型响应不是有效 JSON")
        raise BusinessError(
            "LLM_CONTRACT_INVALID",
            "远程大模型返回内容不符合规则转换契约",
            status_code=502,
        ) from exc


def call_json(payload: dict[str, Any], system_prompt: str) -> dict[str, Any]:
    """要求模型以 JSON 对象作答的单轮调用（不带工具，也不做工具循环）。"""
    baseurl, api_key, model = _require_settings()
    request_body = {
        "model": model,
        "temperature": 0,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ],
        "response_format": {"type": "json_object"},
    }
    envelope = _post(request_body)
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
) -> ModelTurn:
    """发一次 Chat Completions 请求，返回明确的响应契约。

    与 `call_json` 的关键差别：**不把响应当成 JSON 文本**。`tool_calls` 是结构化字段，
    按它自己的契约解析；只有 `content` 才可能是 JSON 文本。

    请求带 `tools` 时，响应必须能归类为四种结果之一。归类不了就报
    `LLM_TOOL_CALL_UNSUPPORTED`——**不退回**按普通文本解析，
    因为那会把「供应商不支持工具调用」伪装成「模型给了一段文字」。
    """
    baseurl, api_key, model = _require_settings()
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
    envelope = _post(body)
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


def tool_result_message(tool_call: ToolCall, result: dict[str, Any]) -> dict[str, Any]:
    """一次工具结果的回填消息。

    `tool_call_id` 与请求逐字对应：这是模型把结果对回它那次调用的唯一依据。
    结果序列化成 JSON 文本（Chat Completions 的 tool 消息 content 是字符串）。
    """
    return {
        "role": "tool",
        "tool_call_id": tool_call.tool_call_id,
        "content": json.dumps(result, ensure_ascii=False),
    }


@dataclass
class ToolLoopResult:
    """工具调用循环的最终结果。

    `rounds` 与 `tool_call_log` 一并返回：调用方要能回答
    「这次结论是哪几次工具调用得到的」，而不是只知道一个最终文本。
    """

    turn: ModelTurn
    messages: list[dict[str, Any]]
    rounds: int
    tool_call_log: list[dict[str, Any]] = field(default_factory=list)


def call_with_tools(
    messages: list[dict[str, Any]],
    *,
    tools: list[dict[str, Any]],
    invoke_tool: Callable[[ToolCall], dict[str, Any]],
    max_rounds: int | None = None,
    tool_choice: Any = "auto",
    response_format: dict[str, Any] | None = None,
) -> ToolLoopResult:
    """工具调用循环：模型请求工具 → 执行 → 回填结果 → 继续推理。

    `invoke_tool` 由调用方注入：它负责校验工具、参数与任务范围，并调真实 handler
    （见 `services.tool_service`）。网关只负责协议层——发请求、解析 tool_calls、
    回填结果，不做工具本身的校验。

    **`max_rounds` 不是静默兜底**：达到上限时抛 `LLM_TOOL_LOOP_LIMIT` 并带上完整
    的调用历史，绝不「跑到上限就当通过」。`None` 或 `0` 表示不设上限，
    由调用方自行控制终止。

    **轮次的定义与图路径完全一致：一次模型调用算一轮**（不是「一批工具算一轮」）。
    一次模型调用可以请求多个工具，它们属于同一轮。因此：

    - 额度用尽时不再发起新的模型调用；
    - 某次模型调用是最后一轮、而它仍请求工具时，**这一批工具也不执行**，
      直接按上限失败——图路径的 `graph_call_model` 用的是同一条规则，
      两条路径不会对「还能不能再跑一批」给出不同答案。
    """
    if max_rounds is None:
        max_rounds = get_settings().llm_tool_max_rounds
    conversation = list(messages)
    log: list[dict[str, Any]] = []
    rounds = 0
    while True:
        if max_rounds and rounds >= max_rounds:
            raise _loop_limit_error(max_rounds, rounds, [])
        turn = call_chat(
            conversation,
            tools=tools,
            tool_choice=tool_choice,
            response_format=response_format,
        )
        rounds += 1
        if turn.kind != TURN_TOOL_CALLS:
            return ToolLoopResult(turn=turn, messages=conversation, rounds=rounds, tool_call_log=log)
        if max_rounds and rounds >= max_rounds:
            raise _loop_limit_error(max_rounds, rounds, [call.tool_name for call in turn.tool_calls])
        # assistant 消息必须**原样**回填模型返回的 tool_calls：重新构造会让
        # tool_call_id 与供应商记录的调用对不上。
        conversation.append(
            {
                "role": "assistant",
                "content": turn.content or "",
                "tool_calls": turn.raw_message.get("tool_calls"),
            }
        )
        for call in turn.tool_calls:
            result = invoke_tool(call)
            log.append(
                {
                    "round": rounds,
                    "tool_call_id": call.tool_call_id,
                    "tool_name": call.tool_name,
                    "arguments": call.arguments,
                    "result": result,
                }
            )
            conversation.append(tool_result_message(call, result))


def _loop_limit_error(max_rounds: int, rounds: int, pending: list[str]) -> BusinessError:
    """到达轮次上限时的显式错误：带上已用轮次与**没有执行**的那批调用。

    `pending` 为空表示额度在调用模型之前就用尽了；非空表示这一轮的模型调用
    已经用掉最后一个额度，它请求的这批工具没有被执行。
    """
    return BusinessError(
        "LLM_TOOL_LOOP_LIMIT",
        f"工具调用达到上限（{max_rounds} 轮）仍未形成结论，本次未完成；"
        "系统不会把「跑到上限」当作结论，也不再执行下一批工具",
        status_code=502,
        details={
            "max_rounds": max_rounds,
            "rounds": rounds,
            "pending_tool_calls": list(pending),
            "not_executed": bool(pending),
        },
    )


