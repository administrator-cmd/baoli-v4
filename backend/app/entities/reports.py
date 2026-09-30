from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.mysql import DATETIME as MySQLDateTime
from sqlalchemy.dialects.mysql import MEDIUMTEXT
from sqlalchemy.orm import Mapped, mapped_column

from ..database import Base
from .base import TimestampMixin


class AuditReport(Base, TimestampMixin):
    """报告输出节点生成的一份报告快照。

    报告由真实节点生成，不提供空白报告手填结论；重新生成是新快照，历史快照保留。
    名称与备注可编辑，正文、三信号与快照范围不可改写。
    origin=NODE 表示由报告节点写入；LEGACY_BACKFILL 表示增量升级时从历史
    节点结果按原值回填（内容与信号均来自当时的真实结果，未做任何改写）。
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
    # 产生这份报告的报告输出节点执行；历史回填同样指向当时的执行记录。
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
    # 报告对账用的输入修订；历史回填无法确定时为空，页面显示“修订未知”。
    input_revision: Mapped[int | None] = mapped_column(Integer)
    origin: Mapped[str] = mapped_column(String(20), nullable=False, default="NODE", server_default=text("'NODE'"))
    remark: Mapped[str | None] = mapped_column(Text)
    archived_at: Mapped[datetime | None] = mapped_column(MySQLDateTime(fsp=6))
    generated_at: Mapped[datetime] = mapped_column(MySQLDateTime(fsp=6), nullable=False)

