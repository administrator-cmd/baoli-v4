"""场景版本与最新发布版本查询。"""

from sqlalchemy import select
from sqlalchemy.orm import Session
from ...entity import ScenarioVersion


def find_published_version(db: Session, scenario_code: str, version_no: int) -> ScenarioVersion | None:
    return db.scalar(
        select(ScenarioVersion).where(
            ScenarioVersion.scenario_code == scenario_code,
            ScenarioVersion.version_no == version_no,
            ScenarioVersion.status == "PUBLISHED",
        )
    )


def find_latest_published_version(db: Session, scenario_code: str) -> ScenarioVersion | None:
    # 排序只读取 ID，完整 JSON 配置按主键读取，不进入排序缓冲区。
    version_id = db.scalar(
        select(ScenarioVersion.id)
        .where(ScenarioVersion.scenario_code == scenario_code, ScenarioVersion.status == "PUBLISHED")
        .order_by(ScenarioVersion.version_no.desc())
        .limit(1)
    )
    return db.get(ScenarioVersion, version_id) if version_id is not None else None
