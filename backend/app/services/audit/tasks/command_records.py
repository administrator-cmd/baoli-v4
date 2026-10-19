"""任务创建和复制命令的幂等记录及结果保存。"""

from datetime import UTC, datetime
from typing import Any
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from ....common.exceptions import BusinessError
from ....entity import AuditTask, BackgroundOperation, OperationLog
from ....domain.identity import public_no
from ...system.operation_types import OBJECT_AUDIT_TASK
from ...system.operation_log import record_operation


def claim_task_command(
    db: Session,
    *,
    object_type: str,
    operation_type: str,
    idempotency_key: str,
    request: dict[str, Any],
    request_hash: str,
    task_no: str,
) -> tuple[BackgroundOperation, bool]:
    """Reserve the command before creating a task; the first checkpoint commit saves both."""
    identity = (
        BackgroundOperation.object_type == object_type,
        BackgroundOperation.object_no == operation_type,
        BackgroundOperation.operation_type == operation_type,
        BackgroundOperation.idempotency_key == idempotency_key,
    )

    def existing_command() -> BackgroundOperation | None:
        return db.scalar(select(BackgroundOperation).where(*identity))

    def checked(command: BackgroundOperation) -> tuple[BackgroundOperation, bool]:
        if command.request_hash != request_hash:
            raise BusinessError(
                "OPERATION_IDEMPOTENCY_CONFLICT",
                "同一幂等标识已用于不同的请求内容，拒绝复用已有操作",
                status_code=409,
                details={"operation_no": command.operation_no},
            )
        return command, True

    existing = existing_command()
    if existing is not None:
        return checked(existing)
    command = BackgroundOperation(
        operation_no=public_no("OP"),
        object_type=object_type,
        # The operation_log key is global within a command type, not per plan/task.
        object_no=operation_type,
        operation_type=operation_type,
        idempotency_key=idempotency_key,
        status="PENDING",
        phase="COMMAND_ACCEPTED",
        request_hash=request_hash,
        request_json=request,
        result_json={"task_no": task_no},
    )
    db.add(command)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        existing = existing_command()
        if existing is None:
            raise
        return checked(existing)
    return command, False


def task_of_command(db: Session, command: BackgroundOperation) -> AuditTask:
    detail = command.result_json if isinstance(command.result_json, dict) else {}
    task_no = detail.get("task_no")
    task = db.scalar(select(AuditTask).where(AuditTask.task_no == task_no)) if task_no else None
    if task is None:
        # 创建/复制命令保留幂等历史，重发旧命令不能重新创建已经被用户删除的任务。
        deletion = db.scalar(select(OperationLog.id).where(
            OperationLog.object_type == OBJECT_AUDIT_TASK,
            OperationLog.object_no == task_no,
            OperationLog.operation_type == "DELETE",
            OperationLog.detail_json["permanent"].as_boolean().is_(True),
        ).limit(1)) if task_no else None
        if deletion is not None:
            raise BusinessError(
                "AUDIT_TASK_DELETED",
                "该创建或复制命令对应的审核任务已永久删除，无法复用",
                status_code=410,
                details={"operation_no": command.operation_no, "task_no": task_no},
            )
        raise BusinessError(
            "TASK_COMMAND_TASK_MISSING",
            "任务命令已提交，但关联任务不存在，命令与任务记录不一致",
            status_code=500,
            details={"operation_no": command.operation_no, "task_no": task_no},
        )
    return task


def finish_task_command(
    db: Session,
    command: BackgroundOperation,
    *,
    object_no: str,
    object_name: str,
    detail: dict[str, Any],
) -> bool:
    """Complete the command and append its immutable audit log in one transaction."""
    locked = db.scalar(
        select(BackgroundOperation).where(BackgroundOperation.id == command.id)
        .with_for_update().execution_options(populate_existing=True)
    )
    if locked is None:
        raise BusinessError("TASK_COMMAND_MISSING", "任务命令记录不存在", status_code=500)
    if locked.status == "SUCCEEDED":
        return True
    if locked.status != "PENDING":
        raise BusinessError("TASK_COMMAND_FAILED", "任务等待点初始化失败，不能返回创建成功", status_code=409)
    record_operation(
        db,
        object_type=locked.object_type,
        object_no=object_no,
        object_name=object_name,
        operation_type=locked.operation_type,
        detail=detail,
        idempotency_key=locked.idempotency_key,
        request_hash=locked.request_hash,
    )
    locked.status = "SUCCEEDED"
    locked.phase = "INTERRUPT_WAITING"
    locked.finished_at = datetime.now(UTC)
    db.commit()
    return False


def record_failed_task_command(
    db: Session,
    command: BackgroundOperation,
    *,
    object_no: str,
    object_name: str,
    detail: dict[str, Any],
) -> None:
    """Append the original failure once, including after a crash before logging it."""
    locked = db.scalar(
        select(BackgroundOperation).where(BackgroundOperation.id == command.id)
        .with_for_update().execution_options(populate_existing=True)
    )
    if locked is None or locked.status != "FAILED":
        raise BusinessError("TASK_COMMAND_STATE_INVALID", "任务命令尚未处于失败终态", status_code=500)
    if locked.phase == "FAILURE_LOGGED":
        return
    record_operation(
        db,
        object_type=locked.object_type,
        object_no=object_no,
        object_name=object_name,
        operation_type=locked.operation_type,
        detail=detail,
        idempotency_key=locked.idempotency_key,
        request_hash=locked.request_hash,
        result="FAILED",
        failure_code=locked.failure_code,
        failure_message=locked.failure_message,
    )
    locked.phase = "FAILURE_LOGGED"
    db.commit()
