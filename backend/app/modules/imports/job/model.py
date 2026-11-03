from typing import Any

from sqlalchemy import (
    CHAR,
    JSON,
    BigInteger,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.base_model import TimestampMixin
from app.core.database import Base


class ImportJob(Base, TimestampMixin):
    __tablename__ = "import_job"
    __table_args__ = (
        CheckConstraint("source_size >= 0"),
        Index("idx_import_job_status_created_at", "status", "created_at"),
        Index("idx_import_job_created_at_id", "created_at", "id"),
        Index("idx_import_job_scenario", "scenario_code"),
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
    scenario_code: Mapped[str | None] = mapped_column(
        String(80), ForeignKey("scenario_template.scenario_code", name="fk_import_job_template", ondelete="RESTRICT", onupdate="RESTRICT")
    )
    status: Mapped[str] = mapped_column(String(40), nullable=False)
    proposals_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    draft_revision: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    failure_code: Mapped[str | None] = mapped_column(String(80))
    failure_message: Mapped[str | None] = mapped_column(Text)
