"""模型节流回归：虚拟时钟、HTTP 替身，不连接模型、数据库或其他服务。"""

import json
import threading
import time
import unittest
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from types import SimpleNamespace
from unittest.mock import Mock, patch

import httpx
from pydantic import ValidationError

from app.common.exceptions import BusinessError
from app.core.config import Settings
from app.integrations.llm import gateway, pacing


class Clock:
    def __init__(self):
        self.now = 1000.0
        self.waits = []

    def advance(self, seconds=None, *, timeout=None):
        delay = seconds if seconds is not None else timeout
        self.waits.append(delay)
        self.now += delay


def response(status=200, *, headers=None, content=b""):
    return httpx.Response(status, headers=headers or {}, content=content,
                          request=httpx.Request("POST", "https://model.invalid/v1/chat/completions"))


class RetryDelayTests(unittest.TestCase):
    def test_retry_after_supports_seconds_dates_and_invalid_values(self):
        self.assertEqual(pacing.retry_after_seconds("75"), 75)
        now = datetime(2026, 11, 6, tzinfo=UTC)
        with patch.object(pacing, "datetime") as clock:
            clock.now.return_value = now
            self.assertEqual(pacing.retry_after_seconds(format_datetime(now + timedelta(seconds=45))), 45)
            self.assertEqual(pacing.retry_after_seconds(format_datetime(now - timedelta(seconds=5))), 0)
        for value in [None, "", "invalid", "nan", "inf"]:
            with self.subTest(value=value):
                self.assertIsNone(pacing.retry_after_seconds(value))

    def test_exponential_delay_has_upward_jitter_and_honors_longer_server_wait(self):
        with patch.object(pacing.random, "uniform", side_effect=lambda low, high: high):
            self.assertEqual(pacing.http_retry_delay(retry_after=None, attempt=2, base_seconds=10), 24)
            self.assertEqual(pacing.http_retry_delay(retry_after="2", attempt=2, base_seconds=10), 24)
            self.assertEqual(pacing.http_retry_delay(retry_after="90", attempt=2, base_seconds=10), 90)

    def test_retry_wait_never_sleeps_past_deadline(self):
        with patch.object(pacing.time, "monotonic", return_value=100), patch.object(pacing.time, "sleep") as sleep:
            with self.assertRaises(BusinessError) as raised:
                pacing.wait_before_retry(30, deadline=120)
            self.assertEqual(raised.exception.code, "LLM_TIMEOUT")
            self.assertFalse(raised.exception.details["result_unknown"])
            sleep.assert_not_called()

    def test_pacing_settings_reject_nonfinite_and_negative_values(self):
        for changes in [{"llm_request_interval_seconds": -1}, {"llm_request_interval_seconds": float("nan")},
                        {"llm_max_concurrent_requests": 0}, {"llm_retry_base_seconds": 0}, {"llm_retry_base_seconds": float("inf")}]:
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                Settings(_env_file=None, **changes)


class PacerTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.pacer = pacing.LlmRequestPacer()
        clock_patch = patch.object(pacing.time, "monotonic", side_effect=lambda: self.clock.now)
        clock_patch.start()
        self.addCleanup(clock_patch.stop)
        wait_patch = patch.object(self.pacer._condition, "wait", side_effect=self.clock.advance)
        wait_patch.start()
        self.addCleanup(wait_patch.stop)

    def slot(self, deadline=2000):
        return self.pacer.slot(interval_seconds=10, deadline=deadline)

    def test_interval_is_counted_after_long_response_finishes(self):
        with self.slot():
            self.clock.advance(60)
        with self.slot():
            self.assertEqual(self.clock.now, 1070)
        self.assertEqual(self.clock.waits, [60, 10])

    def test_wait_timeout_does_not_extend_existing_cooldown(self):
        with self.slot():
            pass
        with self.assertRaises(BusinessError):
            with self.slot(deadline=1005):
                self.fail("不应在等待到期后占用发送窗口")
        with self.slot():
            self.assertEqual(self.clock.now, 1010)
        self.assertEqual(self.clock.waits, [5, 5])

    def test_shared_cooldown_cannot_be_shortened_by_later_requests(self):
        self.pacer.defer(90)
        self.pacer.defer(5)
        with self.slot():
            self.assertEqual(self.clock.now, 1090)

    def test_exception_releases_slot_and_retains_interval(self):
        with self.assertRaises(RuntimeError):
            with self.slot():
                raise RuntimeError("failure")
        with self.slot():
            self.assertEqual(self.clock.now, 1010)

    def test_queue_timeout_does_not_release_another_requests_slot(self):
        with self.slot():
            with self.assertRaises(BusinessError), self.slot(deadline=1003):
                self.fail("不应抢占仍在使用的窗口")
            self.assertEqual(self.pacer._active, 1)

    def test_two_slots_overlap_but_third_waits_for_reusable_slot(self):
        def slot(deadline=2000):
            return self.pacer.slot(interval_seconds=2, deadline=deadline, max_concurrent=2)

        with slot():
            with slot():
                self.assertEqual(self.clock.now, 1000)
                with self.assertRaises(BusinessError), slot(deadline=1001):
                    self.fail("并发不能超过两个槽位")
            with slot():
                self.assertEqual(self.clock.now, 1003)

    def test_shared_cooldown_also_delays_retry_in_already_occupied_slot(self):
        with self.slot():
            self.pacer.defer(30)
            self.pacer.wait_for_retry_window(1100)
            self.assertEqual(self.clock.now, 1030)


class ConcurrentPacerTests(unittest.TestCase):
    def test_two_threads_cannot_send_at_the_same_time(self):
        pacer = pacing.LlmRequestPacer()
        queued, entered = threading.Event(), threading.Event()
        errors = []

        def second_request():
            try:
                with pacer.slot(interval_seconds=0, deadline=time.monotonic() + 3, on_wait=queued.set):
                    entered.set()
            except Exception as exc:
                errors.append(exc)

        worker = threading.Thread(target=second_request)
        with pacer.slot(interval_seconds=0, deadline=time.monotonic() + 3):
            worker.start()
            self.assertTrue(queued.wait(timeout=1))
            self.assertFalse(entered.is_set())
        worker.join(timeout=2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        self.assertTrue(entered.is_set())


class GatewayPacingTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.pacer = pacing.LlmRequestPacer()
        self.settings = SimpleNamespace(llm_max_seconds=1000, llm_timeout_seconds=60,
                                        llm_request_interval_seconds=2, llm_max_concurrent_requests=1, llm_retry_base_seconds=10,
                                        llm_max_output_tokens=393216)
        self.client = Mock()
        self.responses = []
        self.sent_at = []
        self.client.stream.side_effect = self.stream
        self.client.post.return_value = response(content=b'{"choices":[{"message":{"content":"OK"}}]}')
        self.envelope = {"choices": [{"message": {"role": "assistant", "content": "{}"}, "finish_reason": "stop"}]}
        self.read_patch = patch.object(gateway, "_read_stream", return_value=self.envelope)
        patches = [
            patch.object(gateway, "get_settings", return_value=self.settings),
            patch.object(gateway, "_require_settings", return_value=("https://model.invalid/v1", "test-key", "test-model")),
            patch.object(gateway, "_http_client", return_value=self.client),
            patch.object(gateway, "_request_pacer", self.pacer),
            patch.object(pacing.time, "monotonic", side_effect=lambda: self.clock.now),
            patch.object(pacing.time, "sleep", side_effect=self.clock.advance),
            patch.object(pacing.random, "uniform", return_value=0),
            patch.object(self.pacer._condition, "wait", side_effect=self.clock.advance),
            patch.object(gateway.threading, "Thread"),
            self.read_patch,
        ]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)

    @contextmanager
    def stream(self, *args, **kwargs):
        self.sent_at.append(self.clock.now)
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        try:
            yield item
        finally:
            item.close()

    def call(self, key="logical-request"):
        return gateway._post({"model": "test-model"}, call="test", idempotency_key=key)

    def test_all_business_entry_points_share_completion_interval(self):
        self.responses = [response(), response()]
        gateway.call_json({}, "test")
        gateway.call_chat([{"role": "user", "content": "test"}])
        self.assertEqual(self.sent_at, [1000, 1002])

    def test_429_then_503_honor_server_cooldown_and_keep_idempotency_key(self):
        self.responses = [response(429, headers={"Retry-After": "60"}), response(503), response()]
        self.call()
        self.assertEqual(self.sent_at, [1000, 1060, 1080])
        for call in self.client.stream.call_args_list:
            self.assertEqual(call.kwargs["headers"]["Idempotency-Key"], "logical-request")
            self.assertTrue(call.kwargs["json"]["stream"])

    def test_final_http_failure_keeps_cooldown_for_manual_resume(self):
        self.responses = [response(503, headers={"x-request-id": "provider-request"}) for _ in range(4)]
        with self.assertRaises(BusinessError) as raised:
            self.call()
        self.assertEqual(self.sent_at, [1000, 1010, 1030, 1070])
        self.assertEqual(raised.exception.details["http_status"], 503)
        self.assertEqual(raised.exception.details["request_id"], "provider-request")
        self.responses = [response()]
        self.call(key="manual-resume")
        self.assertEqual(self.sent_at[-1], 1150)

    def test_excessive_retry_after_does_not_send_beyond_total_deadline(self):
        self.settings.llm_max_seconds = 20
        self.responses = [response(429, headers={"Retry-After": "60"})]
        with self.assertRaises(BusinessError) as raised:
            self.call()
        self.assertEqual(raised.exception.details["phase"], "retry_wait")
        self.assertEqual(self.sent_at, [1000])
        self.assertEqual(self.clock.waits, [])

    def test_read_timeout_is_not_automatically_reposted(self):
        self.responses = [httpx.ReadTimeout("response unknown")]
        with self.assertRaises(BusinessError) as raised:
            self.call()
        self.assertEqual(raised.exception.code, "LLM_RESULT_UNKNOWN")
        self.assertTrue(raised.exception.details["result_unknown"])
        self.assertEqual(self.sent_at, [1000])

    def test_authentication_failure_is_not_retried(self):
        self.responses = [response(401)]
        with self.assertRaises(BusinessError) as raised:
            self.call()
        self.assertEqual(raised.exception.code, "LLM_AUTH_FAILED")
        self.assertEqual(self.sent_at, [1000])

    def test_connection_error_keeps_existing_safe_retry(self):
        self.responses = [httpx.ConnectError("not delivered"), response()]
        self.call()
        self.assertEqual(self.sent_at, [1000, 1001])

    def test_status_probe_cannot_bypass_business_cooldown(self):
        self.responses = [response()]
        self.call()
        result = gateway.probe_connection(timeout_seconds=30)
        self.assertTrue(result.ok)
        self.assertEqual(self.clock.now, 1002)
        self.client.post.assert_called_once()
        self.assertEqual(self.client.post.call_args.kwargs["timeout"], 28)

    def test_status_probe_http_error_extends_shared_cooldown_without_retry(self):
        self.client.post.return_value = response(429, headers={"Retry-After": "60"})
        result = gateway.probe_connection(timeout_seconds=30)
        self.assertFalse(result.ok)
        self.client.post.assert_called_once()
        self.responses = [response()]
        self.call()
        self.assertEqual(self.sent_at, [1060])

    def test_expired_probe_wait_does_not_send_http_request(self):
        self.pacer.defer(60)
        result = gateway.probe_connection(timeout_seconds=5)
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "LLM_TIMEOUT")
        self.client.post.assert_not_called()
        self.client.stream.assert_not_called()

    def test_streamed_tool_calls_and_output_limit_are_preserved(self):
        self.read_patch.stop()
        chunks = [
            {"id": "response-test", "choices": [{"index": 0, "delta": {"role": "assistant", "tool_calls": [
                {"index": 0, "id": "call-test", "type": "function", "function": {"name": "echo", "arguments": '{"x":1}'}},
            ]}, "finish_reason": None}]},
            {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
        ]
        content = "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks) + "data: [DONE]\n\n"
        self.responses = [response(headers={"content-type": "text/event-stream"}, content=content.encode())]
        tools = [{"type": "function", "function": {"name": "echo", "parameters": {"type": "object"}}}]
        turn = gateway.call_chat([{"role": "user", "content": "test"}], tools=tools)
        self.assertEqual(turn.kind, gateway.TURN_TOOL_CALLS)
        self.assertEqual(turn.tool_calls[0].arguments, {"x": 1})
        body = self.client.stream.call_args.kwargs["json"]
        self.assertEqual(body["tools"], tools)
        self.assertTrue(body["stream"])
        self.assertEqual(body["max_tokens"], 393216)


if __name__ == "__main__":
    unittest.main()
