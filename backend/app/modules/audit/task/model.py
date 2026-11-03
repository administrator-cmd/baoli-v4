"""模型定义，由 entity/tasks.py 按实体拆分。"""

from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
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

from app.core.base_model import TimestampMixin
from app.core.database import Base


class AuditTask(Base, TimestampMixin):
    __tablename__ = "audit_task"
    __table_args__ = (
        UniqueConstraint("id", "scenario_version_id"),
        ForeignKeyConstraint(
            ["scenario_version_id", "current_node"],
            ["scenario_node.scenario_version_id", "scenario_node.node_code"],
            name="fk_audit_task_current_node",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        Index("idx_audit_task_version_status", "scenario_version_id", "status"),
        Index("idx_audit_task_plan", "plan_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    task_no: Mapped[str] = mapped_column(String(40), unique=True, nullable=False)
    scenario_version_id: Mapped[int] = mapped_column(ForeignKey("scenario_version.id", name="fk_audit_task_scenario_version", ondelete="RESTRICT", onupdate="RESTRICT"), nullable=False)
    # 任务创建时使用的审核方案。
    plan_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("audit_plan.id", name="fk_audit_task_plan", ondelete="RESTRICT", onupdate="RESTRICT"))
    task_name: Mapped[str] = mapped_column(String(255), nullable=False)
    # 任务备注：属于可编辑的基本资料，不进入执行快照。
    remark: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(40), nullable=False)
    current_node: Mapped[str] = mapped_column(String(80), nullable=False)
    # 本任务的执行快照：方案的编译产物整份冻结（材料/字段/规则/报告章节/节点）。
    # 执行期只读它，不读场景版本、也不读方案当前配置——方案后来怎么改都不影响已建任务。
    manifest_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    # 本任务的规则范围（启用/未启用规则与字段清单）。从上面的 manifest 算出。
    task_schema_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    # 业务 CSV 清单：format_version、batches（只登记 current/files/record_count）。
    # 由 services.audit.value_store 读写。
    value_file_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    # 任务输入修订号：首次补传、替换材料和人工有效值变化都会递增。
    # 写入口在对象锁内核对命令携带的修订号，过期命令不得覆盖新输入。
    input_revision: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default=text("0"))
