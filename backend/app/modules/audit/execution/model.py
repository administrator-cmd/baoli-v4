"""模型定义，由 entity/tasks.py 按实体拆分。"""

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    CheckConstraint,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.base_model import TimestampMixin, UTCDateTime
from app.core.database import Base


class NodeExecution(Base, TimestampMixin):
    __tablename__ = "node_execution"
    __table_args__ = (
        UniqueConstraint("audit_task_id", "sequence_no"),
        UniqueConstraint("id", "audit_task_id"),
        ForeignKeyConstraint(
            ["audit_task_id", "scenario_version_id"],
            ["audit_task.id", "audit_task.scenario_version_id"],
            name="fk_node_execution_task_version",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["scenario_version_id", "node_code"],
            ["scenario_node.scenario_version_id", "scenario_node.node_code"],
            name="fk_node_execution_version_node",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        CheckConstraint("sequence_no >= 1"),
        Index("idx_node_execution_task_node", "audit_task_id", "node_code"),
        Index("idx_node_execution_task_status", "audit_task_id", "status"),
        Index("idx_node_execution_display_status", "status", "invalidated_from"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    audit_task_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    scenario_version_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    node_code: Mapped[str] = mapped_column(String(80), nullable=False)
    sequence_no: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    result_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    failure_code: Mapped[str | None] = mapped_column(String(80))
    failure_message: Mapped[str | None] = mapped_column(Text)
    # 被重试或换版取代时保留原状态与原因。
    invalidated_from: Mapped[str | None] = mapped_column(String(30))
    invalidated_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    invalidation_reason: Mapped[str | None] = mapped_column(String(200))
