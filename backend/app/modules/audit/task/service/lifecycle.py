"""审核任务名称维护与删除。"""

import logging
from pathlib import Path
from typing import Any
from uuid import uuid4

from sqlalchemy.orm import Session

from app.common.exceptions import BusinessError
from app.core.transactions import after_commit, before_rollback
from app.modules.audit.field.service.value_files import AuditFieldValueFilesService
from app.modules.audit.task import crud
from app.modules.audit.task.model import AuditTask
from app.modules.system.operation.service.background import OperationBackgroundService
from app.modules.system.operation.service.operation_log import OperationLogService
from app.modules.system.operation.service.operation_types import OBJECT_AUDIT_TASK
from app.services.shared.directory_cleanup import remove_directory_tree
from app.services.shared.storage import audit_directory, resolve_storage_path

logger = logging.getLogger(__name__)


class AuditTaskLifecycleService:
    """本实体的服务方法；db 由构造时注入。"""

    def __init__(self, db: Session | None = None) -> None:
        self.db = db

    def update_task(self, task_no: str, payload: dict[str, Any]) -> AuditTask:
        """编辑任务基本资料：名称与备注。

        执行快照（场景/方案版本、任务 schema）不可改；已完成任务需要更换输入时
        应复制为新任务，保留原任务报告。
        """
        task = crud.AuditTaskCRUD(self.db).lock_by_no(task_no)
        if not task:
            raise BusinessError("AUDIT_TASK_NOT_FOUND", "审核任务不存在", status_code=404, details={"task_no": task_no})
        if "task_name" in payload and payload["task_name"] is not None:
            name = str(payload["task_name"]).strip()
            if not name:
                raise BusinessError("TASK_NAME_REQUIRED", "任务名称不能为空", status_code=400)
            task.task_name = name
        if "remark" in payload:
            remark = payload.get("remark")
            task.remark = (str(remark).strip() or None) if remark is not None else None
        OperationLogService(self.db).record_operation(

            object_type=OBJECT_AUDIT_TASK,
            object_no=task.task_no,
            object_name=task.task_name,
            operation_type="UPDATE",
            detail={"task_no": task.task_no},
        )
        crud.AuditTaskCRUD(self.db).flush()
        crud.AuditTaskCRUD(self.db).refresh(task)
        return task


    def delete_task(self, task_no: str) -> None:
        """永久删除任务、全部输入版本和审核数据，保留通用日志及创建/复制命令。

        文件目录先在原存储目录内改名；数据库事务失败时恢复目录。
        提交后清理文件若失败，明确报告已删除数据及尚未清理的路径。
        """
        task = crud.AuditTaskCRUD(self.db).lock_by_no(task_no)
        if not task:
            raise BusinessError("AUDIT_TASK_NOT_FOUND", "审核任务不存在", status_code=404, details={"task_no": task_no})
        OperationBackgroundService(self.db).assert_no_active_operation(OBJECT_AUDIT_TASK, task.task_no)
        if task.status == "RUNNING":
            raise BusinessError("AUDIT_TASK_RUNNING", "任务正在执行，不能删除", status_code=409, details={"task_no": task_no})
        # 与业务写入保持相同次序：先数据库任务行锁，再文件锁。
        AuditFieldValueFilesService(self.db).lock_task_files( task_no)
        task_directory = resolve_storage_path(audit_directory(task.task_no, task.created_at))
        task_root = resolve_storage_path("audit_tasks")
        if task_directory.parent != task_root:
            raise BusinessError("STORAGE_PATH_INVALID", "任务文件目录不安全", status_code=500)
        staged_directories: list[tuple[Path, Path]] = []

        def restore_files() -> None:
            for original, staged in reversed(staged_directories):
                staged.rename(original)

        def discard_files() -> None:
            for _original, staged in staged_directories:
                try:
                    remove_directory_tree(staged)
                except OSError as exc:
                    raise BusinessError(
                        "TASK_FILE_CLEANUP_FAILED", "任务数据已永久删除，但文件清理失败",
                        status_code=500, details={"task_no": task_no, "task_deleted": True,
                                                  "remaining_directory": str(staged)},
                    ) from exc

        before_rollback(self.db, restore_files)
        after_commit(self.db, discard_files)
        if task_directory.exists():
            staged = resolve_storage_path(f"audit_tasks/.deleting-{task.task_no}-{uuid4().hex}")
            task_directory.rename(staged)
            staged_directories.append((task_directory, staged))
        crud.AuditTaskCRUD(self.db).delete_task_data(task.id, task.task_no)
        OperationLogService(self.db).record_operation(
            object_type=OBJECT_AUDIT_TASK, object_no=task.task_no, object_name=task.task_name,
            operation_type="DELETE", permanent=True, detail={"task_no": task.task_no},
        )
        crud.AuditTaskCRUD(self.db).delete(task)
