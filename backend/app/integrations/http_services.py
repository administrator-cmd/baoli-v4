"""外部文档服务适配器：按供应商真实的 HTTP 契约调用，并归一化成内部结构。"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

import httpx
import requests

from ..config import Settings, get_settings
from ..domain.external_artifacts import (
    ExternalArtifact,
    ExternalEvidence,
    ExternalServiceResult,
    OcrJobSubmission,
)
from ..common.exceptions import BusinessError


logger = logging.getLogger(__name__)


# PaddleOCR 轮询状态枚举。来源：真实参考客户端
# OCR-Demo/src/utils/paddleocr_vl_client.py（第 6、111、133-152 行，对应 PaddleOCR-VL 在线 API）：
# 状态流转为 pending → running → done / failed。
# 只有这四个取值来自真实契约；出现集合之外的取值说明供应商契约发生变化，
# 必须立即报 EXTERNAL_SERVICE_CONTRACT_INVALID，而不是继续等到本地预算耗尽。
# 非终结态（继续等待）：pending、running；终结态：done（成功）、failed（失败）。
OCR_JOB_STATES = frozenset({"pending", "running", "done", "failed"})
OCR_JOB_ACTIVE_STATES = frozenset({"pending", "running"})

# PaddleOCR NDJSON 结果中供应商真实页号字段名。
# 参考客户端 OCR-Demo/src/utils/paddleocr_vl_client.py 与现有实现都按结果项顺序计数，
# 没有任何可确认的页号字段名，因此这里保持为空：解析只会生成显式标注的合成页号。
# 核实真实 NDJSON 行内页号字段名后，把字段名填进来，解析会自动改用 PROVIDER 页号。
_OCR_PROVIDER_PAGE_INDEX_FIELDS: tuple[str, ...] = ()


def list_material_types() -> list[dict[str, Any]]:
    """读取 8018 支持的材料类型目录。

    材料识别类型必须从该目录选择，不能由业务自由输入：8018 的 doc_type 是严格枚举，
    自由输入的值会在执行文件处理时被服务端直接拒绝，问题要到运行时才暴露。

    目录是**外部服务的事实**，本函数不缓存、不内置任何默认清单，也不把
    8018 的取值与业务材料名称做映射。
    """

    settings = get_settings()
    base_url = _required_service_url(settings.service_8018_url, "MATERIAL_CLASSIFY", "BAOLI_SERVICE_8018_URL")
    with _client(settings.request_timeout_seconds) as client:
        catalog_response = _request(
            client,
            "GET",
            f"{base_url}/supported-types",
            service_code="MATERIAL_CLASSIFY",
            operation="LIST_TYPES",
        )
    catalog = _json_object(catalog_response, "MATERIAL_CLASSIFY", "LIST_TYPES")
    rows = catalog.get("types")
    if not isinstance(rows, list) or not rows:
        raise _contract_error("MATERIAL_CLASSIFY", "LIST_TYPES")
    items: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict) or row.get("id") is None:
            raise _contract_error("MATERIAL_CLASSIFY", "LIST_TYPES")
        try:
            items.append({"id": row["id"], "name": _non_empty_string(row["name"])})
        except (KeyError, TypeError, ValueError) as exc:
            raise _contract_error("MATERIAL_CLASSIFY", "LIST_TYPES") from exc
    return items


def validate_material_type(
    *,
    file_path: Path,
    filename: str,
    content_type: str,
    material_type: str,
) -> ExternalServiceResult:
    """先核对 8018 的类型目录，再用它校验一份原始上传。"""

    settings = get_settings()
    base_url = _required_service_url(settings.service_8018_url, "MATERIAL_CLASSIFY", "BAOLI_SERVICE_8018_URL")
    doc_type = material_type.strip()
    if not doc_type:
        raise BusinessError(
            "MATERIAL_TYPE_MISSING",
            "当前材料位未声明 8018 材料类型，无法执行材料校验",
            status_code=409,
        )
    with _client(settings.request_timeout_seconds) as client:
        try:
            catalog_response = _request(
                client,
                "GET",
                f"{base_url}/supported-types",
                service_code="MATERIAL_CLASSIFY",
                operation="LIST_TYPES",
            )
            catalog = _json_object(catalog_response, "MATERIAL_CLASSIFY", "LIST_TYPES")
            supported_names = _supported_type_names(catalog)
        except BusinessError as exc:
            # 类型目录查询是只读请求，/validate 尚未调用；本次产物占位可以安全撤销。
            raise BusinessError(
                "MATERIAL_TYPE_CATALOG_FAILED",
                f"8018 材料类型目录查询失败：{exc.message}",
                status_code=exc.status_code,
                details={"cause_code": exc.code, **(exc.details or {})},
            ) from exc
        if doc_type not in supported_names:
            raise BusinessError(
                "MATERIAL_TYPE_UNSUPPORTED",
                "当前材料类型不在 8018 支持目录中，无法执行材料校验",
                status_code=409,
                details={"service": "MATERIAL_CLASSIFY", "material_type": doc_type},
            )
        try:
            with file_path.open("rb") as input_file:
                response = _request(
                    client,
                    "POST",
                    f"{base_url}/validate",
                    service_code="MATERIAL_CLASSIFY",
                    operation="VALIDATE",
                    files={"file": (filename, input_file, content_type)},
                    data={"doc_type": doc_type},
                )
        except OSError as exc:
            raise BusinessError(
                "DOCUMENT_SOURCE_MISSING",
                "当前上传文件无法读取，不能执行材料校验",
                status_code=500,
            ) from exc
    payload = _json_object(response, "MATERIAL_CLASSIFY", "VALIDATE")
    try:
        success = _strict_bool(payload["success"])
        matched = _strict_bool(payload["is_valid"])
        confidence = _confidence(payload["confidence"])
        matched_keywords = payload["matched_keywords"]
        provider_error = payload["error"]
        if not isinstance(matched_keywords, list) or not isinstance(provider_error, str):
            raise TypeError
        # 关键词原文（形如「合同(+4分)」）要原样保留：它是人工判断"文件是否放错材料位"
        # 最直接的依据，只留个计数等于把判据丢掉。
        matched_keyword_list = [str(item) for item in matched_keywords if isinstance(item, str)]
        # doc_type 在服务无法处理文件时真实返回空串（以不可读文件实测确认），
        # 因此按普通字符串读取；它只用于记录，不参与下面的分支判断。
        detected_type = str(payload["doc_type"] or "")
    except (KeyError, TypeError, ValueError) as exc:
        raise _contract_error("MATERIAL_CLASSIFY", "VALIDATE") from exc
    # 8018 的 success 与 is_valid 实测恒同真同假，区分不了「服务处理不了文件」与
    # 「文件不匹配声明的材料类型」。真正的判别字段是 error（两者均已实测）：
    #   error 非空（原文「无法提取文本内容」）→ 服务未能完成识别，属于调用失败；
    #   error 为空且 is_valid=false      → 识别成功但文件不匹配该材料类型。
    # 后者是可裁决的业务结论：落成 matches_declared_material=false 随产物保存，由业务按
    # 识别结论另行裁决（8018 只记录不判定，见 services/audit/artifacts.material_classify_content）。
    # 按 success 一律抛错会把一件该由人裁决的事变成整个文件处理节点的硬失败。
    if provider_error:
        raise BusinessError(
            "EXTERNAL_SERVICE_REJECTED",
            f"8018 未能完成材料类型校验：{provider_error}",
            status_code=502,
            details={
                "service": "MATERIAL_CLASSIFY",
                "operation": "VALIDATE",
                "declared_material_type": doc_type,
                "provider_error": provider_error,
                "confidence": confidence,
                "matched_keywords": matched_keyword_list,
            },
        )
    if not success and matched:
        # 响应自相矛盾：不挑一个当结论，按契约异常暴露。
        raise _contract_error("MATERIAL_CLASSIFY", "VALIDATE")
    return ExternalServiceResult(
        artifact=ExternalArtifact(
            status="SUCCESS",
            content={
                "matches_declared_material": matched,
                "declared_material_type": doc_type,
                "detected_material_type": detected_type,
                "confidence": confidence,
                "matched_keywords": matched_keyword_list,
                "matched_keyword_count": len(matched_keyword_list),
            },
        )
    )


def process_document(
    *,
    file_path: Path,
    filename: str,
    content_type: str,
) -> ExternalServiceResult:
    """调用 8010 并下载处理产物，供本地持久化。"""

    settings = get_settings()
    base_url = _required_service_url(settings.service_8010_url, "DOCUMENT_PARSE", "BAOLI_SERVICE_8010_URL")
    infer = "gpu" if filename.lower().endswith(".pdf") else "cpu"
    with _client(settings.request_timeout_seconds) as client:
        try:
            with file_path.open("rb") as input_file:
                response = _request(
                    client,
                    "POST",
                    f"{base_url}/process",
                    service_code="DOCUMENT_PARSE",
                    operation="PROCESS",
                    params={"infer": infer},
                    files={"file": (filename, input_file, content_type)},
                )
        except OSError as exc:
            raise BusinessError(
                "DOCUMENT_SOURCE_MISSING",
                "当前上传文件无法读取，不能执行文件处理",
                status_code=500,
            ) from exc
        payload = _json_object(response, "DOCUMENT_PARSE", "PROCESS")
        try:
            success = _strict_bool(payload["success"])
            final_url = _non_empty_string(payload["final_url"])
        except (KeyError, TypeError, ValueError) as exc:
            raise _contract_error("DOCUMENT_PARSE", "PROCESS") from exc
        if not success:
            raise BusinessError(
                "EXTERNAL_SERVICE_REJECTED",
                "8010 未成功完成文件处理",
                status_code=502,
                details={"service": "DOCUMENT_PARSE", "operation": "PROCESS"},
            )
        downloaded = _request(
            client,
            "GET",
            _validate_download_url(final_url, "DOCUMENT_PARSE", "DOWNLOAD"),
            service_code="DOCUMENT_PARSE",
            operation="DOWNLOAD",
            headers={"Accept-Encoding": "identity"},
        )
    content = downloaded.content
    if not content:
        raise BusinessError(
            "EXTERNAL_ARTIFACT_EMPTY",
            "8010 下载的处理产物为空",
            status_code=502,
            details={"service": "DOCUMENT_PARSE", "operation": "DOWNLOAD"},
        )
    processed_filename = f"processed_{Path(filename).name or 'document'}"
    processed_content_type = _response_content_type(downloaded, content_type)
    # 8010 /process 的真实响应只确认了 success 与 final_url（二进制产物本身）。
    # 参考契约里没有可核对的文本或页信息字段，因此这里不补写 page/text 结构；
    # 正文与页码语义来自后续 OCR 产物。缺少 8010 页/文本字段属于待核实外部契约。
    return ExternalServiceResult(
        artifact=ExternalArtifact(
            status="SUCCESS",
            content={
                "infer": infer,
                "processed_filename": processed_filename,
                "content_type": processed_content_type,
                "size_bytes": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
            },
        ),
        binary_content=content,
        filename=processed_filename,
        content_type=processed_content_type,
    )


def detect_seals(
    *,
    file_path: Path,
    filename: str,
    content_type: str,
) -> ExternalServiceResult:
    """把原始上传提交给印章识别，只保留归一化后的印章事实。"""

    settings = get_settings()
    base_url = _required_service_url(settings.seal_service_url, "SEAL", "BAOLI_SEAL_SERVICE_URL")
    with _client(settings.request_timeout_seconds) as client:
        try:
            with file_path.open("rb") as input_file:
                response = _request(
                    client,
                    "POST",
                    f"{base_url}/predict",
                    service_code="SEAL",
                    operation="PREDICT",
                    files={"file": (filename, input_file, content_type)},
                )
        except OSError as exc:
            raise BusinessError(
                "DOCUMENT_SOURCE_MISSING",
                "当前上传文件无法读取，不能执行印章识别",
                status_code=500,
            ) from exc
    payload = _json_object(response, "SEAL", "PREDICT")
    try:
        pages_value = payload["pages"]
        summary = payload["summary"]
        if not isinstance(pages_value, list) or not isinstance(summary, dict):
            raise TypeError
        pages: list[dict[str, Any]] = []
        evidence: list[ExternalEvidence] = []
        total = 0
        for page in pages_value:
            if not isinstance(page, dict):
                raise TypeError
            page_no = page["page_index"]
            seals_count = page["seals_count"]
            seals = page["seals"]
            if (
                isinstance(page_no, bool)
                or not isinstance(page_no, int)
                or page_no <= 0
                or isinstance(seals_count, bool)
                or not isinstance(seals_count, int)
                or seals_count < 0
                or not isinstance(seals, list)
                or seals_count != len(seals)
            ):
                raise ValueError
            normalized_seals: list[dict[str, Any]] = []
            for seal in seals:
                if not isinstance(seal, dict):
                    raise TypeError
                # 供应商字段：class_name=印章类型，text=该印章识别出的**主体名称**。
                # 代码内部统一称之为主体名称；下方 content.artifact_subject 说明这一归一化，
                # 供下游按语义读取，不需要重新猜哪个字段是主体。
                class_name = _non_empty_string(seal["class_name"])
                subject_name = seal["text"]
                bbox_value = seal["bbox"]
                confidence = _confidence(seal["confidence"])
                if not isinstance(subject_name, str):
                    raise TypeError
                normalized_subject = subject_name.strip()
                bbox = _seal_bbox(bbox_value)
                normalized_seals.append(
                    {
                        "seal_type": class_name,
                        "subject_name": normalized_subject,
                        "bbox": bbox,
                        "confidence": confidence,
                    }
                )
                if normalized_subject:
                    evidence.append(
                        ExternalEvidence(page_no=page_no, quote_text=normalized_subject, bbox=bbox)
                    )
            pages.append({"page_no": page_no, "seals_count": seals_count, "seals": normalized_seals})
            total += seals_count
        summary_total = summary["total_seals"]
        if isinstance(summary_total, bool) or not isinstance(summary_total, int) or summary_total < 0 or summary_total != total:
            raise ValueError
    except (KeyError, TypeError, ValueError) as exc:
        raise _contract_error("SEAL", "PREDICT") from exc
    # pages 为空、total_seals=0 是识别成功的正常结果，不是服务故障。
    # 印章数量只表示识别到的印章个数，不代表盖章正确：漏盖、错盖、伪造都可能在数量上"正常"。
    # 这条语义显式写进 content，避免下游把数量当成盖章合规结论。
    return ExternalServiceResult(
        artifact=ExternalArtifact(
            status="SUCCESS",
            content={
                "seal_count": total,
                "seal_count_note": "印章数量仅表示识别到的印章个数，不等于盖章正确；漏盖、错盖、伪造不受此数量约束。",
                "pages": pages,
                # 主体条目：每条印章记录一行，字段就是该枚印章识别出的主体名称。
                # 这是本系统对供应商响应中“主体名称”的显式归一化：下游（主体检索、
                # 主体核对）只读这个结构，不再自己判断 class_name/text 哪个是主体。
                # 同一枚印章在多个页面重复出现时各条目分别保留；不同识别名不自动合并。
                "artifact_subject": "subject_name",
                "seal_subjects": [
                    {
                        "entry_no": entry_no,
                        "page_no": page.get("page_no"),
                        "seal_type": seal["seal_type"],
                        "subject_name": seal["subject_name"],
                        "bbox": seal["bbox"],
                        "confidence": seal["confidence"],
                    }
                    for page in pages
                    for entry_no, seal in enumerate(page["seals"], start=1)
                ],
            },
            evidence=evidence,
        )
    )


def submit_processed_document(*, file_path: Path, filename: str) -> OcrJobSubmission:
    """把当前的 8010 产物提交一次，返回可续查的 OCR 作业。"""

    settings, submit_url, token, model = _ocr_configuration()
    deadline_at = datetime.now(UTC) + timedelta(seconds=settings.ocr_max_wait_seconds)
    deadline = time.monotonic() + settings.ocr_max_wait_seconds
    optional_payload = json.dumps(
        {
            "useDocOrientationClassify": False,
            "useDocUnwarping": False,
            "useChartRecognition": False,
        },
        separators=(",", ":"),
    )
    try:
        with file_path.open("rb") as input_file:
            prepared = requests.Request(
                "POST",
                submit_url,
                data={"model": model, "optionalPayload": optional_payload},
                files={"file": (filename, input_file)},
            ).prepare()
    except OSError as exc:
        raise BusinessError(
            "PROCESSED_ARTIFACT_MISSING",
            "当前上传文件缺少可读取的 8010 处理产物，无法提交 OCR",
            status_code=409,
        ) from exc
    body = prepared.body
    content_type = prepared.headers.get("Content-Type")
    if not isinstance(body, bytes) or not content_type:
        raise BusinessError("OCR_MULTIPART_INVALID", "无法生成 PaddleOCR 所需的上传请求", status_code=500)
    with _client(settings.request_timeout_seconds) as client:
        submit = _request(
            client,
            "POST",
            submit_url,
            service_code="OCR",
            operation="SUBMIT",
            timeout=_remaining_timeout(settings.request_timeout_seconds, deadline, "SUBMIT"),
            headers={"Authorization": f"bearer {token}", "Content-Type": content_type},
            content=body,
        )
        submit_payload = _json_object(submit, "OCR", "SUBMIT")
        try:
            submit_data = submit_payload["data"]
            if not isinstance(submit_data, dict):
                raise TypeError
            job_id = _non_empty_string(submit_data["jobId"])
        except (KeyError, TypeError, ValueError) as exc:
            raise _contract_error("OCR", "SUBMIT") from exc
    return OcrJobSubmission(job_id=job_id, deadline_at=deadline_at)


def poll_ocr_job(*, job_id: str, deadline_at: datetime) -> ExternalServiceResult:
    """查询并下载一个**此前已提交**的 PaddleOCR 作业，不重新提交。

    超时语义区分两类，绝不含糊：
      - 单次 HTTP 请求超时：由 `_request` 抛出，消息为“……外部服务请求超时”，只说明这一次调用没等到响应；
      - 本地等待预算耗尽（deadline_at 到期）：抛 EXTERNAL_SERVICE_TIMEOUT，消息明确说明**没有证据**表明
        供应商作业已失效，系统不会自动重新提交，需要人工对账。本地停止等待不等于供应商作业失败。
    """

    settings, submit_url, token, _ = _ocr_configuration()
    try:
        normalized_deadline = deadline_at.astimezone(UTC)
    except (AttributeError, ValueError) as exc:
        raise BusinessError("OCR_JOB_METADATA_INVALID", "已提交的 PaddleOCR 作业缺少有效截止时间", status_code=500) from exc
    remaining = (normalized_deadline - datetime.now(UTC)).total_seconds()
    if remaining <= 0:
        raise _ocr_local_deadline_error("POLL")
    deadline = time.monotonic() + remaining
    normalized_job_id = _non_empty_string(job_id)
    headers = {"Authorization": f"bearer {token}"}
    poll_url = f"{submit_url.rstrip('/')}/{quote(normalized_job_id, safe='')}"
    with _client(settings.request_timeout_seconds) as client:
        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            time.sleep(min(settings.ocr_poll_interval_seconds, remaining))
            if time.monotonic() >= deadline:
                break
            poll = _request(
                client,
                "GET",
                poll_url,
                service_code="OCR",
                operation="POLL",
                timeout=_remaining_timeout(settings.request_timeout_seconds, deadline, "POLL"),
                headers=headers,
            )
            poll_payload = _json_object(poll, "OCR", "POLL")
            try:
                poll_data = poll_payload["data"]
                if not isinstance(poll_data, dict):
                    raise TypeError
                state = poll_data["state"]
                if not isinstance(state, str):
                    raise TypeError
            except (KeyError, TypeError) as exc:
                raise _contract_error("OCR", "POLL") from exc
            if state == "failed":
                provider_error = poll_data.get("error")
                provider_code = poll_data.get("errorCode")
                if isinstance(provider_error, dict):
                    provider_code = provider_error.get("code") or provider_code
                    provider_error = provider_error.get("message") or provider_error.get("msg")
                if not isinstance(provider_error, str) or not provider_error.strip():
                    provider_error = next(
                        (
                            value.strip()
                            for key in ("errorMessage", "errorMsg", "message")
                            if isinstance(value := poll_data.get(key), str) and value.strip()
                        ),
                        None,
                    )
                logger.warning(
                    "PaddleOCR 作业失败 job_id=%s state=%s provider_code=%s provider_error=%s response_fields=%s",
                    normalized_job_id,
                    state,
                    provider_code,
                    provider_error,
                    sorted(poll_data),
                )
                provider_reason = "；".join(
                    str(item) for item in (provider_code, provider_error) if item is not None and str(item).strip()
                )
                raise BusinessError(
                    "OCR_JOB_FAILED",
                    f"PaddleOCR 作业 {normalized_job_id} 返回失败状态"
                    + (f"：{provider_reason}" if provider_reason else "；当前响应未提供可识别的失败原因"),
                    status_code=502,
                    details={"service": "OCR", "operation": "POLL", "job_id": normalized_job_id},
                )
            if state == "done":
                break
            if state not in OCR_JOB_ACTIVE_STATES:
                # 只接过真实契约里确认过的状态（pending/running/done/failed）。未知状态立即报契约错误，
                # 带上状态值与供应商字段名，绝不把它当作"继续等待"最终变成超时。
                raise BusinessError(
                    "EXTERNAL_SERVICE_CONTRACT_INVALID",
                    "PaddleOCR 轮询返回了未知的作业状态",
                    status_code=502,
                    details={
                        "service": "OCR",
                        "operation": "POLL",
                        "field": "data.state",
                        "state": state,
                        "known_states": sorted(OCR_JOB_STATES),
                    },
                )
            continue
        else:
            raise _ocr_local_deadline_error("POLL")
        # 循环以 done 结束：这里下载结果。
        try:
            result_url = poll_data["resultUrl"]
            if not isinstance(result_url, dict):
                raise TypeError
            json_url = _non_empty_string(result_url["jsonUrl"])
        except (KeyError, TypeError, ValueError) as exc:
            raise _contract_error("OCR", "POLL") from exc
        downloaded = _request(
            client,
            "GET",
            _validate_download_url(json_url, "OCR", "DOWNLOAD"),
            service_code="OCR",
            operation="DOWNLOAD",
            timeout=_remaining_timeout(settings.request_timeout_seconds, deadline, "DOWNLOAD"),
            headers={"Accept-Encoding": "identity"},
        )
        try:
            pages, average_confidence, page_no_semantics = _parse_ocr_ndjson(downloaded.text)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise _contract_error("OCR", "DOWNLOAD") from exc
        if not pages:
            raise BusinessError(
                "OCR_RESULT_EMPTY",
                "PaddleOCR 返回结果中没有任何结果项（连空页也没有）",
                status_code=502,
                details={"service": "OCR", "operation": "DOWNLOAD"},
            )
        return ExternalServiceResult(
            artifact=ExternalArtifact(
                status="SUCCESS",
                content={
                    "page_count": len(pages),
                    "avg_confidence": average_confidence,
                    "page_no_semantics": page_no_semantics,
                    "page_no_note": (
                        "页码为供应商结果项的合成顺序号（含空页），不是原始文件页号；"
                        "无供应商页号字段时，页号只能用于定位产物位置，不能当作原始页码引用。"
                        if page_no_semantics == "SYNTHETIC_SEQUENCE"
                        else "页码来自供应商返回的页号字段。"
                    ),
                    "pages": pages,
                },
            )
        )


def _ocr_configuration() -> tuple[Settings, str, str, str]:
    settings = get_settings()
    submit_url = _required_service_url(settings.ocr_service_url, "OCR", "BAOLI_OCR_SERVICE_URL")
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
            details={"service": "OCR", "missing": missing},
        )
    return settings, submit_url, token, model


def _client(timeout: float) -> httpx.Client:
    return httpx.Client(timeout=timeout, follow_redirects=False)


def _required_service_url(
    value: str | None,
    service_code: str,
    setting_name: str,
    *,
    strip_trailing_slash: bool = True,
) -> str:
    if not value:
        raise BusinessError(
            "EXTERNAL_CONFIG_MISSING",
            f"{service_code} 外部服务未配置",
            status_code=503,
            details={"service": service_code, "missing": [setting_name]},
        )
    normalized = value.strip()
    return normalized.rstrip("/") if strip_trailing_slash else normalized


def _request(
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


def _json_object(response: httpx.Response, service_code: str, operation: str) -> dict[str, Any]:
    try:
        payload = response.json()
    except ValueError as exc:
        raise _contract_error(service_code, operation) from exc
    if not isinstance(payload, dict):
        raise _contract_error(service_code, operation)
    return payload


def _supported_type_names(payload: dict[str, Any]) -> set[str]:
    rows = payload.get("types")
    if not isinstance(rows, list) or not rows:
        raise _contract_error("MATERIAL_CLASSIFY", "LIST_TYPES")
    names: set[str] = set()
    for row in rows:
        if not isinstance(row, dict) or row.get("id") is None:
            raise _contract_error("MATERIAL_CLASSIFY", "LIST_TYPES")
        try:
            names.add(_non_empty_string(row["name"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise _contract_error("MATERIAL_CLASSIFY", "LIST_TYPES") from exc
    return names


def _validate_download_url(value: str, service_code: str, operation: str) -> str:
    """校验下载地址的 URL 形式，并原样返回。

    当前只校验 URL 形式：scheme 限定 http/https、必须有主机、不允许携带凭据、不允许片段、端口可解析。
    **本函数不限制目标主机**，也不做"禁止内网地址"的启发式阻断：允许域名集合应来自真实供应商契约，
    在拿到真实响应样本前不硬编码白名单、不猜测域名。
    "供应商是否保证返回的地址只能指向其可控主机"属于**待核实**外部契约事项（见最终报告）。
    """
    try:
        normalized = value.strip()
        parsed = urlsplit(normalized)
        if (
            parsed.scheme.lower() not in {"http", "https"}
            or not parsed.netloc
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
        ):
            raise ValueError
        _ = parsed.port
    except (AttributeError, TypeError, ValueError) as exc:
        raise BusinessError(
            "UNSAFE_DOWNLOAD_URL",
            f"{service_code} 返回了无效的下载地址",
            status_code=502,
            details={"service": service_code, "operation": operation},
        ) from exc
    return normalized


def _ocr_local_deadline_error(operation: str) -> BusinessError:
    """本地等待预算耗尽。必须说清：这只代表本地停止等待，不证明供应商作业失效。"""
    return BusinessError(
        "EXTERNAL_SERVICE_TIMEOUT",
        "PaddleOCR 本地等待预算已用完，尚无证据表明供应商作业已失效；"
        "系统不会自动重新提交，需要人工核对供应商作业状态后再决定",
        status_code=504,
        details={"service": "OCR", "operation": operation, "reason": "LOCAL_WAIT_BUDGET_EXHAUSTED"},
    )


def _remaining_timeout(request_timeout: float, deadline: float, operation: str) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise _ocr_local_deadline_error(operation)
    return min(request_timeout, remaining)


def _response_content_type(response: httpx.Response, fallback: str) -> str:
    value = response.headers.get("content-type", "").split(";", 1)[0].strip()
    return value or fallback or "application/octet-stream"


def _strict_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    raise ValueError


def _non_empty_string(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError
    return value.strip()


def _confidence(value: Any) -> float:
    if isinstance(value, bool):
        raise ValueError
    number = float(value)
    if not math.isfinite(number) or not 0 <= number <= 1:
        raise ValueError
    return number


def _seal_bbox(value: Any) -> list[float] | None:
    if not isinstance(value, list) or len(value) != 4:
        return None
    if any(isinstance(item, bool) or not isinstance(item, (int, float)) or not math.isfinite(float(item)) for item in value):
        return None
    return [float(item) for item in value]


def _provider_page_index(item: dict[str, Any]) -> int | None:
    """从结果项里取供应商真实页号；没有可确认的字段名时返回 None。"""
    for field in _OCR_PROVIDER_PAGE_INDEX_FIELDS:
        value = item.get(field)
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            continue
        return value
    return None


def _parse_ocr_ndjson(value: str) -> tuple[list[dict[str, Any]], float | None, str]:
    """解析 PaddleOCR NDJSON 结果，保留全部结果项（含空页）并显式标注页号来源。

    本函数无法确认供应商是否存在真实页号字段（参考客户端 OCR-Demo 与现有实现都按结果项顺序计数，
    没有任何页号字段），因此生成的 page_no 是**合成的稳定顺序号**，并在每个 page 上用
    page_no_source 显式标注为 SYNTHETIC_SEQUENCE——合成页号是显式标注的，不是伪装成真实页号。
    只有在 _OCR_PROVIDER_PAGE_INDEX_FIELDS 填入已核实的字段名后，才会改用 PROVIDER 页号。

    合成页号按结果项顺序覆盖**全部**项（含空页），所以不会出现"第 1 页为空、第 2 页被前移"的错位。
    空页保留在 pages 里（text 为空字符串、empty=True），page_count 因此等于总项数。

    返回值第三项为顶层语义：SYNTHETIC_SEQUENCE（合成页号）或 PROVIDER（供应商页号）。
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
            confidence = item.get("overallConfidence")
            if confidence is not None:
                if isinstance(confidence, bool):
                    raise ValueError
                number = float(confidence)
                if not math.isfinite(number):
                    raise ValueError
                confidences.append(number)
    average = sum(confidences) / len(confidences) if confidences else None
    page_no_semantics = "PROVIDER" if saw_provider_page_no and not saw_synthetic_page_no else "SYNTHETIC_SEQUENCE"
    return pages, average, page_no_semantics


def _contract_error(service_code: str, operation: str) -> BusinessError:
    return BusinessError(
        "EXTERNAL_SERVICE_CONTRACT_INVALID",
        f"{service_code} 外部服务返回格式不符合预期",
        status_code=502,
        details={"service": service_code, "operation": operation},
    )
