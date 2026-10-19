"""场景身份查询与行锁。"""

from sqlalchemy import select
from sqlalchemy.orm import Session
from ...entity import ScenarioTemplate


def lock_template_by_code(db: Session, scenario_code: str) -> ScenarioTemplate | None:
    return db.scalar(
        select(ScenarioTemplate).where(ScenarioTemplate.scenario_code == scenario_code).with_for_update()
    )


