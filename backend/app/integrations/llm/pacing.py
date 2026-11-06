"""单进程模型请求排队与共享冷却，不涉及审核规则或模型输出。"""

import math
import random
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

from app.common.exceptions import BusinessError


def retry_after_seconds(value: str | None) -> float | None:
    """兼容秒数与 HTTP 日期；无效值交给本地退避处理。"""
    if not value:
        return None
    try:
        seconds = float(value)
    except ValueError:
        try:
            until = parsedate_to_datetime(value)
            if until.tzinfo is None:
                until = until.replace(tzinfo=UTC)
            seconds = (until - datetime.now(UTC)).total_seconds()
        except (ValueError, TypeError, OverflowError):
            return None
    return max(0.0, seconds) if math.isfinite(seconds) else None


def http_retry_delay(*, retry_after: str | None, attempt: int, base_seconds: float) -> float:
    """指数退避加向上抖动，绝不早于服务端明确要求的等待时间。"""
    backoff = base_seconds * (2 ** (attempt - 1))
    delay = backoff + random.uniform(0.0, backoff * 0.2)
    return max(delay, retry_after_seconds(retry_after) or 0.0)


def wait_before_retry(delay: float, *, deadline: float) -> None:
    if delay >= deadline - time.monotonic():
        raise BusinessError(
            "LLM_TIMEOUT", "模型重试所需等待时间超过本次剩余总时限，未再次发送请求", status_code=504,
            details={"service": "LLM", "phase": "retry_wait", "result_unknown": False},
        )
    time.sleep(delay)


class LlmRequestPacer:
    """所有业务入口及连通探测共用；多 worker/多实例之间不提供分布式限流。"""

    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._busy = False
        self._next_allowed_at = 0.0

    def defer(self, seconds: float) -> None:
        with self._condition:
            self._next_allowed_at = max(self._next_allowed_at, time.monotonic() + seconds)
            self._condition.notify_all()

    def _remaining(self, deadline: float) -> float:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise BusinessError(
                "LLM_TIMEOUT", "等待模型请求排队或冷却超过总时限，本次尚未发送请求", status_code=504,
                details={"service": "LLM", "phase": "request_wait", "result_unknown": False},
            )
        return remaining

    @contextmanager
    def slot(self, *, interval_seconds: float, deadline: float,
             on_wait: Callable[[], None] | None = None) -> Iterator[None]:
        notified = False

        def notice() -> None:
            nonlocal notified
            if not notified and on_wait is not None:
                on_wait()
            notified = True

        with self._condition:
            while self._busy:
                remaining = self._remaining(deadline)
                notice()
                self._condition.wait(timeout=remaining)
            self._remaining(deadline)
            self._busy = True
        entered = False
        try:
            with self._condition:
                while True:
                    remaining = self._remaining(deadline)
                    delay = self._next_allowed_at - time.monotonic()
                    if delay <= 0:
                        break
                    notice()
                    self._condition.wait(timeout=min(delay, remaining))
            entered = True
            yield
        finally:
            with self._condition:
                if entered:
                    # 按上一轮结束计间隔，长响应结束后也不能立刻紧接下一条规则。
                    self._next_allowed_at = max(self._next_allowed_at, time.monotonic() + interval_seconds)
                self._busy = False
                self._condition.notify_all()
