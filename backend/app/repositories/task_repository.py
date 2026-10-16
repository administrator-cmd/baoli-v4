from sqlalchemy import select
from sqlalchemy.orm import Session

from ..common.exceptions import BusinessError
from ..entity import AuditTask, TaskDocument


def find_by_no(db: Session, task_no: str | None, *, lock: bool = False) -> AuditTask | None:
    if not task_no:
        return None
    statement = select(AuditTask).where(AuditTask.task_no == task_no)
    if lock:
        statement = statement.with_for_update()
    return db.scalar(statement)


def find_document_by_id(db: Session, document_id: int) -> TaskDocument | None:
    return db.get(TaskDocument, document_id)
