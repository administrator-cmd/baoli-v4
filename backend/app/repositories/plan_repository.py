from sqlalchemy.orm import Session

from ..entity import AuditPlan


def find_by_id(db: Session, plan_id: int | None) -> AuditPlan | None:
    return db.get(AuditPlan, plan_id) if plan_id is not None else None

