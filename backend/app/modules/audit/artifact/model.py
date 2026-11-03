"""模型定义，由 entity/tasks.py 按实体拆分。"""

from datetime import datetime
from typing import Any

from sqlalchemy import (
    CHAR,
    JSON,
    BigInteger,
    ForeignKeyConstraint,
    Index,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.base_model import TimestampMixin, UTCDateTime
from app.core.database import Base


class StoredArtifact(Base, TimestampMixin):
    __tablename__ = "stored_artifact"
    __table_args__ = (
        UniqueConstraint("document_upload_id", "artifact_type", name="uq_stored_artifact_upload_type"),
        Index("idx_stored_artifact_content", "document_upload_id", "artifact_type", "content_sha256"),
        UniqueConstraint("id", "audit_task_id"),
        ForeignKeyConstraint(
            ["document_upload_id", "audit_task_id"],
            ["document_upload.id", "document_upload.audit_task_id"],
            name="fk_stored_artifact_upload_task",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        Index("idx_stored_artifact_upload_status", "document_upload_id", "status"),
        Index("idx_stored_artifact_status", "status", "updated_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    artifact_no: Mapped[str] = mapped_column(String(40), unique=True, nullable=False)
    document_upload_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    audit_task_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    artifact_type: Mapped[str] = mapped_column(String(40), nullable=False)
    relative_path: Mapped[str] = mapped_column(String(500), nullable=False)
    # 产物内容版本：产物实际内容的哈希（JSON 规范序列化或二进制字节）。
    # 同一份上传重新识别后内容可能不同，内容哈希用于判断产物是否更新。
    content_sha256: Mapped[str | None] = mapped_column(CHAR(64))
    status: Mapped[str] = mapped_column(String(30), nullable=False)
    # 外部作业号与截止时间；结果未知时保留最后一次已受理的作业。
    external_job_id: Mapped[str | None] = mapped_column(String(120))
    deadline_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    failure_code: Mapped[str | None] = mapped_column(String(80))
    failure_message: Mapped[str | None] = mapped_column(Text)
    # 只保存 processing_attempts / submit_attempts 等调用摘要；产物正文在 relative_path。
    metadata_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
