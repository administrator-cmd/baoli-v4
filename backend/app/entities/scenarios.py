from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CHAR,
    CheckConstraint,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.mysql import DATETIME as MySQLDateTime
from sqlalchemy.orm import Mapped, mapped_column

from ..database import Base
from .base import TimestampMixin


class ScenarioTemplate(Base, TimestampMixin):
    """审核场景身份与版本计数器。"""

    __tablename__ = "scenario_template"
    __table_args__ = (
        UniqueConstraint("scenario_code"),
        CheckConstraint("latest_version_no >= 0"),
        Index("idx_scenario_template_deleted", "deleted_at"),
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
    deleted_at: Mapped[datetime | None] = mapped_column(MySQLDateTime(fsp=6))
    latest_version_no: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class ScenarioVersion(Base, TimestampMixin):
    """一次发布固化的场景版本；发布后不可覆盖。"""

    __tablename__ = "scenario_version"
    __table_args__ = (
        UniqueConstraint("scenario_code", "version_no"),
        UniqueConstraint("import_job_id"),
        CheckConstraint("version_no >= 1"),
        Index("idx_scenario_version_code_status", "scenario_code", "status", "published_at"),
        Index("idx_scenario_version_base", "base_version_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    import_job_id: Mapped[int | None] = mapped_column(ForeignKey("import_job.id"))
    scenario_code: Mapped[str] = mapped_column(String(80), nullable=False)
    scenario_name: Mapped[str] = mapped_column(String(255), nullable=False)
    schema_key: Mapped[str] = mapped_column(String(80), nullable=False)
    schema_version: Mapped[str] = mapped_column(String(40), nullable=False)
    schema_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    version_no: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False)
    source: Mapped[str] = mapped_column(String(20), nullable=False, default="IMPORT", server_default=text("'IMPORT'"))
    base_version_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("scenario_version.id"))
    change_summary: Mapped[str | None] = mapped_column(String(500))
    manifest_hash: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    manifest_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    published_at: Mapped[datetime] = mapped_column(MySQLDateTime(fsp=6), nullable=False)


class ScenarioMaterial(Base, TimestampMixin):
    __tablename__ = "scenario_material"
    __table_args__ = (UniqueConstraint("scenario_version_id", "material_key"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    scenario_version_id: Mapped[int] = mapped_column(ForeignKey("scenario_version.id"), nullable=False)
    material_key: Mapped[str] = mapped_column(String(40), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    required: Mapped[bool] = mapped_column(Boolean, nullable=False)
    source_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)


class ScenarioNode(Base, TimestampMixin):
    """场景版本里的一个节点实例。"""

    __tablename__ = "scenario_node"
    __table_args__ = (
        UniqueConstraint("scenario_version_id", "node_code"),
        UniqueConstraint("scenario_version_id", "sequence_no"),
        CheckConstraint("sequence_no >= 1"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    scenario_version_id: Mapped[int] = mapped_column(ForeignKey("scenario_version.id"), nullable=False)
    node_code: Mapped[str] = mapped_column(String(40), nullable=False)
    node_type: Mapped[str] = mapped_column(String(40), nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    sequence_no: Mapped[int] = mapped_column(Integer, nullable=False)
    depends_on: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    config_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False)


class ScenarioRule(Base, TimestampMixin):
    """场景版本里的一条规则投影。"""

    __tablename__ = "scenario_rule"
    __table_args__ = (
        UniqueConstraint("scenario_version_id", "rule_key"),
        ForeignKeyConstraint(
            ["scenario_version_id", "node_code"],
            ["scenario_node.scenario_version_id", "scenario_node.node_code"],
        ),
        Index("idx_scenario_rule_version_node_enabled", "scenario_version_id", "node_code", "enabled"),
        Index("idx_scenario_rule_version_code", "scenario_version_id", "rule_code"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    scenario_version_id: Mapped[int] = mapped_column(ForeignKey("scenario_version.id"), nullable=False)
    rule_key: Mapped[str] = mapped_column(String(40), nullable=False)
    rule_code: Mapped[str | None] = mapped_column(String(80))
    legacy_rule_key: Mapped[str | None] = mapped_column(String(40))
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    node_code: Mapped[str] = mapped_column(String(40), nullable=False)
    operator_code: Mapped[str] = mapped_column(String(40), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False)
    rule_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    source_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
