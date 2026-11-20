"""模型定义，由 entity/tasks.py 按实体拆分。"""


from pathlib import PurePosixPath

from sqlalchemy import (
    CHAR,
    BigInteger,
    Boolean,
    CheckConstraint,
    Computed,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.base_model import TimestampMixin
from app.core.database import Base


class TaskDocument(Base, TimestampMixin):
    __tablename__ = "task_document"
    __table_args__ = (
        UniqueConstraint("audit_task_id", "material_key"),
        UniqueConstraint("id", "audit_task_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    document_no: Mapped[str] = mapped_column(String(40), unique=True, nullable=False)
    audit_task_id: Mapped[int] = mapped_column(ForeignKey("audit_task.id", name="fk_task_document_task", ondelete="RESTRICT", onupdate="RESTRICT"), nullable=False)
    material_key: Mapped[str] = mapped_column(String(40), nullable=False)
    material_name: Mapped[str] = mapped_column(String(255), nullable=False)
    required: Mapped[bool] = mapped_column(Boolean, nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False)


class DocumentUpload(Base, TimestampMixin):
    __tablename__ = "document_upload"

    @property
    def artifact_directory(self) -> str:
        return (PurePosixPath(self.relative_path).parent / "artifacts").as_posix()

    __table_args__ = (
        UniqueConstraint("task_document_id", "upload_no"),
        UniqueConstraint("id", "audit_task_id"),
        UniqueConstraint("id", "task_document_id"),
        UniqueConstraint("current_task_document_id"),
        ForeignKeyConstraint(
            ["task_document_id", "audit_task_id"],
            ["task_document.id", "task_document.audit_task_id"],
            name="fk_document_upload_task_document_task",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        CheckConstraint("size_bytes >= 0"),
        CheckConstraint("is_current IN (0, 1)"),
        Index("idx_document_upload_document_current", "task_document_id", "is_current"),
        Index("idx_document_upload_sha256", "sha256"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    upload_no: Mapped[str] = mapped_column(String(40), nullable=False)
    task_document_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    audit_task_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    original_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    relative_path: Mapped[str] = mapped_column(String(500), nullable=False)
    content_type: Mapped[str] = mapped_column(String(120), nullable=False)
    # 上传时由用户从 8018 支持目录中选择的材料类型，只在本次上传内有效：
    # 替换文件时可以改成另一个类型，历史上传版本各自保留当时声明的值。
    # 它是提交给 8018 /validate 的 doc_type（见 services/audit/quality._document_prepare）。
    declared_material_type: Mapped[str] = mapped_column(String(80), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sha256: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False)
    is_current: Mapped[bool] = mapped_column(Boolean, nullable=False)
    current_task_document_id: Mapped[int | None] = mapped_column(
        BigInteger,
        Computed("IF(is_current = 1, task_document_id, NULL)", persisted=True),
        nullable=True,
    )
