"""外部处理产物的持久化与幂等占位。

外部调用（8018 识别 / 8010 处理 / OCR / 印章识别）的产物一律先落库再使用，并且先占位后调用：
占位在联系供应商之前提交，唯一键 (上传, 产物类型) 保证同一份上传同一类产物只调用一次。

8018/8010/8003 失败语义：调用前错误与供应商明确拒绝保存 FAILED；其他失败保留 PROCESSING
并保存真实异常，表示结果未知。用户可单项重试，未知结果须明确确认，系统不自动重发。
OCR 是异步作业：
本地等待预算耗尽只代表本次操作失败，不证明供应商作业已失效；用户可通过单份重试续查原作业。
供应商明确返回 failed 时保存 FAILED 终态与原因，用户可通过重新提交入口创建新作业。
提交结果未知时不自动重发，允许用户确认可能重复创建作业后，单独重新提交该材料的 OCR。

本模块不判断业务结论，只回答「产物是否存在、是否可读、内容哈希是什么」。"""

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from ...common.exceptions import BusinessError, exception_summary
from ...domain.external_artifacts import ExternalArtifact, ExternalServiceResult
from ...domain.identity import public_no
from ...entity.tasks import AuditTask, DocumentUpload, StoredArtifact
from ..shared.storage import resolve_storage_path
from .document_ports import document_service

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
        )
    )

def _artifact_content_path(artifact: StoredArtifact) -> Path:
    """产物信封的落盘位置：与 relative_path 同目录、按产物类型命名的 JSON。"""
    return Path(artifact.relative_path).parent / f"{artifact.artifact_type.lower()}.json"


def _artifact_payload(artifact: StoredArtifact) -> ExternalArtifact:
    """读取磁盘上的产物信封；文件缺失或结构不符时明确报错。"""
    path = resolve_storage_path(str(_artifact_content_path(artifact)))
    if not path.is_file():
        raise BusinessError(
            "ARTIFACT_CONTENT_MISSING",
            "文件处理产物的内容文件不存在，需要重新执行该材料的处理",
            status_code=500,
            details={"artifact_no": artifact.artifact_no, "relative_path": str(path)},
        )
    try:
        with path.open(encoding="utf-8") as content:
            payload = ExternalArtifact.model_validate(json.load(content))
    except (OSError, ValueError, ValidationError) as exc:
        raise BusinessError(
            "ARTIFACT_METADATA_INVALID",
            "文件处理产物内容不符合内部契约",
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

    它与上传文件的 sha256 是两件事：同一份上传文件重新识别后上传哈希不变，
    产物内容却可能不同。内容哈希用于判断产物是否更新。
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
    """写产物信封；二进制产物（8010）另写二进制文件，relative_path 指向二进制。

    信封与二进制同目录，`_artifact_payload` 按产物类型定位信封，不依赖数据库 JSON。
    """
    artifact = result.artifact
    directory = resolve_storage_path(f"tasks/{task.task_no}/artifacts/{upload.upload_no}")
    directory.mkdir(parents=True, exist_ok=True)
    envelope = directory / f"{service_code.lower()}.json"
    with envelope.open("w", encoding="utf-8") as output:
        json.dump(artifact.model_dump(mode="json"), output, ensure_ascii=False)
    if result.binary_content is None:
        if service_code == "DOCUMENT_PARSE":
            raise BusinessError("ARTIFACT_BINARY_MISSING", "文件处理服务未返回可用的去章文件", status_code=500)
        return envelope.relative_to(resolve_storage_path(".")).as_posix(), artifact
    if service_code != "DOCUMENT_PARSE":
        raise BusinessError("ARTIFACT_BINARY_TYPE_INVALID", "非文件处理服务不能保存二进制产物", status_code=500)
    suffix = Path(result.filename or upload.original_filename).suffix.lower() or ".bin"
    binary = directory / f"document_parse{suffix}"
    with binary.open("wb") as output:
        output.write(result.binary_content)
    return str(binary.relative_to(resolve_storage_path(".")).as_posix()), artifact

def _ensure_external_artifact(
    db: Session,
    task: AuditTask,
    upload: DocumentUpload,
    service_code: str,
    call: Callable[[], ExternalServiceResult],
) -> StoredArtifact:
    """先占位再调用外部服务：唯一键 (上传, 产物类型) 让同一上传同一产物只调用一次。

    占位在调用前提交，因此并发与进程中断都不会重复产生外部作业：
    已 READY 直接复用；FAILED 或 PROCESSING 明确报错，由材料弹窗单项重试。
    占位与每次真实调用历史均保留，避免自动重发制造重复作业。
    """
    existing = _artifact_for_upload(db, upload, service_code)
    if existing is not None:
        if existing.status == "READY":
            return existing
        if existing.status == "FAILED":
            raise BusinessError("ARTIFACT_PROCESSING_FAILED", "此前该项处理失败，请在材料处理状态中重试此项", status_code=409,
                                details={"artifact_no": existing.artifact_no, "artifact_type": service_code,
                                         "failure_message": existing.failure_message})
        raise BusinessError(
            "ARTIFACT_PROCESSING_UNKNOWN",
            f"此前对当前文件的 {service_code} 调用结果未能确认，系统不会自动重复提交外部服务，请在材料处理状态中确认后重试此项",
            status_code=409,
            details={"artifact_no": existing.artifact_no, "artifact_type": service_code},
        )
    record = StoredArtifact(
        artifact_no=public_no("ART"),
        audit_task_id=task.id,
        document_upload_id=upload.id,
        artifact_type=service_code,
        relative_path=f"tasks/{task.task_no}/artifacts/{upload.upload_no}/{service_code.lower()}.processing",
        status="PROCESSING",
        metadata_json={"submission_state": "PROCESSING"},
    )
    db.add(record)
    db.flush()
    db.commit()
    return execute_external_artifact_call(db, task, upload, record, call)


def execute_external_artifact_call(db, task, upload, record, call, *, operation_no=None, resubmit_unknown=False):
    """联系服务前记录本次调用，保留失败原因与用户确认的重试意图。"""
    metadata = record.metadata_json or {}
    attempts = list(metadata.get("processing_attempts") or [])
    attempt = {"operation_no": operation_no, "started_at": datetime.now(UTC).isoformat(),
               "previous_status": record.status, "state": "PROCESSING", "resubmit_unknown": resubmit_unknown}
    attempts.append(attempt)
    record.status = "PROCESSING"
    record.failure_code = None
    record.failure_message = None
    record.metadata_json = {**metadata, "processing_attempts": attempts}
    db.commit()
    try:
        result = call()
        relative, artifact = _persist_external_artifact(task, upload, record.artifact_type, result)
        content = artifact.model_dump(mode="json")
        record.relative_path = relative
        record.content_sha256 = _artifact_content_digest(content, result.binary_content)
        record.status = "READY"
        record.failure_code = None
        record.failure_message = None
        attempts[-1] = {**attempt, "state": "READY", "finished_at": datetime.now(UTC).isoformat()}
        record.metadata_json = {**metadata, "processing_attempts": attempts}
        db.flush()
    except Exception as exc:
        db.rollback()
        # 调用前错误和明确拒绝属于已知失败；网络或落盘失败无法证明远端未处理。
        known_failure = isinstance(exc, BusinessError) and exc.code in PRE_CONTACT_ERROR_CODES | {"EXTERNAL_SERVICE_REJECTED"}
        record.status = "FAILED" if known_failure else "PROCESSING"
        attempts[-1] = {**attempt, "state": "FAILED" if known_failure else "UNKNOWN",
                        "finished_at": datetime.now(UTC).isoformat(),
                        "failure_code": exc.code if isinstance(exc, BusinessError) else type(exc).__name__,
                        "failure_message": exc.message if isinstance(exc, BusinessError) else exception_summary(exc)}
        record.failure_code = attempts[-1]["failure_code"]
        record.failure_message = attempts[-1]["failure_message"]
        record.metadata_json = {**metadata, "processing_attempts": attempts}
        db.commit()
        raise
    return record


def _submit_ocr_job(db: Session, task: AuditTask, upload: DocumentUpload, parsed_artifact: StoredArtifact) -> StoredArtifact:
    existing = _artifact_for_upload(db, upload, "OCR")
    if existing:
        if existing.status == "FAILED":
            raise BusinessError(
                "OCR_JOB_FAILED",
                "当前 OCR 作业已失败，请在材料处理状态中使用单份重试后再执行审核",
                status_code=409,
                details={"artifact_no": existing.artifact_no, "job_id": existing.external_job_id,
                         "failure_message": existing.failure_message},
            )
        if existing.status == "SUBMITTING":
            raise BusinessError(
                "OCR_SUBMISSION_STATE_UNKNOWN",
                "此前 OCR 提交结果未能确认，请在材料处理状态中确认后重新提交该材料的 OCR；系统不会自动重提",
                status_code=409,
            )
        if existing.status == "PENDING":
            _, deadline_at = _ocr_job_metadata(existing)
            if deadline_at.astimezone(UTC) <= datetime.now(UTC):
                raise BusinessError(
                    "OCR_JOB_EXPIRED",
                    "此前 OCR 作业已超过总等待时间，请在材料处理状态中使用单份重试继续查询原作业",
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
        status="SUBMITTING",
        metadata_json={"submission_state": "SUBMITTING"},
    )
    db.add(record)
    db.flush()
    # 先占位再联系供应商：提交中断是显式状态，绝不能自动重试——供应商可能已经受理。
    db.commit()
    try:
        submission = document_service().submit_processed_document(
            file_path=processed_path, filename=processed_filename,
        )
    except BusinessError as exc:
        if exc.code in {"EXTERNAL_CONFIG_MISSING", "PROCESSED_ARTIFACT_MISSING", "OCR_MULTIPART_INVALID"}:
            db.delete(record)
            db.commit()
        else:
            record.failure_code = exc.code
            record.failure_message = exc.message
            db.commit()
        raise
    record.relative_path = f"tasks/{task.task_no}/artifacts/{upload.upload_no}/ocr.pending"
    record.status = "PENDING"
    record.external_job_id = submission.job_id
    record.deadline_at = submission.deadline_at
    record.failure_code = None
    record.failure_message = None
    record.metadata_json = {"submission_state": "PENDING"}
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
    """执行已持久化的单份重试提交意图；保留原作业与每次提交尝试。"""
    metadata = record.metadata_json or {}
    attempts = list(metadata["submit_attempts"])
    attempt = dict(attempts[-1])
    if (
        record.status != "SUBMITTING"
        or attempt["idempotency_key"] != idempotency_key
        or attempt["state"] != "SUBMITTING"
    ):
        raise BusinessError("OCR_RETRY_INTENT_INVALID", "OCR 单份重试提交意图与当前作业状态不一致", status_code=409)

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
        # 未联系供应商就失败：产物恢复为原状态与原失败信息，本次失败只记在提交历史里。
        _restore_job_columns(record, attempt)
        record.metadata_json = {
            **attempt["previous_metadata"],
            "submit_attempts": [*attempts[:-1], attempt],
        }
        db.flush()
        raise
    try:
        submission = document_service().submit_processed_document(
            file_path=processed_path, filename=processed_filename,
        )
    except BusinessError as exc:
        attempt = {**attempt, "failure_code": exc.code, "failure_message": exc.message}
        if exc.code in {"EXTERNAL_CONFIG_MISSING", "PROCESSED_ARTIFACT_MISSING", "OCR_MULTIPART_INVALID"}:
            attempt["state"] = "PRE_CONTACT_FAILED"
            record.status = attempt["previous_status"]
            record.relative_path = attempt["previous_relative_path"]
            _restore_job_columns(record, attempt)
            record.metadata_json = {
                **attempt["previous_metadata"],
                "submit_attempts": [*attempts[:-1], attempt],
            }
        else:
            # 已联系供应商但结果未知：保留本次失败信息，状态位 SUBMITTING。
            attempt["state"] = "UNKNOWN"
            record.failure_code = exc.code
            record.failure_message = exc.message
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
        record.failure_code = attempt["failure_code"]
        record.failure_message = attempt["failure_message"]
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
    record.external_job_id = submission.job_id
    record.deadline_at = submission.deadline_at
    record.failure_code = None
    record.failure_message = None
    record.metadata_json = {"submit_attempts": [*attempts[:-1], attempt]}
    # 供应商已返回作业号时先持久化；结果日志中断也不能丢失可续查的作业身份。
    db.commit()
    return record


def _restore_job_columns(record: StoredArtifact, attempt: dict[str, Any]) -> None:
    """提交前失败时恢复原作业号、截止时间与失败信息。"""
    record.external_job_id = attempt.get("previous_external_job_id")
    previous_deadline = attempt.get("previous_deadline_at")
    if isinstance(previous_deadline, str):
        parsed = datetime.fromisoformat(previous_deadline)
        # 历史 metadata 里可能是不带时区的 UTC 文本；数据库列统一按 UTC 存储。
        record.deadline_at = parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
    else:
        record.deadline_at = None
    record.failure_code = attempt.get("previous_failure_code")
    record.failure_message = attempt.get("previous_failure_message")


def _ocr_job_metadata(artifact: StoredArtifact) -> tuple[str, datetime]:
    if artifact.status != "PENDING":
        raise BusinessError("OCR_JOB_METADATA_INVALID", "当前上传文件的 OCR 作业状态无效", status_code=500)
    job_id = artifact.external_job_id
    deadline_at = artifact.deadline_at
    if not isinstance(job_id, str) or not job_id.strip() or deadline_at is None:
        raise BusinessError("OCR_JOB_METADATA_INVALID", "当前上传文件的 OCR 作业记录不完整", status_code=500)
    if deadline_at.tzinfo is None:
        raise BusinessError("OCR_JOB_METADATA_INVALID", "当前上传文件的 OCR 作业截止时间缺少时区", status_code=500)
    return job_id.strip(), deadline_at

def _complete_ocr_job(db: Session, task: AuditTask, upload: DocumentUpload, artifact: StoredArtifact) -> StoredArtifact:
    if artifact.status == "READY":
        return artifact
    job_id, deadline_at = _ocr_job_metadata(artifact)

    def save_progress(progress: dict[str, Any]) -> None:
        artifact.metadata_json = {
            **(artifact.metadata_json or {}), **progress,
            "progress_updated_at": datetime.now(UTC).isoformat(),
        }
        db.commit()

    try:
        result = document_service().poll_ocr_job(job_id=job_id, deadline_at=deadline_at, on_progress=save_progress)
    except BusinessError as exc:
        if exc.code == "OCR_JOB_FAILED":
            # 供应商明确的失败终态必须保存；网络错误或本地超时仍保留可续查作业。
            artifact.status = "FAILED"
            artifact.failure_code = exc.code
            artifact.failure_message = exc.message
            artifact.metadata_json = {
                **(artifact.metadata_json or {}),
                "provider_state": "failed",
                "provider_code": exc.details.get("provider_code"),
                "provider_error": exc.details.get("provider_error"),
                "provider_trace_id": exc.details.get("provider_trace_id"),
                "failed_at": datetime.now(UTC).isoformat(),
            }
            db.commit()
        raise
    relative, payload = _persist_external_artifact(task, upload, "OCR", result)
    artifact.relative_path = relative
    artifact.status = "READY"
    artifact.failure_code = None
    artifact.failure_message = None
    current_metadata = artifact.metadata_json if isinstance(artifact.metadata_json, dict) else {}
    artifact.metadata_json = {
        "submit_attempts": list(current_metadata.get("submit_attempts") or []),
        "provider_state": current_metadata.get("provider_state"),
        "extract_progress": current_metadata.get("extract_progress"),
        "progress_updated_at": current_metadata.get("progress_updated_at"),
    }
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

    8018 已改为只记录不判定：识别结论与置信度只是展示用的事实，不改变材料状态、
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
