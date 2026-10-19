"""材料管理列表、来源关系与处理详情查询。"""

from typing import Any
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session
from ...common.pagination import PageResult, page_result
from ...common.exceptions import BusinessError
from ...entity import AuditTask, DocumentUpload, TaskDocument


def list_documents_page(
    db: Session,
    *,
    task_no: str | None,
    keyword: str | None,
    material_name: str | None,
    has_file: bool | None,
    page: int,
    page_size: int,
) -> PageResult[dict[str, Any]]:
    """审核材料分页列表（跨任务）。"""
    conditions = []
    if task_no:
        conditions.append(AuditTask.task_no == task_no)
    if keyword:
        like = f"%{keyword.strip()}%"
        conditions.append(
            or_(
                TaskDocument.material_name.like(like),
                TaskDocument.document_no.like(like),
                DocumentUpload.original_filename.like(like),
            )
        )
    if material_name:
        conditions.append(TaskDocument.material_name.like(f"%{material_name.strip()}%"))
    if has_file is not None:
        conditions.append(
            TaskDocument.current_upload_id.is_not(None) if has_file else TaskDocument.current_upload_id.is_(None)
        )

    count_statement = (
        select(func.count())
        .select_from(TaskDocument)
        .join(AuditTask, AuditTask.id == TaskDocument.audit_task_id)
        .outerjoin(DocumentUpload, DocumentUpload.id == TaskDocument.current_upload_id)
        .where(*conditions)
    )
    total = db.scalar(count_statement) or 0
    rows = db.execute(
        select(TaskDocument, AuditTask, DocumentUpload)
        .join(AuditTask, AuditTask.id == TaskDocument.audit_task_id)
        .outerjoin(DocumentUpload, DocumentUpload.id == TaskDocument.current_upload_id)
        .where(*conditions)
        .order_by(TaskDocument.updated_at.desc(), TaskDocument.id.desc())
        .limit(page_size)
        .offset((page - 1) * page_size)
    ).all()

    document_ids = [document.id for document, _, _ in rows]
    upload_ranks: dict[int, int] = {}
    upload_counts: dict[int, int] = {}
    if document_ids:
        ranks = db.execute(
            select(
                DocumentUpload.id,
                func.row_number()
                .over(partition_by=DocumentUpload.task_document_id, order_by=DocumentUpload.id)
                .label("rank"),
                func.count().over(partition_by=DocumentUpload.task_document_id).label("total"),
            ).where(DocumentUpload.task_document_id.in_(document_ids))
        ).all()
        for upload_id, rank, count in ranks:
            upload_ranks[int(upload_id)] = int(rank)
            upload_counts[int(upload_id)] = int(count)

    items = []
    for document, task, upload in rows:
        items.append(
            {
                "document_no": document.document_no,
                "task_no": task.task_no,
                "task_name": task.task_name,
                "material_key": document.material_key,
                "material_name": document.material_name,
                "required": document.required,
                "status": document.status,
                "filename": upload.original_filename if upload else None,
                "size_bytes": upload.size_bytes if upload else None,
                "uploaded_at": upload.created_at if upload else None,
                "version_no": upload_ranks.get(upload.id) if upload else None,
                "version_count": upload_counts.get(upload.id, 0) if upload else None,
            }
        )
    return page_result(items, total, page, page_size)


def document_detail(db: Session, document_no: str) -> dict[str, Any]:
    """材料位详情：当前文件与历史上传版本。"""
    document = db.scalar(select(TaskDocument).where(TaskDocument.document_no == document_no))
    if not document:
        raise BusinessError("TASK_DOCUMENT_NOT_FOUND", "材料位不存在", status_code=404, details={"document_no": document_no})
    task = db.get(AuditTask, document.audit_task_id)
    uploads = db.scalars(
        select(DocumentUpload)
        .where(DocumentUpload.task_document_id == document.id)
        .order_by(DocumentUpload.id)
    ).all()
    history = []
    for rank, upload in enumerate(uploads, start=1):
        history.append(
            {
                "upload_no": upload.upload_no,
                "filename": upload.original_filename,
                "size_bytes": upload.size_bytes,
                "content_type": upload.content_type,
                "status": upload.status,
                "is_current": upload.is_current,
                "version_no": rank,
                "created_at": upload.created_at,
            }
        )
    return {
        "document_no": document.document_no,
        "task_no": task.task_no if task else None,
        "task_name": task.task_name if task else None,
        "material_key": document.material_key,
        "material_name": document.material_name,
        "required": document.required,
        "status": document.status,
        "current_upload_no": next((item.upload_no for item in uploads if item.is_current), None),
        "uploads": list(reversed(history)),
    }
