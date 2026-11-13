"""产物记录的 SQL 层。"""

from sqlalchemy import func, select, update

from app.core.base_crud import CRUDBase
from app.modules.audit.artifact.model import StoredArtifact


class AuditArtifactCRUD(CRUDBase):
    model = StoredArtifact

    def count_active_by_upload(self, upload_id: int) -> int:
        statement = (
            select(func.count())
            .select_from(StoredArtifact)
            .where(
                StoredArtifact.document_upload_id == upload_id,
                StoredArtifact.status.in_(["READY", "PENDING", "SUBMITTING"]),
            )
        )
        return int(self.db.scalar(statement) or 0)

    def invalidate_by_upload(self, upload_id: int) -> None:
        self.db.execute(
            update(StoredArtifact)
            .where(
                StoredArtifact.document_upload_id == upload_id,
                StoredArtifact.status.in_(["READY", "PENDING", "SUBMITTING"]),
            )
            .values(status="INVALIDATED")
        )

    def rows_by_uploads(self, upload_ids: list[int], artifact_types: tuple[str, ...]):
        return list(self.db.execute(
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
                StoredArtifact.artifact_type.in_(artifact_types),
            )
            .order_by(StoredArtifact.id)
        ).all())

    def by_upload_and_type(self, upload_id: int, artifact_type: str) -> StoredArtifact | None:
        return self.db.scalar(
            select(StoredArtifact).where(
                StoredArtifact.document_upload_id == upload_id,
                StoredArtifact.artifact_type == artifact_type,
            )
        )

    def by_uploads_and_type(self, upload_ids: list[int], artifact_type: str) -> list[StoredArtifact]:
        if not upload_ids:
            return []
        return list(self.db.scalars(
            select(StoredArtifact).where(
                StoredArtifact.document_upload_id.in_(upload_ids),
                StoredArtifact.artifact_type == artifact_type,
            )
        ).all())

    def latest_id_by_upload_type(self, upload_id: int, artifact_type: str) -> int | None:
        return self.db.scalar(
            select(func.max(StoredArtifact.id))
            .where(
                StoredArtifact.document_upload_id == upload_id,
                StoredArtifact.artifact_type == artifact_type,
            )
        )

    def by_id(self, artifact_id: int) -> StoredArtifact | None:
        return self.db.get(StoredArtifact, artifact_id)

    def submitting_ocr(self) -> list[StoredArtifact]:
        return list(self.db.scalars(
            select(StoredArtifact).where(
                StoredArtifact.artifact_type == "OCR",
                StoredArtifact.status == "SUBMITTING",
            )
        ).all())
