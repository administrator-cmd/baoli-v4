"""8010 文件处理与印章识别。"""

from __future__ import annotations

import hashlib
import math
from pathlib import Path
from typing import Any

from ...common.exceptions import BusinessError
from ...core.config import get_settings
from ...domain.external_artifacts import (
    ExternalArtifact,
    ExternalEvidence,
    ExternalServiceResult,
)
from .http import (
    client,
    contract_error,
    json_object,
    non_empty_string,
    request,
    required_service_url,
    response_content_type,
    strict_bool,
)
from .http import (
    confidence as _confidence,
)

PARSE_SERVICE = "DOCUMENT_PARSE"
SEAL_SERVICE = "SEAL"


def process_document(
    *,
    file_path: Path,
    filename: str,
    content_type: str,
) -> ExternalServiceResult:
    """调用 8010 并下载处理产物，供本地持久化。"""
    settings = get_settings()
    base_url = required_service_url(settings.service_8010_url, PARSE_SERVICE, "BAOLI_SERVICE_8010_URL")
    infer = "gpu" if filename.lower().endswith(".pdf") else "cpu"
    with client() as http:
        try:
            with file_path.open("rb") as input_file:
                response = request(
                    http, "POST", f"{base_url}/process",
                    service_code=PARSE_SERVICE, operation="PROCESS",
                    params={"infer": infer},
                    files={"file": (filename, input_file, content_type)},
                )
        except OSError as exc:
            raise BusinessError(
                "DOCUMENT_SOURCE_MISSING",
                "当前上传文件无法读取，不能执行文件处理",
                status_code=500,
            ) from exc
        payload = json_object(response, PARSE_SERVICE, "PROCESS")
        try:
            success = strict_bool(payload["success"])
            final_url = non_empty_string(payload["final_url"])
        except (KeyError, TypeError, ValueError) as exc:
            raise contract_error(PARSE_SERVICE, "PROCESS") from exc
        if not success:
            raise BusinessError(
                "EXTERNAL_SERVICE_REJECTED",
                "8010 未成功完成文件处理",
                status_code=502,
                details={"service": PARSE_SERVICE, "operation": "PROCESS"},
            )
        downloaded = request(
            http, "GET",
            _validate_download_url(final_url, PARSE_SERVICE, "DOWNLOAD"),
            service_code=PARSE_SERVICE, operation="DOWNLOAD",
            headers={"Accept-Encoding": "identity"},
        )
    content = downloaded.content
    if not content:
        raise BusinessError(
            "EXTERNAL_ARTIFACT_EMPTY",
            "8010 下载的处理产物为空",
            status_code=502,
            details={"service": PARSE_SERVICE, "operation": "DOWNLOAD"},
        )
    processed_filename = f"processed_{Path(filename).name or 'document'}"
    processed_content_type = response_content_type(downloaded, content_type)
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
    base_url = required_service_url(settings.seal_service_url, SEAL_SERVICE, "BAOLI_SEAL_SERVICE_URL")
    with client() as http:
        try:
            with file_path.open("rb") as input_file:
                response = request(
                    http, "POST", f"{base_url}/predict",
                    service_code=SEAL_SERVICE, operation="PREDICT",
                    files={"file": (filename, input_file, content_type)},
                )
        except OSError as exc:
            raise BusinessError(
                "DOCUMENT_SOURCE_MISSING",
                "当前上传文件无法读取，不能执行印章识别",
                status_code=500,
            ) from exc
    payload = json_object(response, SEAL_SERVICE, "PREDICT")
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
                # 供应商字段：class_name=印章类型，text=该印章识别出的主体名称。
                # 代码内部统一称之为主体名称；下方 content.artifact_subject 说明这一归一化，
                # 供下游按语义读取，不需要重新猜哪个字段是主体。
                class_name = non_empty_string(seal["class_name"])
                subject_name = seal["text"]
                bbox_value = seal["bbox"]
                score = _confidence(seal["confidence"])
                if not isinstance(subject_name, str):
                    raise TypeError
                normalized_subject = subject_name.strip()
                bbox = _seal_bbox(bbox_value)
                normalized_seals.append(
                    {
                        "seal_type": class_name,
                        "subject_name": normalized_subject,
                        "bbox": bbox,
                        "confidence": score,
                    }
                )
                if normalized_subject:
                    evidence.append(ExternalEvidence(quote_text=normalized_subject))
            pages.append({"page_no": page_no, "seals_count": seals_count, "seals": normalized_seals})
            total += seals_count
        summary_total = summary["total_seals"]
        if isinstance(summary_total, bool) or not isinstance(summary_total, int) or summary_total < 0 or summary_total != total:
            raise ValueError
    except (KeyError, TypeError, ValueError) as exc:
        raise contract_error(SEAL_SERVICE, "PREDICT") from exc
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
                # 这是本系统对供应商响应中“主体名称”的显式归一化：下游（主体核对）
                # 只读这个结构，不再自己判断 class_name/text 哪个是主体。
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


def _validate_download_url(value: str, service_code: str, operation: str) -> str:
    """校验下载地址的 URL 形式，并原样返回。

    当前只校验 URL 形式：scheme 限定 http/https、必须有主机、不允许携带凭据、不允许片段、端口可解析。
    本函数不限制目标主机，也不做"禁止内网地址"的启发式阻断：允许域名集合应来自真实供应商契约，
    在拿到真实响应样本前不硬编码白名单、不猜测域名。
    "供应商是否保证返回的地址只能指向其可控主机"属于待核实外部契约事项。
    """
    from urllib.parse import urlsplit

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


def _seal_bbox(value: Any) -> list[float] | None:
    if not isinstance(value, list) or len(value) != 4:
        return None
    if any(isinstance(item, bool) or not isinstance(item, (int, float)) or not math.isfinite(float(item)) for item in value):
        return None
    return [float(item) for item in value]
