"""材料管理列表、来源关系与处理详情查询。"""

from typing import Any

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from ...common.exceptions import BusinessError
from ...common.pagination import PageResult, page_result
from ...entity.tasks import AuditTask, DocumentUpload, TaskDocument


def list_documents_page(
    db: Session,
    *,
    task_no: str | None,
    task_name: str | None,
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
    if task_name:
        conditions.append(AuditTask.task_name.like(f"%{task_name.strip()}%"))
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
            DocumentUpload.id.is_not(None) if has_file else DocumentUpload.id.is_(None)
        )

    current_upload_join = and_(
        DocumentUpload.task_document_id == TaskDocument.id,
        DocumentUpload.is_current.is_(True),
    )
    count_statement = (
        select(func.count())
        .select_from(TaskDocument)
        .join(AuditTask, AuditTask.id == TaskDocument.audit_task_id)
        .outerjoin(DocumentUpload, current_upload_join)
        .where(*conditions)
    )
    total = db.scalar(count_statement) or 0
    # 只取列表用到的列：整行 AuditTask 会带上 manifest / task_schema 等大 JSON，
    # 随连接进入排序缓冲；列表只需要任务编号与名称。
    rows = db.execute(
        select(
            TaskDocument,
            AuditTask.task_no,
            AuditTask.task_name,
            DocumentUpload.id.label("upload_id"),
            DocumentUpload.original_filename,
            DocumentUpload.size_bytes,
            DocumentUpload.created_at.label("uploaded_at"),
        )
        .join(AuditTask, AuditTask.id == TaskDocument.audit_task_id)
        .outerjoin(DocumentUpload, current_upload_join)
        .where(*conditions)
        .order_by(TaskDocument.updated_at.desc(), TaskDocument.id.desc())
        .limit(page_size)
        .offset((page - 1) * page_size)
    ).all()

    document_ids = [row[0].id for row in rows]
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
    for document, task_no, task_name, upload_id, filename, size_bytes, uploaded_at in rows:
        items.append(
            {
                "document_no": document.document_no,
                "task_no": task_no,
                "task_name": task_name,
                "material_key": document.material_key,
                "material_name": document.material_name,
                "required": document.required,
                "status": document.status,
                "filename": filename,
                "size_bytes": size_bytes,
                "uploaded_at": uploaded_at,
                "version_no": upload_ranks.get(upload_id) if upload_id else None,
                "version_count": upload_counts.get(upload_id, 0) if upload_id else None,
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
