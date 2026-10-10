from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..common.exceptions import BusinessError
from ..entity import AuditTask, ManualReview, RuleResult, TaskDocument, TaskFieldValue
from ..services import task_csv_service


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
    task = db.get(AuditTask, task_id)
    if task is None:
        raise BusinessError("TASK_NOT_FOUND", "审核任务不存在", status_code=404, details={"task_id": task_id})
    rule_rows = list(db.scalars(select(RuleResult).where(RuleResult.audit_task_id == task_id)).all())
    field_rows = list(db.scalars(select(TaskFieldValue).where(TaskFieldValue.audit_task_id == task_id)).all())
    return TaskReviewRows(
        reviews=list(
            db.scalars(
                select(ManualReview)
                .where(ManualReview.audit_task_id == task_id)
                .order_by(ManualReview.id.desc())
            ).all()
        ),
        # 结果表只保留身份与关联索引；复核页面展示的业务值必须从任务 CSV 恢复。
        rules=task_csv_service.from_index(db, task, RuleResult, rule_rows),
        fields=task_csv_service.from_index(db, task, TaskFieldValue, field_rows),
        documents=list(
            db.scalars(select(TaskDocument).where(TaskDocument.audit_task_id == task_id)).all()
        ),
    )

