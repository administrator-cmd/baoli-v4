"""任务材料外部处理产物与 OCR 状态视图。"""

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ....common.exceptions import BusinessError
from ....entity.tasks import DocumentUpload, StoredArtifact, TaskDocument
from .lookup import task_or_404

MATERIAL_ARTIFACT_TYPES = ("MATERIAL_CLASSIFY", "DOCUMENT_PARSE", "OCR", "SEAL")


def material_artifacts_summary(
    db: Session, upload_ids: list[int]
) -> dict[int, dict[str, dict[str, Any]]]:
    """按当前上传批量汇总四项处理产物状态，供任务详情一次返回。

    只读数据库，不触发外部请求；同一上传同一类型取最新一条记录。
    """
    if not upload_ids:
        return {}
    rows = db.execute(
        select(
            StoredArtifact.id,
            StoredArtifact.document_upload_id,
            StoredArtifact.artifact_type,
            StoredArtifact.status,
            StoredArtifact.artifact_no,
            StoredArtifact.failure_message,
            StoredArtifact.external_job_id,
            StoredArtifact.deadline_at,
            StoredArtifact.metadata_json,
        )
        .where(
            StoredArtifact.document_upload_id.in_(upload_ids),
            StoredArtifact.artifact_type.in_(MATERIAL_ARTIFACT_TYPES),
        )
        .order_by(StoredArtifact.id)
    ).all()
    latest: dict[tuple[int, str], Any] = {}
    for row in rows:
        latest[(row.document_upload_id, row.artifact_type)] = row
    summary: dict[int, dict[str, dict[str, Any]]] = {}
    for (upload_id, artifact_type), row in latest.items():
        metadata = row.metadata_json if isinstance(row.metadata_json, dict) else {}
        entry: dict[str, Any] = {
            "status": row.status,
            "artifact_no": row.artifact_no,
            "message": row.failure_message or _artifact_status_message(row.status),
            # 统一用 ISO 文本，前端按字符串展示，不依赖响应模型的日期序列化。
            "deadline_at": row.deadline_at.isoformat() if row.deadline_at else None,
        }
        if artifact_type == "OCR":
            entry["job_id"] = row.external_job_id
            # 供应商侧状态用于区分排队与识别中；只返回已保存的真实值。
            entry["provider_state"] = metadata.get("provider_state")
            entry["extract_progress"] = metadata.get("extract_progress")
            entry["progress_updated_at"] = metadata.get("progress_updated_at")
        summary.setdefault(upload_id, {})[artifact_type] = entry
    return summary


def _artifact_status_message(status: str) -> str:
    return {
        "READY": "已就绪",
        "PENDING": "作业已提交，等待供应商完成",
        "SUBMITTING": "提交结果未知，可确认后重新提交",
        "PROCESSING": "处理结果未知，可确认后重试此项",
        "FAILED": "处理失败，可在处理状态中重试此项",
        "INVALIDATED": "已随材料替换失效",
    }.get(status, f"处理产物状态：{status}")


def external_artifact_status_view(
    db: Session, task_no: str, document_no: str, artifact_type: str
) -> dict[str, Any]:
    """查询当前上传版本的 8018/8010/8003 产物状态，不触发外部请求。"""
    if artifact_type not in {"MATERIAL_CLASSIFY", "DOCUMENT_PARSE", "SEAL"}:
        raise BusinessError("ARTIFACT_TYPE_INVALID", "仅支持查询材料校验、去章或印章识别产物", status_code=400)
    task = task_or_404(db, task_no)
    document = db.scalar(
        select(TaskDocument).where(
            TaskDocument.audit_task_id == task.id,
            TaskDocument.document_no == document_no,
        )
    )
    if document is None:
        raise BusinessError("TASK_DOCUMENT_NOT_FOUND", "任务材料位不存在", status_code=404)
    base = {
        "document_no": document_no,
        "artifact_type": artifact_type,
        "artifact_no": None,
    }
    if document.current_upload_id is None:
        return {**base, "status": "NOT_SUBMITTED", "message": "该材料位尚未上传文件"}
    upload = db.get(DocumentUpload, document.current_upload_id)
    if upload is None:
        raise BusinessError("TASK_DOCUMENT_UPLOAD_MISSING", "当前上传文件不存在", status_code=500)
    artifact = db.scalar(
        select(StoredArtifact).where(
            StoredArtifact.document_upload_id == upload.id,
            StoredArtifact.artifact_type == artifact_type,
            StoredArtifact.upload_sha256 == upload.sha256,
        )
    )
    if artifact is None:
        return {**base, "status": "NOT_SUBMITTED", "message": "当前上传文件尚无该处理产物"}
    return {
        **base,
        "artifact_no": artifact.artifact_no,
        "status": artifact.status,
        "message": (
            str(artifact.failure_message or "处理结果未知，可确认可能重复调用后重试此项")
            if artifact.status == "PROCESSING"
            else "处理产物已就绪" if artifact.status == "READY"
            else str(artifact.failure_message or f"处理产物状态：{artifact.status}")
        ),
    }


def ocr_status_view(db: Session, task_no: str, document_no: str) -> dict[str, Any]:
    """当前上传文件对应的 OCR 产物只读状态。

    只返回真实记录的产物状态；没有产物时明确说明“尚未提交”，不编造作业号或截止时间。
    job_id / deadline_at 直接取自产物元数据，submit_attempts 缺失时保持 None。
    """
    task = task_or_404(db, task_no)
    document = db.scalar(
        select(TaskDocument).where(
            TaskDocument.audit_task_id == task.id,
            TaskDocument.document_no == document_no,
        )
    )
    if not document:
        raise BusinessError("TASK_DOCUMENT_NOT_FOUND", "任务材料位不存在", status_code=404)
    base = {
        "document_no": document.document_no,
        "artifact_no": None,
        "job_id": None,
        "deadline_at": None,
        "submit_attempts": None,
    }
    if document.current_upload_id is None:
        return {**base, "status": "NOT_SUBMITTED", "message": "该材料位尚未上传文件，没有 OCR 产物"}
    # OCR 正文较大，先取最新记录 ID，再按主键读取，避免 MySQL 对整行 JSON 排序。
    artifact_id = db.scalar(
        select(func.max(StoredArtifact.id))
        .where(
            StoredArtifact.document_upload_id == document.current_upload_id,
            StoredArtifact.artifact_type == "OCR",
        )
    )
    artifact = db.get(StoredArtifact, artifact_id) if artifact_id is not None else None
    if artifact is None:
        return {**base, "status": "NOT_SUBMITTED", "message": "当前上传文件尚未提交 OCR 作业"}
    metadata = artifact.metadata_json if isinstance(artifact.metadata_json, dict) else {}
    job_id = artifact.external_job_id
    deadline_at = artifact.deadline_at
    attempts = metadata.get("submit_attempts")
    message = {
        "SUBMITTING": "OCR 提交结果未知，可点击「重新提交 OCR」；需确认可能重复创建云端作业",
        "PENDING": "OCR 作业已提交，等待供应商完成",
        "READY": "OCR 产物已就绪",
        "FAILED": str(artifact.failure_message or "OCR 作业失败"),
        "INVALIDATED": "OCR 产物已随材料替换失效",
    }.get(artifact.status, f"OCR 产物状态：{artifact.status}")
    return {
        "document_no": document.document_no,
        "artifact_no": artifact.artifact_no,
        "status": artifact.status,
        "job_id": job_id if isinstance(job_id, str) else None,
        "deadline_at": deadline_at,
        "submit_attempts": len(attempts) if isinstance(attempts, list) else attempts if isinstance(attempts, int) else None,
        "provider_state": metadata.get("provider_state"),
        "extract_progress": metadata.get("extract_progress"),
        "progress_updated_at": metadata.get("progress_updated_at"),
        "message": message,
    }
