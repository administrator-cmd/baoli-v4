"""可复用报告风格模板。"""

from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    CheckConstraint,
    Index,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.base_model import TimestampMixin
from app.core.database import Base


class ReportTemplate(Base, TimestampMixin):
    __tablename__ = "report_template"
    __table_args__ = (
        UniqueConstraint("template_no", name="uq_report_template_no"),
        UniqueConstraint("upload_idempotency_key", name="uq_report_template_upload_idempotency_key"),
        Index("idx_report_template_status_updated", "status", "updated_at"),
        CheckConstraint("status IN ('PROCESSING','READY','FAILED')", name="ck_report_template_status"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    template_no: Mapped[str] = mapped_column(String(40), nullable=False)
    upload_idempotency_key: Mapped[str | None] = mapped_column(String(120))
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    source_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    source_path: Mapped[str] = mapped_column(String(500), nullable=False)
    source_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    source_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    page_count: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default=text("0"))
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="PROCESSING", server_default=text("'PROCESSING'"))
    style_profile_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    analysis_error: Mapped[str | None] = mapped_column(Text)
