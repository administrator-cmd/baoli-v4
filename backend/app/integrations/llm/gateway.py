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

import atexit
import itertools
import json
import logging
import os
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx

from app.common.exceptions import BusinessError
from app.core.config import get_settings
from app.integrations.llm.pacing import LlmRequestPacer, http_retry_delay, retry_after_seconds, wait_before_retry

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

# 当前进程内的审核、编制、模板分析及状态探测共用窗口。
_request_pacer = LlmRequestPacer()

# 流式接收期间按固定间隔输出进度；该间隔只影响日志，不参与任何超时判断。
LLM_PROGRESS_LOG_INTERVAL_SECONDS = 30.0


@dataclass(frozen=True)
class RequestLogContext:
    """调用方已持有的业务上下文，只用于日志展示；缺项保持 None，不查库、不猜。"""

    task_name: str | None = None
    material_name: str | None = None
    operation: str | None = None
    field_count: int | None = None


_request_counter = itertools.count(1)
_request_counter_lock = threading.Lock()


def _next_request_short_id() -> str:
    """服务进程内递增的短编号；同一次逻辑请求的所有重试共用一个编号。"""
    with _request_counter_lock:
        return f"R{next(_request_counter)}"


def _context_text(context: RequestLogContext | None) -> str:
    if context is None:
        return ""
    parts = []
    if context.task_name:
        parts.append(f"任务={context.task_name}")
    if context.material_name:
        parts.append(f"材料={context.material_name}")
    if context.operation:
        parts.append(f"操作={context.operation}")
    if context.field_count is not None:
        parts.append(f"字段数={context.field_count}")
    return f" {' '.join(parts)}" if parts else ""


def _seconds_since(now: float, moment: float | None) -> str:
    """距离某个时刻的秒数；还没有发生过时明确写「未收到」，不拿别的数据代替。"""
    return f"{now - moment:.3f}" if moment is not None else "未收到"


def _since_ago(now: float, moment: float | None, *, missing: str) -> str:
    return f"{now - moment:.2f}秒前" if moment is not None else missing


def _tool_calls_text(tool_calls: dict[int, dict[str, Any]]) -> str:
    """逐个工具调用的 index、名称和累计参数字符数；名称未到时明确写未知。"""
    if not tool_calls:
        return ""
    parts = [
        f"{index}:{call.get('name') or '未知'}/{call.get('argument_chars', 0)}字符"
        for index, call in sorted(tool_calls.items())
    ]
    return f"工具=[{', '.join(parts)}]"


def _finish_reason_label(reason: str, *, has_tool_calls: bool) -> str:
    if has_tool_calls or reason == "tool_calls":
        return "工具调用"
    if reason == "stop":
        return "正常结束"
    if reason == "length":
        return "输出截断"
    if reason == "content_filter":
        return "内容过滤"
    return reason or "未知"


def _end_type_text(finish_reasons: list[str], *, has_tool_calls: bool) -> str:
    if not finish_reasons:
        return "未知"
    labels = list(dict.fromkeys(
        _finish_reason_label(reason, has_tool_calls=has_tool_calls)
        for reason in finish_reasons
    ))
    return "/".join(labels)


def _usage_text(envelope: dict[str, Any]) -> str:
    """供应商未返回 usage 时明确写未返回；不按字符数估算 token。"""
    usage = envelope.get("usage")
    if usage is None:
        return "用量=未返回"
    if not isinstance(usage, dict):
        return "用量=格式无效"
    prompt = usage.get("prompt_tokens")
    completion = usage.get("completion_tokens")
    if prompt is None and completion is None:
        return "用量=未返回"
    prompt_text = f"输入token={prompt}" if prompt is not None else "输入token=未返回"
    completion_text = f"输出token={completion}" if completion is not None else "输出token=未返回"
    return f"{prompt_text} {completion_text}"


def _failure_reason(exc: BaseException) -> str:
    if isinstance(exc, BusinessError):
        return f"{exc.code} {exc.message}"
    if isinstance(exc, httpx.HTTPStatusError):
        return f"HTTP {exc.response.status_code}"
    return type(exc).__name__


class _StreamProgress:
    """一次流式尝试的接收统计。

    主线程解析流时更新计数，后台线程按间隔读快照写日志。计数只来自真实收到的
    SSE 行与字符串片段：注释行只算心跳，空 choices、仅 role 的数据块不产生正文，
    不按 token 估算，也不改变解析与错误传播。
    """

    def __init__(
        self, *, short_id: str, identity: str, attempt: int, started: float,
        context_text: str = "",
    ) -> None:
        self.short_id = short_id
        self.identity = identity
        self.context_text = context_text
        self.attempt = attempt
        self.started = started
        self._lock = threading.Lock()
        self._last_network_at: float | None = None
        self._last_output_at: float | None = None
        self._comment_events = 0
        self._json_chunks = 0
        self._content_chars = 0
        self._reasoning_chars = 0
        self._tool_argument_chars = 0
        self._tool_calls: dict[int, dict[str, Any]] = {}
        self._finish_reason = False
        self._done = False

    def attempt_text(self) -> str:
        return f" 尝试={self.attempt}" if self.attempt > 1 else ""

    def elapsed(self) -> float:
        return time.monotonic() - self.started

    def network_line(self, *, comment: bool) -> None:
        """收到一行网络数据；只有供应商明确发来的注释行才计入心跳。"""
        with self._lock:
            self._last_network_at = time.monotonic()
            if comment:
                self._comment_events += 1

    def json_chunk(self) -> None:
        with self._lock:
            self._json_chunks += 1

    def output(self, *, content: int, reasoning: int) -> None:
        """累计正文与推理的字符串片段长度；空片段不算有效输出。"""
        if not content and not reasoning:
            return
        with self._lock:
            self._content_chars += content
            self._reasoning_chars += reasoning
            self._last_output_at = time.monotonic()

    def tool_fragment(self, *, index: int, name: str, arguments: str) -> None:
        """按 index 累计工具名称和参数字符数；只有 index 时也登记，名称写未知。"""
        with self._lock:
            call = self._tool_calls.setdefault(index, {"name": "", "argument_chars": 0})
            if name:
                call["name"] += name
            if arguments:
                chars = len(arguments)
                call["argument_chars"] += chars
                self._tool_argument_chars += chars
            if name or arguments:
                self._last_output_at = time.monotonic()

    def tool_call_count(self) -> int:
        with self._lock:
            return len(self._tool_calls)

    def mark_finish_reason(self) -> None:
        with self._lock:
            self._finish_reason = True

    def mark_done(self) -> None:
        with self._lock:
            self._done = True

    def stats_text(self) -> str:
        """INFO 用的短摘要；零值计数不展开，工具只给数量和参数总量。"""
        now = time.monotonic()
        with self._lock:
            last_network = self._last_network_at
            last_output = self._last_output_at
            content_chars = self._content_chars
            reasoning_chars = self._reasoning_chars
            tool_argument_chars = self._tool_argument_chars
            comment_events = self._comment_events
            tool_call_count = len(self._tool_calls)
        parts = [
            f"最近网络={_since_ago(now, last_network, missing='未收到')}",
            f"有效输出={_since_ago(now, last_output, missing='无')}",
        ]
        if content_chars:
            parts.append(f"正文={content_chars}字符")
        if reasoning_chars:
            parts.append(f"推理={reasoning_chars}字符")
        if tool_call_count:
            parts.append(f"工具数={tool_call_count}")
        if tool_argument_chars:
            parts.append(f"工具参数={tool_argument_chars}字符")
        if comment_events:
            parts.append(f"心跳={comment_events}")
        return " ".join(parts)

    def detailed_summary(self) -> str:
        """DEBUG 用的完整统计；保留所有计数和工具明细。"""
        now = time.monotonic()
        with self._lock:
            last_network = self._last_network_at
            last_output = self._last_output_at
            comment_events = self._comment_events
            json_chunks = self._json_chunks
            content_chars = self._content_chars
            reasoning_chars = self._reasoning_chars
            tool_argument_chars = self._tool_argument_chars
            finish_reason = self._finish_reason
            done = self._done
            tool_calls = {index: dict(call) for index, call in self._tool_calls.items()}
        text = (
            f"elapsed_seconds={now - self.started:.3f}"
            f" since_last_network_seconds={_seconds_since(now, last_network)}"
            f" since_last_output_seconds={_seconds_since(now, last_output)}"
            f" comment_events={comment_events}"
            f" json_chunks={json_chunks}"
            f" content_chars={content_chars}"
            f" reasoning_chars={reasoning_chars}"
            f" tool_argument_chars={tool_argument_chars}"
            f" finish_reason={str(finish_reason).lower()}"
            f" done={str(done).lower()}"
        )
        tool_text = _tool_calls_text(tool_calls)
        return f"{text} {tool_text}" if tool_text else text

    def log_progress(self) -> None:
        logger.info(
            "LLM进行中 请求=%s%s 耗时=%.1f秒 %s",
            self.short_id, self.attempt_text(), self.elapsed(), self.stats_text(),
        )
        logger.debug(
            "LLM详细统计 请求=%s 标识=%s 尝试=%s 阶段=进行中 %s",
            self.short_id, self.identity, self.attempt, self.detailed_summary(),
        )

    def log_completion(self, envelope: dict[str, Any]) -> None:
        finish_reasons = [
            str(choice.get("finish_reason"))
            for choice in envelope.get("choices") or []
            if isinstance(choice, dict) and choice.get("finish_reason") is not None
        ]
        tool_count = self.tool_call_count()
        parts = [
            f"LLM完成 请求={self.short_id}{self.attempt_text()}",
            f"耗时={self.elapsed():.1f}秒",
            f"结束类型={_end_type_text(finish_reasons, has_tool_calls=bool(tool_count))}",
        ]
        if tool_count:
            parts.append(f"工具数={tool_count}")
        parts.append(_usage_text(envelope))
        logger.info(" ".join(parts))
        logger.debug(
            "LLM详细统计 请求=%s 标识=%s 尝试=%s 阶段=完成 模型=%s %s",
            self.short_id, self.identity, self.attempt,
            envelope.get("model") or "未返回", self.detailed_summary(),
        )

    def log_abnormal_end(self, exc: BaseException) -> None:
        logger.warning(
            "LLM失败 请求=%s%s 耗时=%.1f秒 原因=%s %s",
            self.short_id, self.attempt_text(), self.elapsed(),
            _failure_reason(exc), self.stats_text(),
        )
        logger.debug(
            "LLM详细统计 请求=%s 标识=%s 尝试=%s 阶段=失败 error_type=%s %s",
            self.short_id, self.identity, self.attempt,
            type(exc).__name__, self.detailed_summary(),
        )


def _stream_progress_loop(progress: _StreamProgress, stop: threading.Event) -> None:
    """等待网络数据期间仍按期输出进度；`stop` 置位后线程立即结束。"""
    while not stop.wait(LLM_PROGRESS_LOG_INTERVAL_SECONDS):
        progress.log_progress()


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
        details={"service": "LLM", "error_type": type(exc).__name__, "result_unknown": True},
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


def _upstream_stream_error(chunk: dict[str, Any], response: httpx.Response) -> dict[str, Any]:
    """保留上游流内 error 中真实存在的错误码、说明、响应 ID 与请求 ID；缺失字段不补造。

    数据块顶层的 `id` 是响应 ID，响应头 `x-request-id` 是请求 ID；两者独立保留，
    任何一个缺失都不拿另一个冒充，也不猜测供应商的其他字段。
    """
    error = chunk.get("error")
    detail: dict[str, Any] = {}
    if isinstance(error, dict):
        for key in ("code", "type", "message", "param"):
            value = error.get(key)
            if isinstance(value, str) and value.strip():
                detail[key] = value.strip()
            elif isinstance(value, int) and not isinstance(value, bool):
                detail[key] = value
    elif isinstance(error, str) and error.strip():
        detail["message"] = error.strip()
    response_id = chunk.get("id")
    if isinstance(response_id, str) and response_id.strip():
        detail["response_id"] = response_id.strip()
    elif isinstance(response_id, int) and not isinstance(response_id, bool):
        detail["response_id"] = response_id
    request_id = response.headers.get("x-request-id")
    if isinstance(request_id, str) and request_id.strip():
        detail["request_id"] = request_id.strip()
    return detail


def _sse_data(
    response: httpx.Response, *, deadline: float, max_seconds: float, attempt: int, started: float,
    progress: _StreamProgress,
) -> Iterator[str]:
    """按 SSE 事件边界读取 data，保留多行数据，忽略协议定义的注释与控制字段。

    总时限在每一行到达时检查，包括被忽略的心跳注释：只在解析出 data 事件时检查时，
    持续发送注释心跳的连接可以让读取超时与总时限都不触发。

    每一行都计入「最后收到网络数据」时间，注释行另外计入心跳数；统计只观察，不介入读取。
    """
    data_lines: list[str] = []
    for line in response.iter_lines():
        progress.network_line(comment=line.startswith(":"))
        if time.monotonic() >= deadline:
            logger.warning(
                "远程大模型流式请求超过总时限 请求=%s 尝试=%s max_seconds=%s elapsed_seconds=%.3f",
                progress.short_id, attempt, max_seconds, time.monotonic() - started,
            )
            raise BusinessError(
                "LLM_TIMEOUT", "远程大模型请求超过总时限", status_code=504,
                details={"service": "LLM", "max_seconds": max_seconds, "result_unknown": True},
            )
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


def _delta_output_stats(
    delta: dict[str, Any],
) -> tuple[int, int, list[tuple[int, str, str]]]:
    """按真实收到的字符串片段统计正文、推理长度，并返回工具调用的分段信息。

    只读取 delta 中明确存在的字段，非字符串和空片段都不计入；不估算 token。
    调用点在 `_merge_stream_delta` 之后，结构已按流式契约校验过。
    """
    content = delta.get("content")
    content_chars = len(content) if isinstance(content, str) else 0
    reasoning_chars = 0
    for key in ("reasoning_content", "reasoning"):
        value = delta.get(key)
        if isinstance(value, str):
            reasoning_chars += len(value)
    tool_fragments: list[tuple[int, str, str]] = []
    entries = delta.get("tool_calls")
    if isinstance(entries, list):
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            index = entry.get("index")
            if type(index) is not int or index < 0:
                continue
            name = ""
            arguments = ""
            function = entry.get("function")
            if isinstance(function, dict):
                value = function.get("name")
                if isinstance(value, str):
                    name = value
                value = function.get("arguments")
                if isinstance(value, str):
                    arguments = value
            tool_fragments.append((index, name, arguments))
    return content_chars, reasoning_chars, tool_fragments


def _read_stream(
    response: httpx.Response, *, attempt: int, started: float, deadline: float,
    max_seconds: float, progress: _StreamProgress,
) -> dict[str, Any]:
    """接收完整流并还原 Chat Completions 响应；未正常结束的流不能作为业务结果。

    总时限由 `_sse_data` 在读取每一行时检查（含被忽略的心跳注释）；只要数据还在到达就继续
    接收，慢不等于卡住。
    """
    if response.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "text/event-stream":
        raise _stream_invalid("接口未返回 text/event-stream")
    envelope: dict[str, Any] = {}
    choices: dict[int, dict[str, Any]] = {}
    tool_calls: dict[int, dict[int, dict[str, Any]]] = {}
    chunks = 0
    done = False
    for data in _sse_data(
        response, deadline=deadline, max_seconds=max_seconds, attempt=attempt, started=started,
        progress=progress,
    ):
        if data == "[DONE]":
            progress.mark_done()
            done = True
            break
        try:
            chunk = json.loads(data)
        except ValueError as exc:
            raise _stream_invalid("data 不是有效 JSON") from exc
        if not isinstance(chunk, dict):
            raise _stream_invalid("data 不是 JSON 对象")
        chunks += 1
        progress.json_chunk()
        if chunks == 1:
            logger.info(
                "LLM首块 请求=%s%s 耗时=%.1f秒",
                progress.short_id, progress.attempt_text(), time.monotonic() - started,
            )
        if "error" in chunk:
            upstream = _upstream_stream_error(chunk, response)
            logger.warning(
                "远程大模型流中返回错误 请求=%s 尝试=%s code=%s type=%s response_id=%s request_id=%s message=%s",
                progress.short_id, attempt,
                upstream.get("code", "未返回"),
                upstream.get("type", "未返回"),
                upstream.get("response_id", "未返回"),
                upstream.get("request_id", "未返回"),
                upstream.get("message", "未返回"),
            )
            # 请求已被接受并进入流式阶段：是否已生成内容无法确认，不能声称未产生费用。
            # 上游真实返回的错误码、说明与请求 ID 保留在 details，不吞成通用失败。
            details: dict[str, Any] = {
                "service": "LLM",
                "result_unknown": True,
                "request_short_id": progress.short_id,
                "request_identity": progress.identity,
                "attempt": attempt,
                "upstream": upstream,
            }
            if progress.context_text:
                details["request_context"] = progress.context_text
            raise BusinessError(
                "LLM_EXTERNAL_FAILED", "远程大模型流式响应返回错误", status_code=502,
                details=details,
            )
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
            content_chars, reasoning_chars, tool_fragments = _delta_output_stats(delta)
            progress.output(content=content_chars, reasoning=reasoning_chars)
            for call_index, call_name, call_arguments in tool_fragments:
                progress.tool_fragment(
                    index=call_index, name=call_name, arguments=call_arguments,
                )
            finish_reason = entry.get("finish_reason")
            if finish_reason is not None:
                if not isinstance(finish_reason, str) or (
                    choice["finish_reason"] is not None and choice["finish_reason"] != finish_reason
                ):
                    raise _stream_invalid("finish_reason 无效或发生变化")
                choice["finish_reason"] = finish_reason
                progress.mark_finish_reason()
    if not done or not choices or any(choice["finish_reason"] is None for choice in choices.values()):
        logger.warning(
            "远程大模型流未完整结束 请求=%s 尝试=%s json_chunks=%s done=%s "
            "elapsed_seconds=%.3f",
            progress.short_id, attempt, chunks, done, time.monotonic() - started,
        )
        raise BusinessError(
            "LLM_STREAM_INCOMPLETE", "远程大模型流式响应未完整结束，未保存本次生成结果", status_code=502,
            details={"service": "LLM", "result_unknown": True},
        )
    if any(choice["finish_reason"] == "length" for choice in choices.values()):
        logger.warning(
            "远程大模型输出被截断 请求=%s 尝试=%s finish_reason=length",
            progress.short_id, attempt,
        )
        raise BusinessError("LLM_OUTPUT_TRUNCATED", "远程大模型输出被截断，未保存本次生成结果", status_code=502)
    for index, choice in choices.items():
        if tool_calls[index]:
            choice["message"]["tool_calls"] = [tool_calls[index][key] for key in sorted(tool_calls[index])]
    envelope["choices"] = [choices[index] for index in sorted(choices)]
    progress.log_completion(envelope)
    return envelope


_client_lock = threading.Lock()
_shared_client: httpx.Client | None = None


def _http_client() -> httpx.Client:
    """进程内唯一的 HTTP 客户端：连接池跨规则调用保留，进程退出时关闭。

    httpx.Client 线程安全；每次请求仍按当次剩余时限传 timeout，不受连接复用影响。
    """
    global _shared_client
    with _client_lock:
        if _shared_client is None:
            client = httpx.Client()
            atexit.register(client.close)
            _shared_client = client
        return _shared_client


@dataclass(frozen=True)
class ModelProbeResult:
    """一次真实模型连通探测的结果。

    `ok` 只由本次请求的 HTTP 响应与响应结构决定；配置缺失、超时、鉴权失败和
    网络错误都保留原始原因，不用「已配置」冒充「已连通」。
    """

    ok: bool
    latency_ms: int
    error_code: str | None = None
    error_message: str | None = None


# 状态页的探测必须快速返回，不能沿用业务请求的长超时；1 个输出 token 足以验证
# 地址、鉴权、模型名和响应契约。
LLM_PROBE_TIMEOUT_SECONDS = 10.0
LLM_PROBE_MAX_TOKENS = 1


def _elapsed_ms(started: float) -> int:
    return max(0, int((time.monotonic() - started) * 1000))


def probe_connection(timeout_seconds: float = LLM_PROBE_TIMEOUT_SECONDS) -> ModelProbeResult:
    """用一次最小 chat 请求验证大模型渠道。

    健康检查只读、可重复、不重试、不流式，也不消耗业务级 token；2xx 且响应
    含 `choices` 才判定连通。任何失败原样返回错误码与中文原因。
    """
    started = time.monotonic()
    try:
        baseurl, api_key, model = _require_settings()
    except BusinessError as exc:
        return ModelProbeResult(False, _elapsed_ms(started), exc.code, exc.message)

    try:
        settings = get_settings()
        deadline = started + timeout_seconds
        with _request_pacer.slot(interval_seconds=settings.llm_request_interval_seconds, deadline=deadline):
            response = _http_client().post(
                _endpoint(baseurl),
                headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
                json={
                    "model": model,
                    "messages": [{"role": "user", "content": "ping"}],
                    "max_tokens": LLM_PROBE_MAX_TOKENS,
                    "stream": False,
                },
                timeout=max(0.001, deadline - time.monotonic()),
            )
            if response.status_code in LLM_RETRYABLE_HTTP_STATUSES:
                _request_pacer.defer(http_retry_delay(
                    retry_after=response.headers.get("Retry-After"), attempt=1,
                    base_seconds=settings.llm_retry_base_seconds,
                ))
            response.raise_for_status()
    except BusinessError as exc:
        return ModelProbeResult(False, _elapsed_ms(started), exc.code, exc.message)
    except httpx.TimeoutException:
        logger.warning("大模型连通探测超时 timeout_seconds=%s", timeout_seconds)
        return ModelProbeResult(
            False, _elapsed_ms(started), "LLM_TIMEOUT",
            f"连接超时（{timeout_seconds:g} 秒）",
        )
    except httpx.HTTPStatusError as exc:
        status_code = exc.response.status_code
        if status_code in {401, 403}:
            code, message = "LLM_AUTH_FAILED", f"鉴权失败（HTTP {status_code}）"
        else:
            code, message = "LLM_EXTERNAL_FAILED", f"服务返回 HTTP {status_code}"
        logger.warning("大模型连通探测失败 code=%s status_code=%s", code, status_code)
        return ModelProbeResult(False, _elapsed_ms(started), code, message)
    except httpx.RequestError as exc:
        logger.warning("大模型连通探测网络失败 error_type=%s", type(exc).__name__)
        return ModelProbeResult(
            False, _elapsed_ms(started), "LLM_CONNECTION_FAILED",
            f"网络请求失败（{type(exc).__name__}）",
        )

    try:
        payload = response.json()
    except ValueError:
        payload = None
    if not isinstance(payload, dict) or not isinstance(payload.get("choices"), list) or not payload["choices"]:
        logger.warning("大模型连通探测响应不符合 chat 完成契约")
        return ModelProbeResult(
            False, _elapsed_ms(started), "LLM_CONTRACT_INVALID",
            "响应不是有效的 chat 完成",
        )
    return ModelProbeResult(True, _elapsed_ms(started))


def _post(
    body: dict[str, Any], *, call: str, idempotency_key: str | None = None,
    log_context: RequestLogContext | None = None,
) -> dict[str, Any]:
    """所有模型业务入口统一排队；排队时间也计入本次总时限。"""
    _require_settings()
    settings = get_settings()
    short_id = _next_request_short_id()
    deadline = time.monotonic() + settings.llm_max_seconds
    with _request_pacer.slot(
        interval_seconds=settings.llm_request_interval_seconds, deadline=deadline,
        on_wait=lambda: logger.info("LLM等待 请求=%s%s 原因=请求排队或冷却", short_id, _context_text(log_context)),
    ):
        return _post_attempts(
            body, call=call, idempotency_key=idempotency_key, log_context=log_context,
            short_id=short_id, deadline=deadline,
        )


def _post_attempts(
    body: dict[str, Any], *, call: str, short_id: str, deadline: float,
    idempotency_key: str | None = None, log_context: RequestLogContext | None = None,
) -> dict[str, Any]:
    """流式调用 chat/completions；沿用临时故障重试，每次独立组装响应。

    ``idempotency_key`` 标识一次逻辑模型请求，而不是某一次网络尝试；
    同一逻辑请求的自动重试始终复用它，供应商支持该契约时可避免重复生成。
    ``call`` 和 ``log_context`` 只用于日志归属，不进入请求体。
    """
    baseurl, api_key, _ = _require_settings()
    settings = get_settings()
    max_attempts = len(LLM_RETRY_DELAYS_SECONDS) + 1
    request_started = time.monotonic()
    # 短编号只在当前进程内递增，同一次逻辑请求的所有重试共用它；
    # 完整标识沿用幂等键，没有时生成本地诊断编号，只写日志，不发给供应商。
    identity = idempotency_key or f"local-{uuid.uuid4().hex[:12]}"
    logger.info("LLM开始 请求=%s%s 标识=%s", short_id, _context_text(log_context), identity)
    logger.debug(
        "LLM请求映射 请求=%s 标识=%s 调用=%s 进程=%s 最大尝试=%s 读取超时=%s秒 总时限=%s秒",
        short_id, identity, call, os.getpid(), max_attempts,
        settings.llm_timeout_seconds, settings.llm_max_seconds,
    )
    # 总时限只兜住永不结束的流；读取超时按「连续多久收不到数据」判定卡住，
    # 仍在持续出块的长响应不会被总时长截断。
    client = _http_client()
    for attempt in range(1, max_attempts + 1):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            logger.warning(
                "LLM失败 请求=%s 耗时=%.1f秒 原因=超过总时限 max_seconds=%s "
                "最近网络=未收到 有效输出=无",
                short_id, time.monotonic() - request_started, settings.llm_max_seconds,
            )
            raise BusinessError(
                "LLM_TIMEOUT", "远程大模型请求超过总时限", status_code=504,
                details={"service": "LLM", "max_seconds": settings.llm_max_seconds},
            )
        started = time.monotonic()
        progress = _StreamProgress(
            short_id=short_id, identity=identity, attempt=attempt, started=started,
            context_text=_context_text(log_context).strip(),
        )
        stop_progress = threading.Event()
        progress_thread = threading.Thread(
            target=_stream_progress_loop,
            args=(progress, stop_progress),
            name=f"llm-stream-progress-{attempt}",
            daemon=True,
        )
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Accept": "text/event-stream",
        }
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        progress_thread.start()
        try:
            try:
                with client.stream(
                    "POST",
                    _endpoint(baseurl),
                    headers=headers,
                    json={
                        **body,
                        "stream": True,
                        # 要求供应商在流末尾返回 usage，用于核对单次输入 token 规模。
                        "stream_options": {**(body.get("stream_options") or {}), "include_usage": True},
                    },
                    # 读取超时按「连续多久收不到数据」判定卡住，但不超过本次请求剩余的总时限：
                    # 上游长时间不发数据时，等待本身也不会越过总截止时间。
                    timeout=min(settings.llm_timeout_seconds, max(remaining, 0.001)),
                ) as response:
                    response.raise_for_status()
                    return _read_stream(
                        response,
                        attempt=attempt,
                        started=started,
                        deadline=deadline,
                        max_seconds=settings.llm_max_seconds,
                        progress=progress,
                    )
            except BaseException as exc:
                # 仅记录摘要后原样抛出；异常类型与传播路径不变。
                progress.log_abnormal_end(exc)
                raise
            finally:
                stop_progress.set()
                progress_thread.join(timeout=5.0)
        except httpx.TimeoutException as exc:
            if time.monotonic() >= deadline:
                # 等待本身已经用尽总时限：按总时限暴露，不再重试。
                # 连接尚未建立时不能断言请求已送达。
                delivered = not isinstance(exc, (httpx.ConnectTimeout, httpx.PoolTimeout))
                raise BusinessError(
                    "LLM_TIMEOUT", "远程大模型请求超过总时限", status_code=504,
                    details={"service": "LLM", "max_seconds": settings.llm_max_seconds,
                             "result_unknown": delivered},
                ) from exc
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
                wait_before_retry(delay, deadline=deadline)
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
            retry_after = retry_after_seconds(exc.response.headers.get("Retry-After"))
            request_id = exc.response.headers.get("x-request-id")
            details = {"service": "LLM", "http_status": status_code}
            if request_id:
                details["request_id"] = request_id
            if retry_after is not None:
                details["retry_after_seconds"] = retry_after
            if status_code in LLM_RETRYABLE_HTTP_STATUSES:
                delay = max(settings.llm_request_interval_seconds, http_retry_delay(
                    retry_after=exc.response.headers.get("Retry-After"), attempt=attempt,
                    base_seconds=settings.llm_retry_base_seconds,
                ))
                # 最后一次失败也保留冷却，其他任务或用户续跑不能立刻重新轰炸同一渠道。
                _request_pacer.defer(delay)
            if status_code in LLM_RETRYABLE_HTTP_STATUSES and attempt < max_attempts:
                logger.warning(
                    "远程大模型返回可重试 HTTP 错误，冷却后重试 status_code=%s attempt=%s max_attempts=%s delay_seconds=%.1f request_id=%s retry_after_seconds=%s",
                    status_code,
                    attempt,
                    max_attempts,
                    delay,
                    request_id, retry_after,
                )
                wait_before_retry(delay, deadline=deadline)
                continue
            logger.warning(
                "远程大模型返回 HTTP 错误 status_code=%s attempt=%s retry_count=%s request_id=%s retry_after_seconds=%s",
                status_code,
                attempt,
                attempt - 1,
                request_id, retry_after,
            )
            if status_code in {401, 403}:
                raise BusinessError(
                    "LLM_AUTH_FAILED",
                    "远程大模型鉴权失败",
                    status_code=502,
                    details=details,
                ) from exc
            raise BusinessError(
                "LLM_EXTERNAL_FAILED",
                "远程大模型服务返回错误",
                status_code=502,
                details=details,
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
                wait_before_retry(delay, deadline=deadline)
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
    idempotency_key: str | None = None, log_context: RequestLogContext | None = None,
) -> dict[str, Any]:
    """单轮请求 JSON 对象，完整接收流式文本后解析；不带工具或工具循环。

    ``idempotency_key`` 在自动重试期间保持不变，标识这一次逻辑请求。
    ``log_context`` 只用于日志展示，缺项保持省略。
    """
    _, _, model = _require_settings()
    settings = get_settings()
    request_body = {
        "model": model,
        "temperature": 0,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ],
        "response_format": {"type": "json_object"},
        "max_tokens": settings.llm_max_output_tokens,
    }
    if reasoning_effort is not None:
        request_body["reasoning_effort"] = reasoning_effort
    envelope = _post(
        request_body, call="call_json", idempotency_key=idempotency_key, log_context=log_context,
    )
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
    log_context: RequestLogContext | None = None,
) -> ModelTurn:
    """完整接收一次流式 Chat Completions 响应，返回明确的响应契约。

    与 `call_json` 的关键差别：不把响应当成 JSON 文本。`tool_calls` 是结构化字段，
    按它自己的契约解析；只有 `content` 才可能是 JSON 文本。

    请求带 `tools` 时，响应必须能归类为四种结果之一。归类不了就报
    `LLM_TOOL_CALL_UNSUPPORTED`——不退回按普通文本解析，
    因为那会把「供应商不支持工具调用」伪装成「模型给了一段文字」。
    """
    _, _, model = _require_settings()
    settings = get_settings()
    body: dict[str, Any] = {
        "model": model,
        "temperature": temperature,
        "messages": messages,
        # 不传时由供应商决定默认输出上限；显式设置避免长响应被默认值截断。
        "max_tokens": settings.llm_max_output_tokens,
    }
    if tools is not None:
        body["tools"] = tools
        body["tool_choice"] = tool_choice
    if response_format is not None:
        body["response_format"] = response_format
    requested_at = datetime.now(UTC).isoformat()
    envelope = _post(
        body, call="call_chat", idempotency_key=idempotency_key, log_context=log_context,
    )
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


