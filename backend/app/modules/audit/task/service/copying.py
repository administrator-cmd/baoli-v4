"""任务复制命令的幂等处理与事务编排。"""

from sqlalchemy.orm import Session

from app.common.exceptions import BusinessError
from app.domain.identity import public_no
from app.modules.audit.task.model import AuditTask
from app.modules.audit.task.service.command_records import AuditTaskCommandRecordsService
from app.modules.audit.task.service.creation import AuditTaskCreationService
from app.modules.system.operation.service.operation_log import OperationLogService
from app.modules.system.operation.service.operation_types import OBJECT_AUDIT_TASK


class AuditTaskCopyingService:
    """本实体的服务方法；db 由构造时注入。"""

    def __init__(self, db: Session | None = None) -> None:
        self.db = db

    def copy_audit_task(self, source_task_no: str,
        *,
        task_name: str,
        idempotency_key: str,
    ) -> AuditTask:
        """写入幂等复制命令与新任务，由请求层一并提交。"""
        request_payload = {"task_no": source_task_no, "task_name": task_name}
        request_hash = OperationLogService().normalized_request_hash(request_payload)

        new_task_no = public_no("TASK")
        command, reused = AuditTaskCommandRecordsService(self.db).claim_task_command(

            object_type=OBJECT_AUDIT_TASK,
            operation_type="TASK_COPY",
            idempotency_key=idempotency_key,
            request=request_payload,
            request_hash=request_hash,
            task_no=new_task_no,
        )
        if reused:
            copied = AuditTaskCommandRecordsService(self.db).task_of_command( command)
            detail = {"source_task_no": source_task_no, "new_task_no": copied.task_no}
            if command.status == "FAILED":
                AuditTaskCommandRecordsService(self.db).record_failed_task_command(
                     command, object_no=source_task_no, object_name=task_name, detail=detail
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
                AuditTaskCommandRecordsService(self.db).finish_task_command(

                    command,
                    object_no=source_task_no,
                    object_name=task_name,
                    detail={**detail, "plan_scope_hash": copied.task_schema_json.get("plan_scope_hash")},
                )
            elif command.status != "SUCCEEDED":
                raise BusinessError("TASK_COMMAND_STATE_INVALID", "任务复制命令状态无效", status_code=500)
            return copied

        created = AuditTaskCreationService(self.db).copy_task(

            source_task_no,
            task_name=task_name,
            new_task_no=new_task_no,
        )
        AuditTaskCommandRecordsService(self.db).finish_task_command(

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
