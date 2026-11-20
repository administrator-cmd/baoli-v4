"""材料位查询的 SQL 层：只取实体与行，不组装视图。"""

from sqlalchemy import and_, func, or_, select, update

from app.constants.status import ACTIVE_OPERATION_STATUSES
from app.core.base_crud import CRUDBase
from app.domain.materials.material_precheck import PRECHECK_KEY
from app.modules.audit.artifact.model import StoredArtifact
from app.modules.audit.execution.model import NodeExecution
from app.modules.audit.task.model import AuditTask
from app.modules.material.document.model import DocumentUpload, TaskDocument
from app.modules.system.operation.model import BackgroundOperation


class MaterialDocumentCRUD(CRUDBase):
    model = TaskDocument

    def document_page_rows(self, *,
        task_no: str | None,
        task_name: str | None,
        keyword: str | None,
        material_name: str | None,
        has_file: bool | None,
        page: int,
        page_size: int,
    ):
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
        total = self.db.scalar(count_statement) or 0
        # 只取列表用到的列：整行 AuditTask 会带上 manifest / task_schema 等大 JSON，
        # 随连接进入排序缓冲；列表只需要任务编号与名称。
        rows = self.db.execute(
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
        return list(rows), int(total)


    def upload_ranks(self, document_ids: list[int]) -> tuple[dict[int, int], dict[int, int]]:
        """每个上传在该材料位内的序号与总数。"""
        if not document_ids:
            return {}, {}
        ranks: dict[int, int] = {}
        counts: dict[int, int] = {}
        for upload_id, rank, count in self.db.execute(
            select(
                DocumentUpload.id,
                func.row_number()
                .over(partition_by=DocumentUpload.task_document_id, order_by=DocumentUpload.id)
                .label("rank"),
                func.count().over(partition_by=DocumentUpload.task_document_id).label("total"),
            ).where(DocumentUpload.task_document_id.in_(document_ids))
        ).all():
            ranks[int(upload_id)] = int(rank)
            counts[int(upload_id)] = int(count)
        return ranks, counts


    def document_by_no(self, document_no: str) -> TaskDocument | None:
        return self.db.scalar(select(TaskDocument).where(TaskDocument.document_no == document_no))

    def processing_attention_rows(self, document_ids: list[int]):
        active_task_ids = (
            select(AuditTask.id)
            .join(BackgroundOperation, BackgroundOperation.object_no == AuditTask.task_no)
            .where(BackgroundOperation.object_type == "AUDIT_TASK", BackgroundOperation.status.in_(ACTIVE_OPERATION_STATUSES))
        )
        return self.db.execute(
            select(TaskDocument.id, StoredArtifact.artifact_type, StoredArtifact.status, StoredArtifact.failure_message)
            .join(DocumentUpload, DocumentUpload.task_document_id == TaskDocument.id)
            .join(StoredArtifact, StoredArtifact.document_upload_id == DocumentUpload.id)
            .where(TaskDocument.id.in_(document_ids), DocumentUpload.is_current.is_(True),
                   or_(StoredArtifact.status == "FAILED",
                       and_(StoredArtifact.status.in_(["PROCESSING", "SUBMITTING"]),
                            TaskDocument.audit_task_id.not_in(active_task_ids))))
        ).all()

    def quality_attention_rows(self, document_ids: list[int]):
        precheck = (
            select(NodeExecution.result_json[PRECHECK_KEY])
            .where(NodeExecution.audit_task_id == AuditTask.id,
                   NodeExecution.status != "INVALIDATED")
            .order_by(NodeExecution.id.desc()).limit(1).correlate(AuditTask).scalar_subquery()
        )
        return self.db.execute(
            select(TaskDocument.id, TaskDocument.material_key, AuditTask.input_revision,
                   DocumentUpload.id.label("upload_id"), precheck.label("precheck"))
            .join(AuditTask, AuditTask.id == TaskDocument.audit_task_id)
            .join(DocumentUpload, DocumentUpload.task_document_id == TaskDocument.id)
            .where(TaskDocument.id.in_(document_ids), DocumentUpload.is_current.is_(True))
        ).all()


    def task_by_id(self, task_id: int) -> AuditTask | None:
        return self.db.get(AuditTask, task_id)


    def uploads_for_document(self, document_id: int) -> list[DocumentUpload]:
        return list(
            self.db.scalars(
                select(DocumentUpload)
                .where(DocumentUpload.task_document_id == document_id)
                .order_by(DocumentUpload.id)
            ).all()
        )

    def by_task_and_no(self, task_id: int, document_no: str) -> TaskDocument | None:
        return self.db.scalar(
            select(TaskDocument).where(
                TaskDocument.audit_task_id == task_id,
                TaskDocument.document_no == document_no,
            )
        )

    def by_task_and_material(self, task_id: int, material_key: str) -> TaskDocument | None:
        return self.db.scalar(
            select(TaskDocument).where(
                TaskDocument.audit_task_id == task_id,
                TaskDocument.material_key == material_key,
            )
        )

    def upload_by_no(self, upload_no: str) -> int | None:
        return self.db.scalar(select(DocumentUpload.id).where(DocumentUpload.upload_no == upload_no))

    def invalidate_current(self, document_id: int, status: str = "SUPERSEDED") -> None:
        self.db.execute(
            update(DocumentUpload)
            .where(DocumentUpload.task_document_id == document_id, DocumentUpload.is_current.is_(True))
            .values(is_current=False, status=status)
        )

    def invalidate_upload(self, upload_id: int, status: str = "REMOVED") -> None:
        self.db.execute(
            update(DocumentUpload)
            .where(DocumentUpload.id == upload_id)
            .values(is_current=False, status=status)
        )

    def current_upload(self, document_id: int) -> DocumentUpload | None:
        return self.db.scalar(
            select(DocumentUpload).where(
                DocumentUpload.task_document_id == document_id,
                DocumentUpload.is_current.is_(True),
            )
        )

    def documents_for_task(self, task_id: int) -> list[TaskDocument]:
        return list(self.db.scalars(
            select(TaskDocument)
            .where(TaskDocument.audit_task_id == task_id)
            .order_by(TaskDocument.id)
        ).all())

    def current_upload_map(self, task_id: int) -> dict[int, DocumentUpload]:
        return {
            item.task_document_id: item
            for item in self.db.scalars(
                select(DocumentUpload)
                .join(TaskDocument, TaskDocument.id == DocumentUpload.task_document_id)
                .where(TaskDocument.audit_task_id == task_id, DocumentUpload.is_current.is_(True))
            ).all()
        }

    def current_uploads(self, task_id: int) -> list[tuple[TaskDocument, DocumentUpload | None]]:
        uploads = self.current_upload_map(task_id)
        return [(document, uploads.get(document.id)) for document in self.documents_for_task(task_id)]
