"""任务复制命令的幂等处理与事务编排。"""

from sqlalchemy import select
from sqlalchemy.orm import Session

from ....common.exceptions import BusinessError
from ....domain.identity import public_no
from ....entity.tasks import AuditTask
from ...system.operation_log import normalized_request_hash
from ...system.operation_types import OBJECT_AUDIT_TASK
from .command_records import claim_task_command, finish_task_command, record_failed_task_command, task_of_command
from .creation import copy_task


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
        failed = db.scalar(select(AuditTask).where(AuditTask.task_no == new_task_no))
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
