"""方案查询与场景发布版本的业务可用性检查。"""

from sqlalchemy import select
from sqlalchemy.orm import Session

from ...common.exceptions import BusinessError
from ...entity.plans import AuditPlan
from ...entity.scenarios import ScenarioTemplate, ScenarioVersion
from ..scenario.lookup import find_latest_published_version


def plan_or_404(db: Session, plan_code: str, *, lock: bool = False, include_deleted: bool = False) -> AuditPlan:
    """按方案编码取方案；取不到即 404。lock 供写路径先锁行，include_deleted 供恢复与历史查看。"""
    statement = select(AuditPlan).where(AuditPlan.plan_code == plan_code)
    if not include_deleted:
        statement = statement.where(AuditPlan.deleted_at.is_(None))
    if lock:
        statement = statement.with_for_update()
    plan = db.scalar(statement)
    if not plan:
        raise BusinessError(
            "PLAN_NOT_FOUND",
            "审核方案不存在或已被删除",
            status_code=404,
            details={"plan_code": plan_code},
        )
    return plan


def _scenario_or_404(db: Session, scenario_code: str) -> ScenarioTemplate:
    """取场景身份；不存在或已删除时 404。

    只做「存在性」判断。能不能用来建方案由 `_published_scenario_or_fail` 判断：
    存在但未发布、已停用的场景同样不能建方案，那是业务状态问题而不是找不到。
    """
    template = db.scalar(select(ScenarioTemplate).where(ScenarioTemplate.scenario_code == scenario_code))
    if not template:
        raise BusinessError(
            "SCENARIO_NOT_FOUND",
            "审核场景不存在或已被删除",
            status_code=404,
            details={"scenario_code": scenario_code},
        )
    return template


def _published_scenario_or_fail(db: Session, scenario_code: str) -> ScenarioVersion:
    """业务用户可用的场景版本：场景必须未删除、已启用、且已有已发布版本。

    业务用户只能从「已发布且启用」的场景建方案。三种不可用状态
    分别明确报出，不合并成一个「场景不可用」——它们对管理员的处理动作不同：
    草稿还没发布要去发布，场景停用要去启用，场景删除是另一回事。

    没有已发布版本时明确失败，不先建一个没有版本的半成品方案再等后台补绑定：
    那会让业务用户拿到一个看似存在、实际无法创建任务的方案。
    """
    template = _scenario_or_404(db, scenario_code)
    if not template.enabled:
        raise BusinessError(
            "SCENARIO_DISABLED",
            f"场景“{template.scenario_name}”已停用，不能用于创建方案",
            status_code=409,
            details={"scenario_code": scenario_code},
        )
    version = find_latest_published_version(db, scenario_code)
    if version is None:
        raise BusinessError(
            "SCENARIO_NOT_PUBLISHED",
            f"场景“{template.scenario_name}”还没有已发布的版本，不能创建方案：请先由管理员发布场景",
            status_code=409,
            details={"scenario_code": scenario_code},
        )
    return version


def _published_scenario_version_or_fail(
    db: Session, scenario_code: str, version_no: int
) -> ScenarioVersion:
    """按页面预览的明确版本号取可建方案的已发布版本。

    版本号与当前允许建方案的版本不一致时返回 `PLAN_VERSION_CHANGED`，由页面刷新后重试：
    静默改取「当前最新版本」会让用户看着一版预览、实际建出另一版的方案。
    """
    latest = _published_scenario_or_fail(db, scenario_code)
    if latest.version_no != version_no:
        raise BusinessError(
            "PLAN_VERSION_CHANGED",
            f"场景版本已变化（预览的是第 {version_no} 版，当前可用的最新已发布版本是第 {latest.version_no} 版）。"
            "请刷新页面重新核对范围后再创建方案",
            status_code=409,
            details={
                "scenario_code": scenario_code,
                "previewed_version_no": version_no,
                "current_version_no": latest.version_no,
            },
        )
    return latest
