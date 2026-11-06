"""AI 辅助生成规则库的作业、冻结导出与导入关联实体。"""

from datetime import datetime
from typing import Any

from sqlalchemy import (
    CHAR,
    JSON,
    BigInteger,
    CheckConstraint,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.base_model import TimestampMixin, UTCDateTime
from app.core.database import Base


class RulebookGenerationJob(Base, TimestampMixin):
    """一次生成作业与当前可编辑文档。

    原文件、用户输入、契约与提示词版本在生成开始后不可改；document_json 是唯一可写
    的当前内容，导出快照另存于导出实体。作业状态不落本表，列表按最近后台操作和是否
    已有文档推导。
    """

    __tablename__ = "rulebook_generation_job"
    __table_args__ = (
        UniqueConstraint("job_no", name="uq_rulebook_generation_job_no"),
        UniqueConstraint("idempotency_key", name="uq_rulebook_generation_job_idempotency"),
        CheckConstraint("source_size >= 0", name="ck_rulebook_generation_source_size"),
        Index("idx_rulebook_generation_created_at_id", "created_at", "id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    job_no: Mapped[str] = mapped_column(String(40), nullable=False)
    # 上传创建幂等键：同键同内容返回原记录，同键不同内容明确冲突。
    idempotency_key: Mapped[str] = mapped_column(String(120), nullable=False)
    request_hash: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    source_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    source_path: Mapped[str] = mapped_column(String(500), nullable=False)
    source_sha256: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    source_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    business_background: Mapped[str] = mapped_column(Text, nullable=False, default="")
    generation_requirements: Mapped[str] = mapped_column(Text, nullable=False, default="")
    document_schema_version: Mapped[str] = mapped_column(String(40), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(40), nullable=False)
    document_json: Mapped[dict[str, Any] | None] = mapped_column(JSON(none_as_null=True))
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default=text("0"))


class RulebookGenerationRevision(Base):
    """只追加的完整文档快照；作业表的当前文档是唯一可编辑入口。"""

    __tablename__ = "rulebook_generation_revision"
    __table_args__ = (
        UniqueConstraint("generation_job_id", "revision", name="uq_generation_revision_job"),
        UniqueConstraint("operation_id", name="uq_generation_revision_operation"),
        CheckConstraint("revision >= 1", name="ck_generation_revision_positive"),
        CheckConstraint("origin IN ('INITIAL','MANUAL','AI','RESTORE','BASELINE')", name="ck_generation_revision_origin"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    generation_job_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("rulebook_generation_job.id", ondelete="RESTRICT", name="fk_generation_revision_job"),
        nullable=False,
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    document_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    origin: Mapped[str] = mapped_column(String(16), nullable=False)
    operation_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("background_operation.id", ondelete="RESTRICT", name="fk_generation_revision_operation"),
    )
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), server_default=text("CURRENT_TIMESTAMP(6)"), nullable=False,
    )


class RulebookGenerationMessage(Base):
    """用户要求与助手简短回复；执行状态从关联后台操作读取，不重复存储。"""

    __tablename__ = "rulebook_generation_message"
    __table_args__ = (
        UniqueConstraint("operation_id", "role", name="uq_generation_message_operation_role"),
        ForeignKeyConstraint(
            ["generation_job_id", "revision"],
            ["rulebook_generation_revision.generation_job_id", "rulebook_generation_revision.revision"],
            name="fk_generation_message_revision", ondelete="RESTRICT",
        ),
        CheckConstraint("role IN ('USER','ASSISTANT')", name="ck_generation_message_role"),
        Index("idx_generation_message_job_id", "generation_job_id", "id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    generation_job_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    operation_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("background_operation.id", ondelete="RESTRICT", name="fk_generation_message_operation"),
        nullable=False,
    )
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), server_default=text("CURRENT_TIMESTAMP(6)"), nullable=False,
    )


class RulebookGenerationExport(Base):
    """指定修订的冻结导出产物；快照不可变，当前文档另存于作业表。"""

    __tablename__ = "rulebook_generation_export"
    __table_args__ = (
        UniqueConstraint("export_no", name="uq_rulebook_generation_export_no"),
        UniqueConstraint("generation_job_id", "revision", name="uq_rulebook_generation_export_revision"),
        CheckConstraint("size >= 0", name="ck_rulebook_generation_export_size"),
        Index("idx_rulebook_generation_export_job", "generation_job_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    export_no: Mapped[str] = mapped_column(String(40), nullable=False)
    generation_job_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey(
            "rulebook_generation_job.id",
            name="fk_rulebook_generation_export_job",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        nullable=False,
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    document_snapshot_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    export_format_version: Mapped[str] = mapped_column(String(40), nullable=False)
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    relative_path: Mapped[str] = mapped_column(String(500), nullable=False)
    sha256: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), server_default=text("CURRENT_TIMESTAMP(6)"), nullable=False,
    )


class RulebookGenerationImport(Base):
    """冻结导出与真实导入作业的关联及导入请求幂等。"""

    __tablename__ = "rulebook_generation_import"
    __table_args__ = (
        UniqueConstraint(
            "generation_export_id", "idempotency_key",
            name="uq_rulebook_generation_import_request",
        ),
        # 单个导入作业至多一条生成来源关系。
        UniqueConstraint("import_job_id", name="uq_rulebook_generation_import_job"),
        Index("idx_rulebook_generation_import_export", "generation_export_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    generation_export_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey(
            "rulebook_generation_export.id",
            name="fk_rulebook_generation_import_export",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        nullable=False,
    )
    # 导入作业被用户删除后只解除关联，生成原件与导出记录继续保留。
    import_job_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey(
            "import_job.id",
            name="fk_rulebook_generation_import_job",
            ondelete="SET NULL",
            onupdate="RESTRICT",
        ),
        nullable=True,
    )
    idempotency_key: Mapped[str] = mapped_column(String(120), nullable=False)
    request_hash: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), server_default=text("CURRENT_TIMESTAMP(6)"), nullable=False,
    )
