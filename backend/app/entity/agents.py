from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    CHAR,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.mysql import DATETIME as MySQLDateTime
from sqlalchemy.orm import Mapped, mapped_column

from ..database import Base
from .base import TimestampMixin


class AgentToolCall(Base, TimestampMixin):
    """Agent 工具调用账本：模型请求了什么、传了什么、得到什么。

    独立成表而不是塞进 checkpoint：checkpoint 只恢复流程状态，回答不了「模型调了哪些工具、
    依据哪一版事实、当时看到的是哪一版工具定义」，interrupt/resume 后也无法判断某个
    `tool_call_id` 是否已执行过，只能重跑。

    `tool_call_id` 唯一，这是「同一个调用不重复执行」的数据库级保证。恢复时按 status 区分：
    SUCCEEDED 读原结果、RUNNING 进待核查、FAILED 保留原错误。失败不允许用同一个
    `tool_call_id` 重试（那会让一条记录先后对应两次真实执行），必须生成新标识并把原标识
    写进 `retry_of_tool_call_id`。

    `evaluation_id` / `rule_instance_id` / `input_snapshot_hash` / `fact_snapshot_ref`
    非空：缺任一列都说明调用来源不合法或快照未冻结，必须显式失败。
    """

    __tablename__ = "agent_tool_call"
    __table_args__ = (
        UniqueConstraint("tool_call_id"),
        Index("idx_agent_tool_call_evaluation", "audit_task_id", "evaluation_id"),
        Index("idx_agent_tool_call_rule", "rule_instance_id"),
        Index("idx_agent_tool_call_retry", "retry_of_tool_call_id"),
        Index("idx_agent_tool_call_status", "status"),
        Index("idx_agent_tool_call_started", "started_at", "id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    audit_task_id: Mapped[int | None] = mapped_column(ForeignKey("audit_task.id"))
    evaluation_id: Mapped[str] = mapped_column(String(80), nullable=False)
    rule_instance_id: Mapped[str] = mapped_column(String(160), nullable=False)
    tool_call_id: Mapped[str] = mapped_column(String(120), nullable=False)
    # 明确重试时指向原调用的标识；为空表示这是一次全新调用，不是重试。
    retry_of_tool_call_id: Mapped[str | None] = mapped_column(String(120))
    tool_name: Mapped[str] = mapped_column(String(80), nullable=False)
    tool_version: Mapped[str | None] = mapped_column(String(20))
    # 工具目录版本与内容指纹：回答「模型当时看到的是哪一版工具定义」。
    # 指纹由目录内容算出，能发现「版本号没改但内容变了」。
    tool_catalog_version: Mapped[str] = mapped_column(String(20), nullable=False)
    tool_catalog_hash: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    tool_policy_version: Mapped[str | None] = mapped_column(String(20))
    # 模型给出的参数原文：只含业务口径与身份引用，运行期依赖不在这里。
    arguments_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    # 本次调用依据的输入事实快照身份。恢复时与原记录不同即协议冲突，不按新参数重算。
    input_snapshot_hash: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    fact_snapshot_ref: Mapped[str] = mapped_column(String(120), nullable=False)
    caller_identity: Mapped[str | None] = mapped_column(String(80))
    # PENDING / RUNNING / SUCCEEDED / FAILED。RUNNING 表示可能已发出但结果未知。
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    result_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    error_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    # 外部作业号（若该工具调用过外部服务）。有它才能查询原作业而不是重发。
    external_job_id: Mapped[str | None] = mapped_column(String(120))
    started_at: Mapped[datetime] = mapped_column(MySQLDateTime(fsp=6), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(MySQLDateTime(fsp=6))


class EvaluationSnapshot(Base, TimestampMixin):
    """规则评估所依据的冻结事实内容（不是只有指纹）。

    只存哈希意味着工具执行时仍去读「当前数据」，同一批次的两次工具调用可能落到不同的材料
    版本上，而指纹对得上只是巧合。本表把事实内容本身落库，工具执行只从它解析事实；工具
    确实需要读文件或产物时，必须核对其引用、hash 与修订号与本表一致。

    冻结内容不可改写（`trg_evaluation_snapshot_immutable`）：改写它等于让已经据此执行的
    工具结果无从解释。
    """

    __tablename__ = "evaluation_snapshot"
    __table_args__ = (
        UniqueConstraint("snapshot_ref"),
        UniqueConstraint("evaluation_id", "rule_instance_id", "input_revision"),
        Index("idx_evaluation_snapshot_task", "audit_task_id", "node_code"),
        Index("idx_evaluation_snapshot_hash", "fact_snapshot_hash"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    # 可读引用（形如 facts:<条数>:<短哈希>），同时是恢复入口。
    snapshot_ref: Mapped[str] = mapped_column(String(120), nullable=False)
    audit_task_id: Mapped[int] = mapped_column(ForeignKey("audit_task.id"), nullable=False)
    evaluation_id: Mapped[str] = mapped_column(String(80), nullable=False)
    rule_instance_id: Mapped[str] = mapped_column(String(160), nullable=False)
    node_code: Mapped[str] = mapped_column(String(80), nullable=False)
    # 冻结时的任务输入修订号：与任务当前修订不一致即说明输入已变。
    input_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    facts_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    fact_snapshot_hash: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    tool_catalog_version: Mapped[str] = mapped_column(String(20), nullable=False)
    tool_catalog_hash: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    tool_policy_version: Mapped[str] = mapped_column(String(20), nullable=False)
    allowed_tools_json: Mapped[list[Any]] = mapped_column(JSON, nullable=False)

