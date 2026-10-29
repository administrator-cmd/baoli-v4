"""远程大模型故障的影响范围分类。

网关抛出的错误码已经区分了故障来源；这里只回答一个问题：这次故障是否意味着
**后续请求同样会失败**。它决定业务层是继续执行其他规则，还是暂停本次调度。

不按中文消息匹配故障，也不在这里新增重试次数或等待时限：自动重试只发生在网关内部，
且只针对能确认请求未送达的情形。
"""

from typing import Any

# 渠道级故障：请求没有可靠完成，或渠道本身不可用。继续请求只会重复失败。
CHANNEL_FAILURE_CODES = frozenset({
    "LLM_CONFIG_MISSING",
    "LLM_AUTH_FAILED",
    "LLM_NETWORK_FAILED",
    "LLM_TIMEOUT",
    "LLM_RESULT_UNKNOWN",
    "LLM_EXTERNAL_FAILED",
    "LLM_STREAM_INCOMPLETE",
})

# 请求可能已送达供应商、但响应没有可靠返回：既不能断言没执行，也不能当作已完成。
# 该错误码由网关抛出，是否可能已计费由网关在 details.result_unknown 中按请求阶段标记。


def is_channel_failure(code: str | None) -> bool:
    """该错误码是否属于渠道级故障。"""
    return code in CHANNEL_FAILURE_CODES


def channel_failure_detail(
    code: str, message: str, *, rule_key: str | None = None, result_unknown: bool = False,
) -> dict[str, Any]:
    """暂停调度的原因。

    `result_unknown` 由调用方按请求所处阶段给出：已经收到生成内容后中断、读取超时、
    或响应未可靠返回时为真；连接前就失败的请求不据此声称可能已计费。
    """
    return {
        "code": code,
        "message": message,
        "result_unknown": bool(result_unknown),
        "rule_key": rule_key,
    }
