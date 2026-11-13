"""PaddleOCR 作业：提交、轮询与 NDJSON 结果解析。"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote

import requests

from app.common.exceptions import BusinessError
from app.core.config import Settings, get_settings
from app.domain.materials.external_artifacts import ExternalArtifact, ExternalServiceResult, OcrJobSubmission
from app.integrations.documents.http import contract_error, json_object, non_empty_string, required_service_url

logger = logging.getLogger(__name__)

SERVICE_CODE = "OCR"

# PaddleOCR 轮询状态枚举。来源：真实参考客户端
# OCR-Demo/src/utils/paddleocr_vl_client.py（第 6、111、133-152 行，对应 PaddleOCR-VL 在线 API）：
# 状态流转为 pending → running → done / failed。
OCR_JOB_ACTIVE_STATES = frozenset({"pending", "running"})

# PaddleOCR NDJSON 结果中供应商真实页号字段名。
# 参考客户端 OCR-Demo/src/utils/paddleocr_vl_client.py 与现有实现都按结果项顺序计数，
# 没有任何可确认的页号字段名，因此这里保持为空：解析只会生成显式标注的合成页号。
# 核实真实 NDJSON 行内页号字段名后，把字段名填进来，解析会自动改用 PROVIDER 页号。
_OCR_PROVIDER_PAGE_INDEX_FIELDS: tuple[str, ...] = ()


def submit_processed_document(*, file_path: Path, filename: str) -> OcrJobSubmission:
    """按官方 requests 示例提交 PDF/图片；审核材料通常传入 8010 产物。"""
    settings, submit_url, token, model = _configuration()
    deadline_at = datetime.now(UTC) + timedelta(seconds=settings.ocr_max_wait_seconds)
    deadline = time.monotonic() + settings.ocr_max_wait_seconds
    optional_payload = {
        "useDocOrientationClassify": False,
        "useDocUnwarping": False,
        "useChartRecognition": False,
    }
    headers = {"Authorization": f"bearer {token}"}
    data = {"model": model, "optionalPayload": json.dumps(optional_payload)}
    try:
        input_file = file_path.open("rb")
    except OSError as exc:
        raise BusinessError(
            "OCR_SOURCE_FILE_MISSING",
            "当前文件不存在或无法读取，无法提交 OCR",
            status_code=409,
        ) from exc
    with input_file:
        file_sha256 = hashlib.file_digest(input_file, "sha256").hexdigest()
        file_size = input_file.tell()
        input_file.seek(0)
        logger.info(
            "PaddleOCR 提交 filename=%s uploaded_filename=%s bytes=%s sha256=%s model=%s options=%s",
            filename, file_path.name, file_size, file_sha256, model, data["optionalPayload"],
        )
        try:
            submit = requests.post(
                submit_url, headers=headers, data=data, files={"file": input_file},
                timeout=_remaining_timeout(settings.request_timeout_seconds, deadline, "SUBMIT"),
            )
            submit.raise_for_status()
        except requests.RequestException as exc:
            raise _request_error(exc, "SUBMIT") from exc
    if submit.status_code != 200:
        raise contract_error(SERVICE_CODE, "SUBMIT")
    submit_payload = json_object(submit, SERVICE_CODE, "SUBMIT")
    try:
        submit_data = submit_payload["data"]
        if not isinstance(submit_data, dict):
            raise TypeError
        job_id = non_empty_string(submit_data["jobId"])
    except (KeyError, TypeError, ValueError) as exc:
        raise contract_error(SERVICE_CODE, "SUBMIT") from exc
    logger.info(
        "PaddleOCR 已受理 job_id=%s filename=%s sha256=%s trace_id=%s",
        job_id, filename, file_sha256, submit_payload.get("traceId"),
    )
    return OcrJobSubmission(job_id=job_id, deadline_at=deadline_at)


def poll_ocr_job(
    *, job_id: str, deadline_at: datetime,
    on_progress: Callable[[dict[str, Any]], None] | None = None,
) -> ExternalServiceResult:
    """按官方 requests 示例立即查询，未结束时等待后再次查询，完成后下载 JSONL。"""
    settings, submit_url, token, _ = _configuration()
    try:
        normalized_deadline = deadline_at.astimezone(UTC)
    except (AttributeError, ValueError) as exc:
        raise BusinessError("OCR_JOB_METADATA_INVALID", "已提交的 PaddleOCR 作业缺少有效截止时间", status_code=500) from exc
    if normalized_deadline <= datetime.now(UTC):
        raise _local_deadline_error("POLL")
    deadline = time.monotonic() + (normalized_deadline - datetime.now(UTC)).total_seconds()
    normalized_job_id = non_empty_string(job_id)
    headers = {"Authorization": f"bearer {token}"}
    poll_url = f"{submit_url.rstrip('/')}/{quote(normalized_job_id, safe='')}"
    previous_progress = None
    while True:
        try:
            poll_response = requests.get(
                poll_url, headers=headers,
                timeout=_remaining_timeout(settings.request_timeout_seconds, deadline, "POLL"),
            )
            poll_response.raise_for_status()
        except requests.RequestException as exc:
            raise _request_error(exc, "POLL") from exc
        if poll_response.status_code != 200:
            raise contract_error(SERVICE_CODE, "POLL")
        poll_payload = json_object(poll_response, SERVICE_CODE, "POLL")
        if not isinstance(poll_payload.get("data"), dict):
            raise contract_error(SERVICE_CODE, "POLL")
        poll_data = poll_payload["data"]
        state = poll_data.get("state")
        if not isinstance(state, str):
            raise contract_error(SERVICE_CODE, "POLL")
        extract_progress = poll_data.get("extractProgress")
        if extract_progress is not None and not isinstance(extract_progress, dict):
            raise contract_error(SERVICE_CODE, "POLL")
        progress = {"provider_state": state, "extract_progress": extract_progress}
        if progress != previous_progress:
            logger.info("PaddleOCR 处理进度 job_id=%s state=%s progress=%s",
                        normalized_job_id, state, extract_progress)
            if on_progress is not None:
                on_progress(progress)
            previous_progress = progress
        if state == "failed":
            provider_error = poll_data["errorMsg"]
            if not isinstance(provider_error, str):
                raise contract_error(SERVICE_CODE, "POLL")
            logger.warning("PaddleOCR 作业失败 job_id=%s trace_id=%s errorMsg=%s",
                           normalized_job_id, poll_payload.get("traceId"), provider_error)
            raise BusinessError(
                "OCR_JOB_FAILED",
                f"PaddleOCR 作业 {normalized_job_id} 返回失败状态：{provider_error}",
                status_code=502,
                details={"service": SERVICE_CODE, "operation": "POLL", "job_id": normalized_job_id,
                         "provider_state": state, "provider_code": poll_data.get("errorCode"),
                         "provider_error": provider_error,
                         "provider_trace_id": poll_payload.get("traceId")},
            )
        if state == "done":
            break
        if state not in OCR_JOB_ACTIVE_STATES:
            raise BusinessError("EXTERNAL_SERVICE_CONTRACT_INVALID", "PaddleOCR 轮询返回了未知的作业状态",
                                status_code=502, details={"service": SERVICE_CODE, "operation": "POLL",
                                                          "state": state})
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise _local_deadline_error("POLL")
        time.sleep(min(settings.ocr_poll_interval_seconds, remaining))
    result_url = poll_data.get("resultUrl")
    if not isinstance(result_url, dict) or not isinstance(result_url.get("jsonUrl"), str):
        raise contract_error(SERVICE_CODE, "POLL")
    try:
        jsonl_response = requests.get(
            result_url["jsonUrl"],
            timeout=_remaining_timeout(settings.request_timeout_seconds, deadline, "DOWNLOAD"),
        )
        jsonl_response.raise_for_status()
    except requests.RequestException as exc:
        raise _request_error(exc, "DOWNLOAD") from exc
    try:
        pages, average_confidence, page_no_semantics = _parse_ndjson(jsonl_response.text)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise contract_error(SERVICE_CODE, "DOWNLOAD") from exc
    if not pages:
        raise BusinessError("OCR_RESULT_EMPTY", "PaddleOCR 返回结果中没有任何结果项（连空页也没有）",
                            status_code=502, details={"service": SERVICE_CODE, "operation": "DOWNLOAD"})
    return ExternalServiceResult(
        artifact=ExternalArtifact(status="SUCCESS", content={
            "page_count": len(pages), "avg_confidence": average_confidence,
            "page_no_semantics": page_no_semantics,
            "page_no_note": ("页码为供应商结果项的合成顺序号（含空页），不是原始文件页号；"
                              "无供应商页号字段时，页码只能用于定位产物位置，不能当作原始页码引用。"
                              if page_no_semantics == "SYNTHETIC_SEQUENCE" else "页码来自供应商返回的页号字段。"),
            "pages": pages,
        })
    )


def _configuration() -> tuple[Settings, str, str, str]:
    settings = get_settings()
    submit_url = required_service_url(settings.ocr_service_url, SERVICE_CODE, "BAOLI_OCR_SERVICE_URL")
    token = settings.ocr_token_value
    model = settings.ocr_model
    missing = []
    if not token:
        missing.append("BAOLI_OCR_TOKEN")
    if not model:
        missing.append("BAOLI_OCR_MODEL")
    if missing:
        raise BusinessError(
            "EXTERNAL_CONFIG_MISSING",
            "OCR 外部服务配置不完整",
            status_code=503,
            details={"service": SERVICE_CODE, "missing": missing},
        )
    return settings, submit_url, token, model


def _request_error(exc: requests.RequestException, operation: str) -> BusinessError:
    """把 requests 异常明确交给现有作业状态处理，不重试或吞掉失败。"""
    timed_out = isinstance(exc, requests.Timeout)
    details: dict[str, Any] = {
        "service": SERVICE_CODE, "operation": operation, "error_type": type(exc).__name__,
    }
    if exc.response is not None:
        details["http_status"] = exc.response.status_code
        response_text = exc.response.text.strip()
        if response_text:
            details["provider_response"] = response_text
    logger.warning("PaddleOCR HTTP 请求失败 details=%s", details)
    return BusinessError(
        "EXTERNAL_SERVICE_TIMEOUT" if timed_out else "EXTERNAL_SERVICE_FAILED",
        "PaddleOCR 请求超时" if timed_out else "PaddleOCR HTTP 请求失败",
        status_code=504 if timed_out else 502,
        details=details,
    )


def _local_deadline_error(operation: str) -> BusinessError:
    """本地等待预算耗尽。必须说清：这只代表本地停止等待，不证明供应商作业失效。"""
    return BusinessError(
        "EXTERNAL_SERVICE_TIMEOUT",
        "PaddleOCR 本地等待预算已用完，尚无证据表明供应商作业已失效；"
        "系统不会自动重新提交，需要人工核对供应商作业状态后再决定",
        status_code=504,
        details={"service": SERVICE_CODE, "operation": operation, "reason": "LOCAL_WAIT_BUDGET_EXHAUSTED"},
    )


def _remaining_timeout(request_timeout: float, deadline: float, operation: str) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise _local_deadline_error(operation)
    return min(request_timeout, remaining)


def _provider_page_index(item: dict[str, Any]) -> int | None:
    """从结果项里取供应商真实页号；没有可确认的字段名时返回 None。"""
    for field in _OCR_PROVIDER_PAGE_INDEX_FIELDS:
        value = item.get(field)
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            continue
        return value
    return None


def _parse_ndjson(value: str) -> tuple[list[dict[str, Any]], float | None, str]:
    """解析 PaddleOCR NDJSON 结果，保留全部结果项（含空页）并显式标注页号来源。

    供应商是否有真实页号字段无法确认（参考客户端与现有实现都按结果项顺序计数），因此生成的
    page_no 是合成的稳定顺序号，并在每个 page 上用 page_no_source 标注为 SYNTHETIC_SEQUENCE。
    只有在 `_OCR_PROVIDER_PAGE_INDEX_FIELDS` 填入已核实的字段名后才会改用 PROVIDER 页号。

    合成页号按结果项顺序覆盖全部项（含空页），所以不会出现「第 1 页为空、第 2 页被前移」的
    错位；空页保留在 pages 里（text 为空字符串、empty=True），page_count 因此等于总项数。
    """
    pages: list[dict[str, Any]] = []
    confidences: list[float] = []
    saw_provider_page_no = False
    saw_synthetic_page_no = False
    for line in value.splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if not isinstance(row, dict):
            raise ValueError
        result = row.get("result") or {}
        if not isinstance(result, dict):
            raise ValueError
        layout_results = result.get("layoutParsingResults") or []
        if not isinstance(layout_results, list):
            raise ValueError
        for item in layout_results:
            if not isinstance(item, dict):
                raise ValueError
            markdown = item.get("markdown") or {}
            if not isinstance(markdown, dict):
                raise ValueError
            text = markdown.get("text") or ""
            if not isinstance(text, str):
                raise ValueError
            normalized_text = text.strip()
            provider_page_no = _provider_page_index(item)
            if provider_page_no is not None:
                page_no = provider_page_no
                page_no_source = "PROVIDER"
                saw_provider_page_no = True
            else:
                page_no = len(pages) + 1
                page_no_source = "SYNTHETIC_SEQUENCE"
                saw_synthetic_page_no = True
            pages.append(
                {
                    "page_no": page_no,
                    "text": normalized_text,
                    "empty": not normalized_text,
                    "page_no_source": page_no_source,
                    # NDJSON 只有整页 markdown 文本，没有可确认的块级坐标字段，故 bbox 恒为 None。
                    "blocks": [{"text": normalized_text, "bbox": None}] if normalized_text else [],
                }
            )
            score = item.get("overallConfidence")
            if score is not None:
                if isinstance(score, bool):
                    raise ValueError
                number = float(score)
                if not math.isfinite(number):
                    raise ValueError
                confidences.append(number)
    average = sum(confidences) / len(confidences) if confidences else None
    page_no_semantics = "PROVIDER" if saw_provider_page_no and not saw_synthetic_page_no else "SYNTHETIC_SEQUENCE"
    return pages, average, page_no_semantics
