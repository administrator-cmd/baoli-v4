from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.mysql import MEDIUMTEXT
from sqlalchemy.orm import Mapped, mapped_column

from ..core.database import Base
from .base import TimestampMixin, UTCDateTime


class AuditReport(Base, TimestampMixin):
    """生效报告规则真实生成的一份报告快照。

    重新生成时创建新快照，已有快照保留。名称与备注可编辑，
    正文、完成信号与快照范围不可改写。
    """

    __tablename__ = "audit_report"
    __table_args__ = (
        UniqueConstraint("report_no"),
        Index("idx_audit_report_task_generated", "audit_task_id", "generated_at"),
        Index("idx_audit_report_generated", "generated_at"),
        Index("idx_audit_report_status", "status"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    report_no: Mapped[str] = mapped_column(String(40), nullable=False)
    audit_task_id: Mapped[int] = mapped_column(ForeignKey("audit_task.id"), nullable=False)
    # 生成报告时所属的规则执行批次。
    node_execution_id: Mapped[int | None] = mapped_column(BigInteger)
    report_name: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="GENERATED", server_default=text("'GENERATED'"))
    content_type: Mapped[str] = mapped_column(String(60), nullable=False, default="text/markdown", server_default=text("'text/markdown'"))
    content_md: Mapped[str | None] = mapped_column(MEDIUMTEXT)
    report_generated: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=text("0"))
    scope_execution_complete: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=text("0"))
    risk_conclusion_available: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=text("0"))
    # 生成时刻的不可判定原因集合与真实范围快照（输入修订、上传/事实/复核/结果 ID）。
    reasons_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    snapshot_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    # 生成报告时的任务输入修订，用于对账。
    input_revision: Mapped[int | None] = mapped_column(Integer)
    remark: Mapped[str | None] = mapped_column(Text)
    archived_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    generated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)

