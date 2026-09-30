from sqlalchemy.orm import Session

from ..domain.identity import public_no
from ..common.exceptions import BusinessError
from ..entity import AuditTask
from ..repositories import task_repository
from .audit.interrupt import resume_task_creation_interrupt
from .operation_log_service import require_idempotent_replay
from .operation_types import OBJECT_AUDIT_TASK
from .task_service import (
    claim_task_command,
    copy_task,
    finish_task_command,
    record_failed_task_command,
    task_of_command,
)


def copy_audit_task(
    db: Session,
    source_task_no: str,
    *,
    task_name: str,
    idempotency_key: str,
) -> AuditTask:
    """执行幂等的任务复制命令，事务和恢复逻辑不泄露到 API 层。"""
    request_payload = {"task_no": source_task_no, "task_name": task_name}
    existing, request_hash = require_idempotent_replay(
        db,
        object_type=OBJECT_AUDIT_TASK,
        operation_type="TASK_COPY",
        idempotency_key=idempotency_key,
        request=request_payload,
    )
    if existing is not None:
        if str(existing.result or "") != "SUCCEEDED":
            raise BusinessError(
                "TASK_COPY_PREVIOUSLY_FAILED",
                f"该幂等标识对应的复制任务命令此前执行失败：{existing.failure_message or '原因未记录'}。"
                "请更换幂等标识后重试",
                status_code=409,
                details={"log_no": existing.log_no, "failure_code": existing.failure_code},
            )
        detail = existing.detail_json if isinstance(existing.detail_json, dict) else {}
        replayed = task_repository.find_by_no(db, detail.get("new_task_no"))
        if replayed is None:
            raise BusinessError(
                "TASK_COPY_REPLAY_UNAVAILABLE",
                "该幂等标识对应的任务记录不存在，请更换标识后重新复制",
                status_code=409,
                details={"log_no": existing.log_no},
            )
        return replayed

    new_task_no = public_no("TASK")
    command, reused = claim_task_command(
        db,
        object_type=OBJECT_AUDIT_TASK,
        operation_type="TASK_COPY",
        idempotency_key=idempotency_key,
        request=request_payload,
        request_hash=request_hash,
        task_no=new_task_no,
    )
    if reused:
        copied = task_of_command(db, command)
        detail = {"source_task_no": source_task_no, "new_task_no": copied.task_no}
        if command.status == "FAILED":
            record_failed_task_command(
                db, command, object_no=source_task_no, object_name=task_name, detail=detail
            )
            raise BusinessError(
                "TASK_COPY_PREVIOUSLY_FAILED",
                f"该幂等标识对应的复制任务命令此前执行失败：{command.failure_message or '原因未记录'}。",
                status_code=409,
                details={
                    "operation_no": command.operation_no,
                    "new_task_no": copied.task_no,
                    "failure_code": command.failure_code,
                },
            )
        if command.status == "PENDING":
            try:
                resume_task_creation_interrupt(db, copied, command.id)
            except BusinessError:
                record_failed_task_command(
                    db, command, object_no=source_task_no, object_name=task_name, detail=detail
                )
                raise
            finish_task_command(
                db,
                command,
                object_no=source_task_no,
                object_name=task_name,
                detail={**detail, "plan_scope_hash": copied.task_schema_json.get("plan_scope_hash")},
            )
        elif command.status != "SUCCEEDED":
            raise BusinessError("TASK_COMMAND_STATE_INVALID", "任务复制命令状态无效", status_code=500)
        return copied

    try:
        created = copy_task(
            db,
            source_task_no,
            task_name=task_name,
            new_task_no=new_task_no,
            creation_operation_id=command.id,
        )
    except BusinessError:
        db.rollback()
        failed = task_repository.find_by_no(db, new_task_no)
        if failed is not None and failed.status == "FAILED":
            record_failed_task_command(
                db,
                command,
                object_no=source_task_no,
                object_name=task_name,
                detail={"source_task_no": source_task_no, "new_task_no": new_task_no},
            )
        raise
    finish_task_command(
        db,
        command,
        object_no=source_task_no,
        object_name=task_name,
        detail={
            "source_task_no": source_task_no,
            "new_task_no": created.task_no,
            "plan_scope_hash": created.task_schema_json.get("plan_scope_hash"),
        },
    )
    return created

