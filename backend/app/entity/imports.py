from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CHAR,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from ..database import Base
from .base import TimestampMixin


class ImportJob(Base, TimestampMixin):
    __tablename__ = "import_job"
    __table_args__ = (
        CheckConstraint("source_size >= 0"),
        Index("idx_import_job_status_created_at", "status", "created_at"),
        Index("idx_import_job_created_at_id", "created_at", "id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    job_no: Mapped[str] = mapped_column(String(40), unique=True, nullable=False)
    source_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    source_path: Mapped[str] = mapped_column(String(500), nullable=False)
    source_sha256: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    source_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    profile_version: Mapped[str] = mapped_column(String(40), nullable=False)
    schema_key: Mapped[str] = mapped_column(String(80), nullable=False)
    schema_version: Mapped[str] = mapped_column(String(40), nullable=False)
    schema_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    scenario_code: Mapped[str | None] = mapped_column(String(80))
    profile_display_version: Mapped[str | None] = mapped_column(String(40))
    status: Mapped[str] = mapped_column(String(40), nullable=False)
    workbook_ir_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    proposals_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    manifest_draft_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    coverage_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    draft_revision: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    precheck_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    failure_code: Mapped[str | None] = mapped_column(String(80))
    failure_message: Mapped[str | None] = mapped_column(Text)


class ImportSheet(Base, TimestampMixin):
    __tablename__ = "import_sheet"
    __table_args__ = (
        UniqueConstraint("import_job_id", "sheet_key"),
        Index("idx_import_sheet_job_status", "import_job_id", "status"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    import_job_id: Mapped[int] = mapped_column(ForeignKey("import_job.id"), nullable=False)
    sheet_key: Mapped[str] = mapped_column(String(40), nullable=False)
    sheet_name: Mapped[str] = mapped_column(String(255), nullable=False)
    visible: Mapped[bool] = mapped_column(Boolean, nullable=False)
    status: Mapped[str] = mapped_column(String(40), nullable=False)
    snapshot_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)


class ImportIssue(Base, TimestampMixin):
    __tablename__ = "import_issue"
    __table_args__ = (
        UniqueConstraint("import_job_id", "issue_no"),
        Index("idx_import_issue_job_status", "import_job_id", "status"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    import_job_id: Mapped[int] = mapped_column(ForeignKey("import_job.id"), nullable=False)
    issue_no: Mapped[str] = mapped_column(String(40), nullable=False)
    category: Mapped[str] = mapped_column(String(40), nullable=False)
    severity: Mapped[str] = mapped_column(String(20), nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    source_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    options_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False)
    decision_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)


class FieldCandidate(Base, TimestampMixin):
    __tablename__ = "field_candidate"
    __table_args__ = (
        UniqueConstraint("import_job_id", "candidate_ref"),
        Index("idx_field_candidate_job_status", "import_job_id", "status"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    import_job_id: Mapped[int] = mapped_column(ForeignKey("import_job.id"), nullable=False)
    candidate_ref: Mapped[str] = mapped_column(String(120), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    value_type: Mapped[str] = mapped_column(String(30), nullable=False)
    material_ref: Mapped[str] = mapped_column(String(120), nullable=False)
    proposal_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False)

