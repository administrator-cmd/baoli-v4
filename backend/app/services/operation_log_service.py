"""业务操作日志：记录真实发生的写操作，并与后台命令对账。

只记录已成功提交的同步写操作（CRUD、启停、删除恢复、发布、复制、上传、替换、移除、复核提交、
报告备注归档）；失败的尝试不写日志，避免把「尝试」显示成「已发生」。

后台长命令（节点执行、复核恢复、导入转换）的记录在 background_operation，这里不复制它的内容，
只在操作日志查询时按 command_no / 编号并列展示。同一对象同一操作类型的幂等标识唯一：
响应丢失后的重发先查历史记录，输入一致返回原结果，不一致明确冲突，不重复创建。
"""

import hashlib
import json
import logging
from datetime import datetime
from typing import Any

from sqlalchemy import and_, case, func, literal, or_, select, text, union_all
from sqlalchemy.orm import Session

from ..common.exceptions import BusinessError
from ..entity import ImportJob
from ..entity import AuditTask, BackgroundOperation, OperationLog
from ..domain.identity import public_no

logger = logging.getLogger(__name__)



def normalized_request_hash(payload: dict[str, Any]) -> str:
    """请求内容的稳定哈希：幂等比对用，同一内容必得同一值。"""
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def find_by_idempotency(
    db: Session,
    *,
    object_type: str,
    operation_type: str,
    idempotency_key: str,
) -> OperationLog | None:
    """按对象、操作类型与幂等标识查找原操作记录；没有则返回 None。"""
    return db.scalar(
        select(OperationLog).where(
            OperationLog.object_type == object_type,
            OperationLog.operation_type == operation_type,
            OperationLog.idempotency_key == idempotency_key,
        )
    )


def require_idempotent_replay(
    db: Session,
    *,
    object_type: str,
    operation_type: str,
    idempotency_key: str | None,
    request: dict[str, Any],
) -> tuple[OperationLog | None, str]:
    """幂等命令的统一入口。

    返回 (已有记录, 规范化请求摘要)。已有记录且输入一致时必须直接返回原结果；
    输入不一致时明确冲突。调用方在真正执行写操作后用同一摘要落库。
    """
    request_hash = normalized_request_hash(request)
    if not idempotency_key:
        return None, request_hash
    existing = find_by_idempotency(
        db,
        object_type=object_type,
        operation_type=operation_type,
        idempotency_key=idempotency_key,
    )
    if existing is None:
        return None, request_hash
    stored = existing.request_hash or normalized_request_hash(dict(existing.detail_json or {}))
    if stored != request_hash:
        raise BusinessError(
            "OPERATION_IDEMPOTENCY_CONFLICT",
            "同一幂等标识已用于不同的请求内容，拒绝复用已有操作",
            status_code=409,
            details={"operation_no": existing.log_no, "object_no": existing.object_no},
        )
    return existing, request_hash


def record_operation(
    db: Session,
    *,
    object_type: str,
    object_no: str,
    object_name: str,
    operation_type: str,
    detail: dict[str, Any] | None = None,
    idempotency_key: str | None = None,
    request_hash: str | None = None,
    command_no: str | None = None,
    duration_ms: int | None = None,
    result: str = "SUCCEEDED",
    failure_code: str | None = None,
    failure_message: str | None = None,
) -> OperationLog:
    """在业务事务内追加一条操作日志；调用方负责随业务一起提交。

    `result` 默认 SUCCEEDED：绝大多数记录的是已经真实完成的写操作。
    幂等命令必须显式传 FAILED——失败结果不落库的话，同幂等标识的重发会以为
    「什么都没发生」而重跑一次，把一次失败变成一次假成功。
    """
    log = OperationLog(
        log_no=public_no("LOG"),
        object_type=object_type,
        object_no=object_no,
        object_name=object_name[:255],
        operation_type=operation_type,
        result=result,
        idempotency_key=idempotency_key,
        request_hash=request_hash,
        command_no=command_no,
        duration_ms=duration_ms,
        detail_json=detail,
        failure_code=failure_code,
        failure_message=failure_message,
    )
    db.add(log)
    return log


def _business_entry_select():
    return select(
        OperationLog.log_no.label("entry_no"),
        literal("BUSINESS").label("source"),
        OperationLog.object_type.label("object_type"),
        OperationLog.object_no.label("object_no"),
        OperationLog.object_name.label("object_name"),
        OperationLog.operation_type.label("operation_type"),
        OperationLog.result.label("result"),
        # 列位置与命令查询对齐：第 8 列统一是耗时（业务为毫秒，命令为微秒）。
        OperationLog.duration_ms.label("duration_value"),
        OperationLog.command_no.label("command_no"),
        OperationLog.detail_json.label("detail_json"),
        OperationLog.failure_code.label("failure_code"),
        OperationLog.failure_message.label("failure_message"),
        OperationLog.created_at.label("created_at"),
        # 业务日志是追加写入的：写入时间即完成时间，没有单独的 finished_at 列。
        OperationLog.updated_at.label("finished_at"),
    )


def _command_entry_select():
    object_name = func.coalesce(
        select(AuditTask.task_name).where(AuditTask.task_no == BackgroundOperation.object_no).limit(1).scalar_subquery(),
        select(ImportJob.source_filename).where(ImportJob.job_no == BackgroundOperation.object_no).limit(1).scalar_subquery(),
        BackgroundOperation.object_no,
    )
    # 命令状态映射成统一结果口径；PENDING/RUNNING 是“执行中”，不是成功。
    result = case(
        (BackgroundOperation.status == "SUCCEEDED", literal("SUCCEEDED")),
        (BackgroundOperation.status.in_(("FAILED", "INTERRUPTED")), literal("FAILED")),
        else_=literal("ACCEPTED"),
    )
    return select(
        BackgroundOperation.operation_no.label("entry_no"),
        literal("COMMAND").label("source"),
        BackgroundOperation.object_type.label("object_type"),
        BackgroundOperation.object_no.label("object_no"),
        object_name.label("object_name"),
        BackgroundOperation.operation_type.label("operation_type"),
        result.label("result"),
        # 与业务日志同一列位置：命令的耗时按开始/结束时间计算，缺时间时为 NULL。
        func.timestampdiff(
            text("MICROSECOND"),
            BackgroundOperation.started_at,
            BackgroundOperation.finished_at,
        ).label("duration_value"),
        BackgroundOperation.operation_no.label("command_no"),
        BackgroundOperation.result_json.label("detail_json"),
        BackgroundOperation.failure_code.label("failure_code"),
        BackgroundOperation.failure_message.label("failure_message"),
        BackgroundOperation.created_at.label("created_at"),
        BackgroundOperation.finished_at.label("finished_at"),
    )


def list_operations_page(
    db: Session,
    *,
    object_type: str | None,
    operation_type: str | None,
    result: str | None,
    keyword: str | None,
    created_from: datetime | None,
    created_to: datetime | None,
    page: int,
    page_size: int,
) -> dict[str, Any]:
    """操作日志分页：业务写操作与后台命令并列展示，各自的编号独立可查。"""
    business = _business_entry_select().subquery()
    command = _command_entry_select().subquery()
    combined = union_all(
        select(
            business.c.entry_no,
            business.c.source,
            business.c.object_type,
            business.c.object_no,
            business.c.object_name,
            business.c.operation_type,
            business.c.result,
            business.c.duration_value,
            business.c.command_no,
            business.c.detail_json,
            business.c.failure_code,
            business.c.failure_message,
            business.c.created_at,
            business.c.finished_at,
        ),
        select(
            command.c.entry_no,
            command.c.source,
            command.c.object_type,
            command.c.object_no,
            command.c.object_name,
            command.c.operation_type,
            command.c.result,
            command.c.duration_value,
            command.c.command_no,
            command.c.detail_json,
            command.c.failure_code,
            command.c.failure_message,
            command.c.created_at,
            command.c.finished_at,
        ),
    ).subquery("operation_entries")

    conditions = []
    if object_type:
        conditions.append(combined.c.object_type == object_type)
    if operation_type:
        conditions.append(combined.c.operation_type == operation_type)
    if result:
        conditions.append(combined.c.result == result)
    if keyword:
        like = f"%{keyword.strip()}%"
        conditions.append(or_(combined.c.object_name.like(like), combined.c.object_no.like(like), combined.c.entry_no.like(like)))
    if created_from:
        conditions.append(combined.c.created_at >= created_from)
    if created_to:
        conditions.append(combined.c.created_at <= created_to)

    where_clause = and_(*conditions) if conditions else None
    count_statement = select(func.count()).select_from(combined)
    rows_statement = select(combined)
    if where_clause is not None:
        count_statement = count_statement.where(where_clause)
        rows_statement = rows_statement.where(where_clause)
    total = db.scalar(count_statement) or 0
    rows = db.execute(
        rows_statement.order_by(combined.c.created_at.desc(), combined.c.entry_no.desc())
        .limit(page_size)
        .offset((page - 1) * page_size)
    ).all()

    items: list[dict[str, Any]] = []
    for row in rows:
        raw_duration = row.duration_value
        duration_ms = None
        if raw_duration is not None:
            # 业务日志以毫秒存，命令的列为微秒差；按来源统一换算成毫秒。
            duration_ms = int(raw_duration) // 1000 if row.source == "COMMAND" else int(raw_duration)
        detail = row.detail_json if isinstance(row.detail_json, dict) else None
        items.append(
            {
                "entry_no": row.entry_no,
                "source": row.source,
                "object_type": row.object_type,
                "object_no": row.object_no,
                "object_name": row.object_name,
                "operation_type": row.operation_type,
                "result": row.result,
                "duration_ms": duration_ms,
                "command_no": row.command_no,
                "detail": detail,
                "failure_code": row.failure_code,
                "failure_message": row.failure_message,
                "created_at": row.created_at,
                "finished_at": row.finished_at,
            }
        )
    return {"items": items, "total": int(total), "page": page, "page_size": page_size}


