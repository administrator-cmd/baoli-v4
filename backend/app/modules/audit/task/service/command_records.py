"""任务创建和复制命令的幂等记录及结果保存。"""

from datetime import UTC, datetime
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.common.exceptions import BusinessError
from app.domain.shared.identity import public_no
from app.modules.audit.task.crud import AuditTaskCRUD
from app.modules.audit.task.model import AuditTask
from app.modules.system.operation.crud import SystemOperationCRUD
from app.modules.system.operation.model import BackgroundOperation
from app.modules.system.operation.service.operation_log import OperationLogService
from app.modules.system.operation.service.operation_types import OBJECT_AUDIT_TASK


class AuditTaskCommandRecordsService:
    """本实体的服务方法；db 由构造时注入。"""

    def __init__(self, db: Session | None = None) -> None:
        self.db = db

    def claim_task_command(self, *,
        object_type: str,
        operation_type: str,
        idempotency_key: str,
        request: dict[str, Any],
        request_hash: str,
        task_no: str,
    ) -> tuple[BackgroundOperation, bool]:
        """创建任务前占用幂等键；命令与任务由请求层一并提交。"""

        def existing_command(*, lock: bool = False) -> BackgroundOperation | None:
            return SystemOperationCRUD(self.db).background_by_operation_key(
                object_type=object_type,
                object_no=operation_type,
                operation_type=operation_type,
                idempotency_key=idempotency_key,
                lock=lock,
            )

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
            # 幂等键按命令类型唯一，不按方案或任务隔离。
            object_no=operation_type,
            operation_type=operation_type,
            idempotency_key=idempotency_key,
            status="PENDING",
            phase="COMMAND_ACCEPTED",
            request_hash=request_hash,
            request_json=request,
            result_json={"task_no": task_no},
        )
        try:
            with self.db.begin_nested():
                SystemOperationCRUD(self.db).add(command)
        except IntegrityError:
            # 唯一键冲突后读取最新提交记录，不沿用冲突前的快照。
            existing = existing_command(lock=True)
            if existing is None:
                raise
            return checked(existing)
        return command, False


    def task_of_command(self, command: BackgroundOperation) -> AuditTask:
        detail = command.result_json if isinstance(command.result_json, dict) else {}
        task_no = detail.get("task_no")
        task = AuditTaskCRUD(self.db).by_no(task_no, lock=True, values=True) if task_no else None
        if task is None:
            # 创建/复制命令保留幂等历史，重发旧命令不能重新创建已经被用户删除的任务。
            deletion = SystemOperationCRUD(self.db).deletion_log_id(
                object_type=OBJECT_AUDIT_TASK,
                object_no=task_no,
                operation_type="DELETE",
                lock=True,
            ) if task_no else None
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


    def finish_task_command(self, command: BackgroundOperation,
        *,
        object_no: str,
        object_name: str,
        detail: dict[str, Any],
    ) -> bool:
        """完成命令并记录日志，不提前提交请求事务。"""
        locked = SystemOperationCRUD(self.db).lock_by_id(command.id)
        if locked is None:
            raise BusinessError("TASK_COMMAND_MISSING", "任务命令记录不存在", status_code=500)
        if locked.status == "SUCCEEDED":
            return True
        if locked.status != "PENDING":
            raise BusinessError("TASK_COMMAND_FAILED", "任务等待点初始化失败，不能返回创建成功", status_code=409)
        OperationLogService(self.db).record_operation(

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
        SystemOperationCRUD(self.db).flush()
        return False


    def record_failed_task_command(self, command: BackgroundOperation,
        *,
        object_no: str,
        object_name: str,
        detail: dict[str, Any],
    ) -> None:
        """Append the original failure once, including after a crash before logging it."""
        locked = SystemOperationCRUD(self.db).lock_by_id(command.id)
        if locked is None or locked.status != "FAILED":
            raise BusinessError("TASK_COMMAND_STATE_INVALID", "任务命令尚未处于失败终态", status_code=500)
        if locked.phase == "FAILURE_LOGGED":
            return
        OperationLogService(self.db).record_operation(

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
        SystemOperationCRUD(self.db).flush()
