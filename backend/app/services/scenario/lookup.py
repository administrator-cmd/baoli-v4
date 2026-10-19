"""场景身份与发布版本的存在性查询。"""

from sqlalchemy import select
from sqlalchemy.orm import Session
from ...common.exceptions import BusinessError
from ...entity import ScenarioTemplate, ScenarioVersion
from ...repositories.scenario import version as scenario_version


def template_or_404(
    db: Session,
    scenario_code: str,
    *,
    lock: bool = False,
) -> ScenarioTemplate:
    """按场景编码取场景；取不到即 404，写路径可先锁行。"""
    statement = select(ScenarioTemplate).where(ScenarioTemplate.scenario_code == scenario_code)
    if lock:
        statement = statement.with_for_update()
    template = db.scalar(statement)
    if not template:
        raise BusinessError(
            "SCENARIO_NOT_FOUND",
            "审核场景不存在或已被删除",
            status_code=404,
            details={"scenario_code": scenario_code},
        )
    return template


def version_or_404(db: Session, scenario_code: str, version_no: int) -> ScenarioVersion:
    """按场景编码与版本号取已发布版本；取不到即 404。"""
    version = db.scalar(
        select(ScenarioVersion).where(
            ScenarioVersion.scenario_code == scenario_code,
            ScenarioVersion.version_no == version_no,
        )
    )
    if not version:
        raise BusinessError(
            "SCENARIO_VERSION_NOT_FOUND",
            "场景版本不存在",
            status_code=404,
            details={"scenario_code": scenario_code, "version_no": version_no},
        )
    return version


def scenario_or_404(db: Session, scenario_code: str, version_no: int) -> ScenarioVersion:
    """按场景编码与版本号取已发布版本；不存在时 404。"""
    scenario = scenario_version.find_published_version(db, scenario_code, version_no)
    if not scenario:
        raise BusinessError("SCENARIO_VERSION_NOT_FOUND", "已发布场景版本不存在", status_code=404)
    return scenario
