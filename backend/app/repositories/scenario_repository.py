from sqlalchemy import select
from sqlalchemy.orm import Session

from ..entities import ScenarioDraft, ScenarioDraftSheet, ScenarioTemplate, ScenarioVersion


def find_template_by_code(db: Session, scenario_code: str) -> ScenarioTemplate | None:
    return db.scalar(select(ScenarioTemplate).where(ScenarioTemplate.scenario_code == scenario_code))


def lock_template_by_code(db: Session, scenario_code: str) -> ScenarioTemplate | None:
    return db.scalar(
        select(ScenarioTemplate).where(ScenarioTemplate.scenario_code == scenario_code).with_for_update()
    )


def find_version_by_id(db: Session, version_id: int | None) -> ScenarioVersion | None:
    return db.get(ScenarioVersion, version_id) if version_id is not None else None




def find_published_version(db: Session, scenario_code: str, version_no: int) -> ScenarioVersion | None:
    return db.scalar(
        select(ScenarioVersion).where(
            ScenarioVersion.scenario_code == scenario_code,
            ScenarioVersion.version_no == version_no,
            ScenarioVersion.status == "PUBLISHED",
        )
    )


def add_template(db: Session, template: ScenarioTemplate) -> None:
    db.add(template)


def draft_sheet_roles(db: Session, scenario_code: str) -> list[str]:
    draft = db.scalar(select(ScenarioDraft).where(ScenarioDraft.scenario_code == scenario_code))
    if draft is None:
        return []
    return list(
        db.scalars(
            select(ScenarioDraftSheet.sheet_role)
            .where(ScenarioDraftSheet.draft_id == draft.id)
            .order_by(ScenarioDraftSheet.sort_no, ScenarioDraftSheet.id)
        ).all()
    )

