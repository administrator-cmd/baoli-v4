from sqlalchemy.orm import Session

from ..domain.identity import public_no
from ..common.exceptions import BusinessError
from ..entity import AuditTask
from ..repositories import task_repository
from .operation_log_service import normalized_request_hash
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
    request_hash = normalized_request_hash(request_payload)

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
            # 任务创建不再需要建立图等待点：提交命令记录即可。
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

