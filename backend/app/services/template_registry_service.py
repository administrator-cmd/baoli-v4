"""场景身份与版本号分配。

模板身份是 scenario_code（稳定标识），不是中文名称；中文名称只是显示与检索用的属性。
版本号由 scenario_template.latest_version_no 在同一事务内原子递增分配，
不依赖无保护的 max(version_no)+1；scenario_version 上的唯一约束仍是最终兜底。
"""

from sqlalchemy.orm import Session

from ..common.exceptions import BusinessError
from ..entity import ScenarioTemplate
from ..repositories import scenario_repository


def _lock_template(db: Session, scenario_code: str) -> ScenarioTemplate | None:
    return scenario_repository.lock_template_by_code(db, scenario_code)


def allocate_version(db: Session, scenario_code: str, scenario_name: str) -> int:
    """为新发布分配版本号。调用方必须在同一事务内完成发布提交。"""
    template = _lock_template(db, scenario_code)
    if template is None:
        raise BusinessError(
            "SCENARIO_NOT_FOUND", "审核场景不存在或已被删除，不能发布版本", status_code=404,
            details={"scenario_code": scenario_code},
        )
    template.latest_version_no = template.latest_version_no + 1
    template.scenario_name = scenario_name
    db.flush()
    return template.latest_version_no
