"""外部处理产物的持久化与幂等占位。

外部调用（8018 识别 / 8010 处理 / OCR / 印章识别）的产物一律先落库再使用，
并且**先占位后调用**：占位在联系供应商之前提交，唯一键
(上传, 产物类型, 上传哈希) 保证同一份上传同一类产物只调用一次。

失败语义：

- 只有在能确认供应商**未受理**（调用前的本地错误，见 `PRE_CONTACT_ERROR_CODES`）
  时才删除占位；其余失败一律保留占位并报「结果未知」——自动重发会制造重复外部作业。
- OCR 是异步作业：提交后停在 `SUBMITTING`/`PENDING`，本地等待预算耗尽只代表本次
  操作失败，**不证明**供应商作业已失效，因此不自动重新提交，交人工对账。

本模块不判断业务结论，只回答「产物是否存在、是否可读、内容哈希是什么」。"""

import hashlib, json
from ...domain.identity import public_no
from ...common.exceptions import BusinessError, exception_summary
from ...domain.external_artifacts import ExternalArtifact, ExternalServiceResult, ReadyOcrArtifact, ready_artifact_payload
from .document_ports import document_service
from ...entity import AuditTask, DocumentUpload, StoredArtifact
from ..storage_service import resolve_storage_path
from datetime import UTC, datetime
from pathlib import Path
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session
from typing import Any, Callable


PRE_CONTACT_ERROR_CODES = {
    "EXTERNAL_CONFIG_MISSING",
    "MATERIAL_TYPE_CATALOG_FAILED",
    "PROCESSED_ARTIFACT_MISSING",
    "DOCUMENT_SOURCE_MISSING",
    "MATERIAL_TYPE_MISSING",
    "MATERIAL_TYPE_UNSUPPORTED",
    "OCR_MULTIPART_INVALID",
}

def _artifact_for_upload(db: Session, upload: DocumentUpload, service_code: str) -> StoredArtifact | None:
    return db.scalar(
        select(StoredArtifact).where(
            StoredArtifact.document_upload_id == upload.id,
            StoredArtifact.artifact_type == service_code,
            StoredArtifact.upload_sha256 == upload.sha256,
        )
    )

def _artifact_payload(artifact: StoredArtifact) -> ExternalArtifact:
    try:
        payload = ready_artifact_payload(artifact.metadata_json)
    except ValidationError as exc:
        raise BusinessError(
            "ARTIFACT_METADATA_INVALID",
            "文件处理产物元数据不符合内部契约",
            status_code=500,
            details={"artifact_no": artifact.artifact_no},
        ) from exc
    if payload.status != "SUCCESS":
        raise BusinessError(
            "ARTIFACT_STATUS_INVALID",
            "当前文件处理产物不是成功结果",
            status_code=500,
            details={"artifact_no": artifact.artifact_no},
        )
    return payload

def _artifact_content_digest(content: Any, binary: bytes | None) -> str:
    """产物内容哈希：JSON 产物按规范序列化的字节，二进制产物按真实文件字节。

    它与「上传身份」的哈希是两件事：同一份上传文件重新识别后上传哈希不变，
    产物内容却可能不同。索引必须绑定内容哈希，否则旧主体向量会继续冒充当前产物。
    """
    if binary is not None:
        return hashlib.sha256(binary).hexdigest()
    canonical = json.dumps(content, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

def _persist_external_artifact(
    task: AuditTask,
    upload: DocumentUpload,
    service_code: str,
    result: ExternalServiceResult,
) -> tuple[str, ExternalArtifact]:
    artifact = result.artifact
    if result.binary_content is None:
        relative = f"tasks/{task.task_no}/artifacts/{upload.upload_no}/{service_code.lower()}.json"
        target = resolve_storage_path(relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("w", encoding="utf-8") as output:
            json.dump(artifact.model_dump(mode="json"), output, ensure_ascii=False)
        return relative, artifact

    if service_code != "DOCUMENT_PARSE":
        raise BusinessError("ARTIFACT_BINARY_TYPE_INVALID", "非文件处理服务不能保存二进制产物", status_code=500)
    suffix = Path(result.filename or upload.original_filename).suffix.lower() or ".bin"
    relative = f"tasks/{task.task_no}/artifacts/{upload.upload_no}/document_parse{suffix}"
    target = resolve_storage_path(relative)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("wb") as output:
        output.write(result.binary_content)
    return relative, artifact

def _ensure_external_artifact(
    db: Session,
    task: AuditTask,
    upload: DocumentUpload,
    service_code: str,
    call: Callable[[], ExternalServiceResult],
) -> StoredArtifact:
    """先占位再调用外部服务：唯一键 (上传, 产物类型, sha256) 让同一上传同一产物只调用一次。

    占位在调用前提交，因此并发与进程中断都不会重复产生外部作业：
    已 READY 直接复用；停在 PROCESSING 说明上次调用结果未知，明确报错交人工核对。
    失败时只有在能确认供应商未受理（调用前的本地错误）才删除占位；
    其余情况保留占位，避免自动重发制造重复作业。
    """
    existing = _artifact_for_upload(db, upload, service_code)
    if existing is not None:
        if existing.status == "READY":
            return existing
        raise BusinessError(
            "ARTIFACT_PROCESSING_UNKNOWN",
            f"此前对当前文件的 {service_code} 调用结果未能确认，系统不会自动重复提交外部服务，请人工核对供应商作业状态",
            status_code=409,
            details={"artifact_no": existing.artifact_no, "artifact_type": service_code},
        )
    record = StoredArtifact(
        artifact_no=public_no("ART"),
        audit_task_id=task.id,
        document_upload_id=upload.id,
        artifact_type=service_code,
        relative_path=f"tasks/{task.task_no}/artifacts/{upload.upload_no}/{service_code.lower()}.processing",
        upload_sha256=upload.sha256,
        status="PROCESSING",
        metadata_json={"submission_state": "PROCESSING"},
    )
    db.add(record)
    db.flush()
    db.commit()
    try:
        result = call()
    except BusinessError as exc:
        if exc.code in PRE_CONTACT_ERROR_CODES:
            db.delete(record)
            db.commit()
        raise
    relative, artifact = _persist_external_artifact(task, upload, service_code, result)
    record.relative_path = relative
    record.status = "READY"
    record.metadata_json = artifact.model_dump(mode="json")
    # 同一份上传重新识别后内容可能变化：记录真实内容哈希，让索引能识别出“产物已重建”。
    record.content_sha256 = _artifact_content_digest(record.metadata_json, result.binary_content)
    db.flush()
    return record

def _submit_ocr_job(db: Session, task: AuditTask, upload: DocumentUpload, parsed_artifact: StoredArtifact) -> StoredArtifact:
    existing = _artifact_for_upload(db, upload, "OCR")
    if existing:
        if existing.status == "SUBMITTING":
            raise BusinessError(
                "OCR_SUBMISSION_STATE_UNKNOWN",
                "此前 OCR 提交结果未能确认，系统不会自动重复提交，请人工核对供应商作业状态",
                status_code=409,
            )
        if existing.status == "PENDING":
            _, deadline_at = _ocr_job_metadata(existing)
            if deadline_at.astimezone(UTC) <= datetime.now(UTC):
                raise BusinessError(
                    "OCR_JOB_EXPIRED",
                    "此前 OCR 作业已超过总等待时间，系统不会自动重复提交，请人工核对作业或明确重新上传材料",
                    status_code=409,
                )
        if existing.status not in {"PENDING", "READY"}:
            raise BusinessError("OCR_ARTIFACT_STATUS_INVALID", "当前上传文件的 OCR 作业状态无效", status_code=500)
        return existing
    processed_path, processed_filename = _processed_document_source(parsed_artifact)
    record = StoredArtifact(
        artifact_no=public_no("ART"),
        audit_task_id=task.id,
        document_upload_id=upload.id,
        artifact_type="OCR",
        relative_path=f"tasks/{task.task_no}/artifacts/{upload.upload_no}/ocr.submitting",
        upload_sha256=upload.sha256,
        status="SUBMITTING",
        metadata_json={"submission_state": "SUBMITTING"},
    )
    db.add(record)
    db.flush()
    # 先占位再联系供应商：提交中断是显式状态，绝不能自动重试——供应商可能已经受理。
    db.commit()
    try:
        submission = document_service().submit_processed_document(file_path=processed_path, filename=processed_filename)
    except BusinessError as exc:
        if exc.code in {"EXTERNAL_CONFIG_MISSING", "PROCESSED_ARTIFACT_MISSING", "OCR_MULTIPART_INVALID"}:
            db.delete(record)
            db.commit()
        raise
    record.relative_path = f"tasks/{task.task_no}/artifacts/{upload.upload_no}/ocr.pending"
    record.status = "PENDING"
    record.metadata_json = {
        "job_id": submission.job_id,
        "deadline_at": submission.deadline_at.isoformat(),
    }
    db.commit()
    return record

def _submit_authorized_ocr_retry(
    db: Session,
    task: AuditTask,
    upload: DocumentUpload,
    parsed_artifact: StoredArtifact,
    record: StoredArtifact,
    *,
    idempotency_key: str,
) -> StoredArtifact:
    """执行已持久化的人工重提意图；保留原作业与每次提交尝试。"""
    metadata = record.metadata_json or {}
    attempts = list(metadata["submit_attempts"])
    attempt = dict(attempts[-1])
    if (
        record.status != "SUBMITTING"
        or attempt["idempotency_key"] != idempotency_key
        or attempt["state"] != "SUBMITTING"
    ):
        raise BusinessError("OCR_RETRY_INTENT_INVALID", "OCR 人工重提意图与当前作业状态不一致", status_code=409)

    try:
        processed_path, processed_filename = _processed_document_source(parsed_artifact)
    except Exception as exc:
        attempt = {
            **attempt,
            "state": "PRE_CONTACT_FAILED",
            "failure_code": exc.code if isinstance(exc, BusinessError) else "OCR_RETRY_PREPARE_FAILED",
            "failure_message": exc.message if isinstance(exc, BusinessError) else exception_summary(exc),
        }
        record.status = attempt["previous_status"]
        record.relative_path = attempt["previous_relative_path"]
        record.metadata_json = {
            **attempt["previous_metadata"],
            "submit_attempts": [*attempts[:-1], attempt],
        }
        db.flush()
        raise
    try:
        submission = document_service().submit_processed_document(
            file_path=processed_path, filename=processed_filename
        )
    except BusinessError as exc:
        attempt = {**attempt, "failure_code": exc.code, "failure_message": exc.message}
        if exc.code in {"EXTERNAL_CONFIG_MISSING", "PROCESSED_ARTIFACT_MISSING", "OCR_MULTIPART_INVALID"}:
            attempt["state"] = "PRE_CONTACT_FAILED"
            record.status = attempt["previous_status"]
            record.relative_path = attempt["previous_relative_path"]
            record.metadata_json = {
                **attempt["previous_metadata"],
                "submit_attempts": [*attempts[:-1], attempt],
            }
        else:
            attempt["state"] = "UNKNOWN"
            record.metadata_json = {**metadata, "submit_attempts": [*attempts[:-1], attempt]}
        db.flush()
        raise
    except Exception as exc:
        attempt = {
            **attempt,
            "state": "UNKNOWN",
            "failure_code": "OCR_RETRY_UNEXPECTED_ERROR",
            "failure_message": exception_summary(exc),
        }
        record.metadata_json = {**metadata, "submit_attempts": [*attempts[:-1], attempt]}
        db.flush()
        raise

    attempt = {
        **attempt,
        "state": "PENDING",
        "job_id": submission.job_id,
        "deadline_at": submission.deadline_at.isoformat(),
    }
    record.relative_path = f"tasks/{task.task_no}/artifacts/{upload.upload_no}/ocr.pending"
    record.status = "PENDING"
    record.metadata_json = {
        "job_id": submission.job_id,
        "deadline_at": submission.deadline_at.isoformat(),
        "submit_attempts": [*attempts[:-1], attempt],
    }
    # 供应商已返回作业号时先持久化；结果日志中断也不能丢失可续查的作业身份。
    db.commit()
    return record


def _ocr_job_metadata(artifact: StoredArtifact) -> tuple[str, datetime]:
    if artifact.status != "PENDING" or not isinstance(artifact.metadata_json, dict):
        raise BusinessError("OCR_JOB_METADATA_INVALID", "当前上传文件的 OCR 作业记录无效", status_code=500)
    job_id = artifact.metadata_json.get("job_id")
    deadline_at = artifact.metadata_json.get("deadline_at")
    if not isinstance(job_id, str) or not job_id.strip() or not isinstance(deadline_at, str):
        raise BusinessError("OCR_JOB_METADATA_INVALID", "当前上传文件的 OCR 作业记录不完整", status_code=500)
    try:
        parsed_deadline = datetime.fromisoformat(deadline_at)
    except ValueError as exc:
        raise BusinessError("OCR_JOB_METADATA_INVALID", "当前上传文件的 OCR 作业截止时间无效", status_code=500) from exc
    if parsed_deadline.tzinfo is None:
        raise BusinessError("OCR_JOB_METADATA_INVALID", "当前上传文件的 OCR 作业截止时间缺少时区", status_code=500)
    return job_id.strip(), parsed_deadline

def _complete_ocr_job(db: Session, task: AuditTask, upload: DocumentUpload, artifact: StoredArtifact) -> StoredArtifact:
    if artifact.status == "READY":
        return artifact
    job_id, deadline_at = _ocr_job_metadata(artifact)
    result = document_service().poll_ocr_job(job_id=job_id, deadline_at=deadline_at)
    relative, payload = _persist_external_artifact(task, upload, "OCR", result)
    artifact.relative_path = relative
    artifact.status = "READY"
    current_metadata = artifact.metadata_json if isinstance(artifact.metadata_json, dict) else {}
    artifact.metadata_json = ReadyOcrArtifact(
        artifact=payload,
        job_id=job_id,
        deadline_at=deadline_at,
        submit_attempts=list(current_metadata.get("submit_attempts") or []),
    ).model_dump(mode="json")
    artifact.content_sha256 = _artifact_content_digest(payload.model_dump(mode="json"), result.binary_content)
    db.flush()
    return artifact

def _processed_document_source(artifact: StoredArtifact) -> tuple[Path, str]:
    payload = _artifact_payload(artifact)
    filename = payload.content.get("processed_filename")
    if not isinstance(filename, str) or not filename:
        raise BusinessError(
            "PROCESSED_ARTIFACT_MISSING",
            "当前上传文件缺少可用的 8010 处理产物，无法执行 OCR",
            status_code=409,
        )
    path = resolve_storage_path(artifact.relative_path)
    if not path.is_file():
        raise BusinessError(
            "PROCESSED_ARTIFACT_MISSING",
            "当前上传文件的 8010 处理产物不存在，无法执行 OCR",
            status_code=409,
        )
    return path, filename

def _material_classify_content(db: Session, upload: DocumentUpload) -> dict[str, Any] | None:
    """该上传的 8018 材料识别结果内容；没有产物时返回 None。

    8018 已改为**只记录不判定**：识别结论与置信度只是展示用的事实，不改变材料状态、
    不挂人工复核、不影响任何分支。因此这里既不按 `matches_declared_material` 写材料状态，
    也不据它生成待办——材料类型不符要由人按识别结论另行裁决，不由自动识别代替。
    """
    artifact = _artifact_for_upload(db, upload, "MATERIAL_CLASSIFY")
    if artifact is None or artifact.status != "READY":
        return None
    payload = _artifact_payload(artifact)
    if not isinstance(payload.content.get("matches_declared_material"), bool):
        raise BusinessError("MATERIAL_CLASSIFY_CONTRACT_INVALID", "材料识别服务未返回类型匹配结论")
    return payload.content

def _artifact_page_text(artifact: StoredArtifact) -> dict[int, str] | None:
    """产物内每页可定位的文本；产物只提供结构化摘要（没有页文本）时返回 None。"""
    content = _artifact_payload(artifact).content
    pages = content.get("pages")
    if not isinstance(pages, list) or not pages:
        return None
    index: dict[int, str] = {}
    for item in pages:
        if not isinstance(item, dict):
            return None
        page_no = item.get("page_no")
        if isinstance(page_no, bool) or not isinstance(page_no, int):
            return None
        text = item.get("text")
        index[page_no] = text if isinstance(text, str) else ""
    if not any(text.strip() for text in index.values()):
        # 例如印章识别只返回数量与坐标，没有页文本：没有可核对的引文依据。
        return None
    return index
