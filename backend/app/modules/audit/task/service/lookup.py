"""审核任务的存在性查询：SQL 在 crud。"""

from sqlalchemy.orm import Session

from app.common.exceptions import BusinessError
from app.modules.audit.field.service.value_files import AuditFieldValueFilesService
from app.modules.audit.task import crud
from app.modules.audit.task.model import AuditTask


class AuditTaskLookupService:
    """本实体的服务方法；db 由构造时注入。"""

    def __init__(self, db: Session | None = None) -> None:
        self.db = db

    def task_or_404(self, task_no: str, *, lock: bool = False, values: bool = False) -> AuditTask:
        """按任务编号取任务；取不到时明确 404。

        `lock=True` 用于写路径：先锁定对象行，再做状态检查与幂等判断，
        避免检查与修改之间出现 TOCTOU 窗口。
        `values=True` 用于只读详情入口：在本事务首次查询前锁定文件，刷新任务清单；
        不与写路径的 `lock=True` 混用，写路径在持有任务行锁后才申请文件锁。
        """
        if values:
            # 先锁文件、再读取数据库清单；只读查询不申请数据库排他锁。
            AuditFieldValueFilesService(self.db).lock_task_files(task_no)
        task = crud.AuditTaskCRUD(self.db).by_no(task_no, lock=lock, values=values)
        if not task:
            raise BusinessError("AUDIT_TASK_NOT_FOUND", "审核任务不存在", status_code=404, details={"task_no": task_no})
        return task
