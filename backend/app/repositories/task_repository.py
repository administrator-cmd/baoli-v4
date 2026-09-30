from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..entity import AuditTask, ManualReview, RuleResult, TaskDocument, TaskFieldValue


@dataclass(frozen=True)
class TaskReviewRows:
    reviews: list[ManualReview]
    rules: list[RuleResult]
    fields: list[TaskFieldValue]
    documents: list[TaskDocument]


def find_by_no(db: Session, task_no: str | None, *, lock: bool = False) -> AuditTask | None:
    if not task_no:
        return None
    statement = select(AuditTask).where(AuditTask.task_no == task_no)
    if lock:
        statement = statement.with_for_update()
    return db.scalar(statement)


def find_document_by_id(db: Session, document_id: int) -> TaskDocument | None:
    return db.get(TaskDocument, document_id)


def review_rows(db: Session, task_id: int) -> TaskReviewRows:
    return TaskReviewRows(
        reviews=list(
            db.scalars(
                select(ManualReview)
                .where(ManualReview.audit_task_id == task_id)
                .order_by(ManualReview.id.desc())
            ).all()
        ),
        rules=list(db.scalars(select(RuleResult).where(RuleResult.audit_task_id == task_id)).all()),
        fields=list(
            db.scalars(select(TaskFieldValue).where(TaskFieldValue.audit_task_id == task_id)).all()
        ),
        documents=list(
            db.scalars(select(TaskDocument).where(TaskDocument.audit_task_id == task_id)).all()
        ),
    )

