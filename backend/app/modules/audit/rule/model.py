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


class RuleResult(Base, TimestampMixin):
    """一次规则执行的身份、状态与诊断；业务结论存在本次任务的 CSV。

    普通规则的 `rule_key` 是配置中的稳定键，计算结果的 `rule_key` 是计算业务编号。
    两者可能重名，结果须结合所属节点执行定位；数据库唯一键为
    (`node_execution_id`, `rule_key`)，不能按裸 `rule_key` 跨节点合并。

    `status`、`message`、`risk_level`、`rule_role` 作为列表与风险筛选的索引列；
    CSV 只保存处置、建议、依据与展示协议，不重复保存这几项。
    """

    __tablename__ = "rule_result"
    __table_args__ = (
        UniqueConstraint("node_execution_id", "rule_key"),
        UniqueConstraint("id", "audit_task_id"),
        ForeignKeyConstraint(
            ["node_execution_id", "audit_task_id"],
            ["node_execution.id", "node_execution.audit_task_id"],
            name="fk_rule_result_execution_task",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        Index("idx_rule_result_task_status", "audit_task_id", "status"),
        Index("idx_rule_result_task_rule", "audit_task_id", "rule_key"),
        Index("idx_rule_result_task_code", "audit_task_id", "rule_code"),
        Index("idx_rule_result_task_risk_level", "audit_task_id", "risk_level"),
        # 风险结果列表跨任务筛选角色，规则角色必须做前导列。
        Index("idx_rule_result_role_task", "rule_role", "audit_task_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    audit_task_id: Mapped[int] = mapped_column(ForeignKey("audit_task.id", name="fk_rule_result_task", ondelete="RESTRICT", onupdate="RESTRICT"), nullable=False)
    node_execution_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    rule_key: Mapped[str] = mapped_column(String(80), nullable=False)
    rule_code: Mapped[str | None] = mapped_column(String(80))
    rule_name: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(40), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    # 风险等级：风险结果列表按它筛选与排序，因此单独成列而不是压进 JSON。
    risk_level: Mapped[str | None] = mapped_column(String(40))
    # 规则角色 RISK / CHECK：取自任务清单里该条规则的同一判定。
    # 风险结果列表按它过滤，不必为每一行解析整份 manifest_json。
    rule_role: Mapped[str] = mapped_column(String(20), nullable=False)
    # 单条规则的模型循环结局与轮次。
    loop_status: Mapped[str | None] = mapped_column(String(30))
    turn_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default=text("0"))
    # 模型原始响应文件，相对 data 根目录。
    # 小体积执行诊断：审核标准、输入引用、未解决事项；不含模型原文与业务取值。
    result_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
