"""方案各类配置行的有序读取。"""

from sqlalchemy.orm import Session
from ..shared import configuration as config_store
from ...entity import PlanField, PlanMaterial, PlanRule, PlanSection


def plan_config_rows(
    db: Session, plan_id: int
) -> tuple[list[PlanMaterial], list[PlanField], list[PlanRule], list[PlanSection]]:
    """方案自己的配置行，按用户可见的顺序。"""
    return (
        list(sorted(config_store.rows(db, PlanMaterial, plan_id), key=lambda item: (item.sort_no, item.id,))),
        list(sorted(config_store.rows(db, PlanField, plan_id), key=lambda item: (item.sort_no, item.id,))),
        list(sorted(config_store.rows(db, PlanRule, plan_id), key=lambda item: (item.sort_no, item.id,))),
        list(sorted(config_store.rows(db, PlanSection, plan_id), key=lambda item: (item.sort_no, item.id,))),
    )
