from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    ForeignKey,
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


class ScenarioTemplate(Base, TimestampMixin):
    """审核场景身份与版本计数器。"""

    __tablename__ = "scenario_template"
    __table_args__ = (
        UniqueConstraint("scenario_code"),
        CheckConstraint("latest_version_no >= 0"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    scenario_code: Mapped[str] = mapped_column(String(80), nullable=False)
    scenario_name: Mapped[str] = mapped_column(String(255), nullable=False)
    schema_key: Mapped[str] = mapped_column(String(80), nullable=False)
    schema_version: Mapped[str] = mapped_column(String(40), nullable=False)
    schema_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    category: Mapped[str | None] = mapped_column(String(80))
    description: Mapped[str | None] = mapped_column(Text)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default=text("1"))
    latest_version_no: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class ScenarioVersion(Base, TimestampMixin):
    """一次发布固化的场景版本；发布后不可覆盖。"""

    __tablename__ = "scenario_version"
    __table_args__ = (
        UniqueConstraint("scenario_code", "version_no"),
        UniqueConstraint("import_job_id"),
        CheckConstraint("version_no >= 1"),
        Index("idx_scenario_version_code_published", "scenario_code", "published_at"),
        Index("idx_scenario_version_base", "base_version_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    import_job_id: Mapped[int | None] = mapped_column(ForeignKey("import_job.id", name="fk_scenario_version_import_job", ondelete="RESTRICT", onupdate="RESTRICT"))
    # 历史快照：场景删除后版本仍保留，因此不建指向 scenario_template 的外键。
    scenario_code: Mapped[str] = mapped_column(String(80), nullable=False)
    scenario_name: Mapped[str] = mapped_column(String(255), nullable=False)
    schema_key: Mapped[str] = mapped_column(String(80), nullable=False)
    schema_version: Mapped[str] = mapped_column(String(40), nullable=False)
    schema_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    version_no: Mapped[int] = mapped_column(Integer, nullable=False)
    source: Mapped[str] = mapped_column(String(20), nullable=False, default="IMPORT", server_default=text("'IMPORT'"))
    base_version_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("scenario_version.id", name="fk_scenario_version_base", ondelete="RESTRICT", onupdate="RESTRICT"))
    change_summary: Mapped[str | None] = mapped_column(String(500))
    manifest_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    published_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)


class ScenarioNode(Base, TimestampMixin):
    """场景版本里的一个节点实例。"""

    __tablename__ = "scenario_node"
    __table_args__ = (
        UniqueConstraint("scenario_version_id", "node_code"),
        UniqueConstraint("scenario_version_id", "sequence_no"),
        CheckConstraint("sequence_no >= 1"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    scenario_version_id: Mapped[int] = mapped_column(ForeignKey("scenario_version.id", name="fk_scenario_node_version", ondelete="RESTRICT", onupdate="RESTRICT"), nullable=False)
    node_code: Mapped[str] = mapped_column(String(80), nullable=False)
    node_type: Mapped[str] = mapped_column(String(40), nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    sequence_no: Mapped[int] = mapped_column(Integer, nullable=False)
    depends_on: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    config_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False)
