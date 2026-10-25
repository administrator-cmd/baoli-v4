"""审核任务的存在性查询。"""

from sqlalchemy import select
from sqlalchemy.orm import Session

from ....common.exceptions import BusinessError
from ....entity.tasks import AuditTask
from ..value_files import lock_task_files


def task_or_404(db: Session, task_no: str, *, lock: bool = False, values: bool = False) -> AuditTask:
    """按任务编号取任务；取不到时明确 404。

    `lock=True` 用于写路径：先锁定对象行，再做状态检查与幂等判断，
    避免检查与修改之间出现 TOCTOU 窗口。
    `values=True` 用于只读详情入口：在本事务首次查询前锁定文件，刷新任务清单；
    不与写路径的 `lock=True` 混用，写路径在持有任务行锁后才申请文件锁。
    """
    if values:
        # 先锁文件、再读取数据库清单；只读查询不申请数据库排他锁。
        lock_task_files(db, task_no)
    statement = select(AuditTask).where(AuditTask.task_no == task_no)
    if lock:
        statement = statement.with_for_update()
    if values:
        statement = statement.execution_options(populate_existing=True)
    task = db.scalar(statement)
    if not task:
        raise BusinessError("AUDIT_TASK_NOT_FOUND", "审核任务不存在", status_code=404, details={"task_no": task_no})
    return task
