"""审核模板身份与版本号分配。

模板身份是 scenario_code（稳定标识），不是中文名称；中文名称只是显示与检索用的属性。
版本号由 scenario_template.latest_version_no 在同一事务内原子递增分配，
不依赖无保护的 max(version_no)+1；scenario_version 上的唯一约束仍是最终兜底。
"""

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..errors import BusinessError
from ..models import ScenarioTemplate, ScenarioVersion


def _lock_template(db: Session, scenario_code: str) -> ScenarioTemplate | None:
    return db.scalar(
        select(ScenarioTemplate).where(ScenarioTemplate.scenario_code == scenario_code).with_for_update()
    )


def ensure_template(db: Session, scenario_code: str, scenario_name: str) -> ScenarioTemplate:
    """确保模板身份行存在（用于"更新已有模板"）。"""
    template = _lock_template(db, scenario_code)
    if template is None:
        raise BusinessError(
            "TEMPLATE_NOT_FOUND",
            "要更新的审核模板不存在，请选择已有模板或新建模板",
            status_code=404,
            details={"scenario_code": scenario_code},
        )
    return template


def allocate_version(db: Session, scenario_code: str, scenario_name: str) -> int:
    """为新发布分配版本号。调用方必须在同一事务内完成发布提交。"""
    template = _lock_template(db, scenario_code)
    if template is None:
        db.add(
            ScenarioTemplate(
                scenario_code=scenario_code,
                scenario_name=scenario_name,
                latest_version_no=0,
            )
        )
        try:
            with db.begin_nested():
                db.flush()
        except IntegrityError as exc:
            # 并发建立同一模板身份：只回滚本层保存点，不冲掉发布事务里已锁定的作业。
            raise BusinessError(
                "TEMPLATE_VERSION_CONFLICT",
                "该审核模板刚刚被另一次发布创建，请重新提交",
                status_code=409,
                details={"scenario_code": scenario_code},
            ) from exc
        template = _lock_template(db, scenario_code)
        if template is None:
            raise BusinessError("TEMPLATE_VERSION_CONFLICT", "审核模板版本分配失败，请重新提交", status_code=409)
    template.latest_version_no = template.latest_version_no + 1
    template.scenario_name = scenario_name
    db.flush()
    return template.latest_version_no


def published_version(db: Session, scenario_code: str, version_no: int) -> ScenarioVersion | None:
    return db.scalar(
        select(ScenarioVersion).where(
            ScenarioVersion.scenario_code == scenario_code,
            ScenarioVersion.version_no == version_no,
        )
    )
