from datetime import datetime
from typing import Any

from sqlalchemy import CHAR, JSON, BigInteger, Boolean, Index, Integer, String, Text, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.base_model import TimestampMixin, UTCDateTime
from app.core.database import Base


class BackgroundOperation(Base, TimestampMixin):
    """后台长操作的持久记录：先认领，再由后台用独立 Session 执行。

    请求只返回受理状态；真正的结果写回 result_json / failure_*。
    活跃操作（PENDING/RUNNING）在对象上互斥，重复的幂等标识返回同一条记录。
    """

    __tablename__ = "background_operation"
    __table_args__ = (
        UniqueConstraint("operation_no"),
        # 幂等标识与请求内容绑定：同一对象同一操作类型下，标识不同才是不同的操作。
        UniqueConstraint("object_type", "object_no", "operation_type", "idempotency_key"),
        Index("idx_background_operation_object", "object_type", "object_no", "status"),
        Index("idx_background_operation_status", "status", "created_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    operation_no: Mapped[str] = mapped_column(String(40), nullable=False)
    object_type: Mapped[str] = mapped_column(String(30), nullable=False)
    object_no: Mapped[str] = mapped_column(String(40), nullable=False)
    operation_type: Mapped[str] = mapped_column(String(40), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(120), nullable=False)
    target_node: Mapped[str | None] = mapped_column(String(40))
    input_version: Mapped[str | None] = mapped_column(String(120))
    # 规范化请求摘要：同一幂等标识必须比较它才能判断是重发还是新请求。
    request_hash: Mapped[str | None] = mapped_column(CHAR(64))
    # 命令当前停在哪一步（COMMAND_ACCEPTED / EXTERNAL_SUBMITTED / RESULT_UNKNOWN /
    # BUSINESS_COMMITTED / GRAPH_ADVANCED / INTERRUPT_WAITING / RESULT_READY）。恢复时按它决定唯一推进动作。
    phase: Mapped[str | None] = mapped_column(String(40))
    # 外部作业引用（例如 OCR jobId）：本地超时后只能续查同一个作业，不能重新提交。
    external_job_ref: Mapped[str | None] = mapped_column(String(120))
    status: Mapped[str] = mapped_column(String(30), nullable=False)
    request_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    result_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    # 相对于 data 根目录；原始模型返回独立保存，业务校验失败仍保留索引。
    raw_response_path: Mapped[str | None] = mapped_column(String(500))
    failure_code: Mapped[str | None] = mapped_column(String(80))
    failure_message: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class OperationLog(Base, TimestampMixin):
    """一次被受理并成功完成的业务写操作的持久记录。

    只记录真实发生并已提交的操作（创建、编辑、启停、删除恢复、发布、复制、
    上传/替换/移除、复核提交、报告备注与归档等）；不记录失败的尝试，
    也不复制业务执行记录——节点执行与后台命令分别在 node_execution 与
    background_operation 中，按 command_no 关联查询。
    """

    __tablename__ = "operation_log"
    __table_args__ = (
        UniqueConstraint("log_no"),
        # 同步命令的幂等标识：同一对象同一操作类型下，重复提交必须复用第一次的
        # 结果（响应丢失后的重发不能重复创建）。没有幂等标识的操作记录不受影响
        # （MySQL 唯一键允许多个 NULL）。
        UniqueConstraint("object_type", "operation_type", "idempotency_key"),
        Index("idx_operation_log_object", "object_type", "object_no", "created_at"),
        Index("idx_operation_log_created", "created_at"),
        Index("idx_operation_log_delete", "object_type", "object_no", "operation_type", "permanent"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    log_no: Mapped[str] = mapped_column(String(40), nullable=False)
    object_type: Mapped[str] = mapped_column(String(30), nullable=False)
    object_no: Mapped[str] = mapped_column(String(40), nullable=False)
    object_name: Mapped[str] = mapped_column(String(255), nullable=False)
    operation_type: Mapped[str] = mapped_column(String(40), nullable=False)
    result: Mapped[str] = mapped_column(String(20), nullable=False, default="SUCCEEDED", server_default=text("'SUCCEEDED'"))
    idempotency_key: Mapped[str | None] = mapped_column(String(120))
    request_hash: Mapped[str | None] = mapped_column(CHAR(64))
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    # 关联的后台命令编号（若该操作受理了后台执行）。
    command_no: Mapped[str | None] = mapped_column(String(40))
    # 是否为永久删除；用于删除命令的幂等识别。
    permanent: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=text("0"))
    detail_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    failure_code: Mapped[str | None] = mapped_column(String(80))
    failure_message: Mapped[str | None] = mapped_column(Text)

