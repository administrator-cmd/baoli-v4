"""审核任务主记录查询。"""

from sqlalchemy import select
from sqlalchemy.orm import Session
from ...entity import AuditTask


def find_by_no(db: Session, task_no: str | None, *, lock: bool = False, refresh: bool = False) -> AuditTask | None:
    if not task_no:
        return None
    statement = select(AuditTask).where(AuditTask.task_no == task_no)
    if lock:
        statement = statement.with_for_update()
    if refresh:
        statement = statement.execution_options(populate_existing=True)
    return db.scalar(statement)
