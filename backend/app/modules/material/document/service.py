"""材料管理列表与详情：SQL 在 crud，本层负责组装视图。"""

from typing import Any

from sqlalchemy.orm import Session

from app.common.exceptions import BusinessError
from app.common.pagination import PageResult, page_result
from app.modules.material.document import crud


class MaterialDocumentService:
    """本实体的服务方法；db 由构造时注入。"""

    def __init__(self, db: Session | None = None) -> None:
        self.db = db

    def processing_notes(self, document_ids: list[int]) -> dict[int, list[str]]:
        notes = {document_id: [] for document_id in document_ids}
        for row in crud.MaterialDocumentCRUD(self.db).quality_attention_rows(document_ids):
            if row.precheck is None or row.precheck["input_revision"] != row.input_revision:
                continue
            block = row.precheck["blocked_materials"].get(row.material_key)
            if block is not None and block["upload_id"] == row.upload_id:
                notes[row.id].extend(f"前置检查停止处理：{item['message']}" for item in block["reasons"])
        stages = {"MATERIAL_CLASSIFY": "材料类型识别", "DOCUMENT_PARSE": "去除印章", "OCR": "正文识别", "SEAL": "印章识别"}
        for row in crud.MaterialDocumentCRUD(self.db).processing_attention_rows(document_ids):
            state = "处理失败" if row.status == "FAILED" else "处理结果待确认"
            notes[row.id].append(f"{stages[row.artifact_type]}{state}：{row.failure_message or '具体原因未记录'}")
        return notes

    def list_documents_page(self, *,
        task_no: str | None,
        task_name: str | None,
        keyword: str | None,
        material_name: str | None,
        has_file: bool | None,
        page: int,
        page_size: int,
    ) -> PageResult[dict[str, Any]]:
        """审核材料分页列表（跨任务）。"""
        rows, total = crud.MaterialDocumentCRUD(self.db).document_page_rows(
            task_no=task_no,
            task_name=task_name,
            keyword=keyword,
            material_name=material_name,
            has_file=has_file,
            page=page,
            page_size=page_size,
        )
        upload_ranks, upload_counts = crud.MaterialDocumentCRUD(self.db).upload_ranks( [row[0].id for row in rows])
        processing_notes = self.processing_notes([row[0].id for row in rows])

        items = []
        for document, row_task_no, row_task_name, upload_id, filename, size_bytes, uploaded_at in rows:
            items.append(
                {
                    "document_no": document.document_no,
                    "task_no": row_task_no,
                    "task_name": row_task_name,
                    "material_key": document.material_key,
                    "material_name": document.material_name,
                    "required": document.required,
                    "status": document.status,
                    "processing_notes": processing_notes[document.id],
                    "filename": filename,
                    "size_bytes": size_bytes,
                    "uploaded_at": uploaded_at,
                    "version_no": upload_ranks.get(upload_id) if upload_id else None,
                    "version_count": upload_counts.get(upload_id, 0) if upload_id else None,
                }
            )
        return page_result(items, total, page, page_size)


    def document_detail(self, document_no: str) -> dict[str, Any]:
        """材料位详情：当前文件与历史上传版本。"""
        document = crud.MaterialDocumentCRUD(self.db).document_by_no( document_no)
        if not document:
            raise BusinessError("TASK_DOCUMENT_NOT_FOUND", "材料位不存在", status_code=404, details={"document_no": document_no})
        task = crud.MaterialDocumentCRUD(self.db).task_by_id( document.audit_task_id)
        uploads = crud.MaterialDocumentCRUD(self.db).uploads_for_document( document.id)
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
            "processing_notes": self.processing_notes([document.id])[document.id],
            "task_no": task.task_no if task else None,
            "task_name": task.task_name if task else None,
            "material_key": document.material_key,
            "material_name": document.material_name,
            "required": document.required,
            "status": document.status,
            "current_upload_no": next((item.upload_no for item in uploads if item.is_current), None),
            "uploads": list(reversed(history)),
        }
