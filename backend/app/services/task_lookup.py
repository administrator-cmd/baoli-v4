"""审核任务与场景版本的读取入口。

放在这里而不是放在某个业务服务里：审核命令层、图运行时、视图与任务服务都要用它，
把它挂在其中一个服务上会让其他服务为了一个 404 查询反向依赖那个服务，形成环。
本模块只依赖 repository 与公共异常，不依赖任何其它服务。
"""

from sqlalchemy.orm import Session

from ..common.exceptions import BusinessError
from ..entities import AuditTask, ScenarioVersion
from ..repositories import scenario_repository, task_repository


def task_or_404(db: Session, task_no: str, *, lock: bool = False) -> AuditTask:
    """按任务编号取任务；取不到时明确 404。

    `lock=True` 用于写路径：先锁定对象行，再做状态检查与幂等判断，
    避免检查与修改之间出现 TOCTOU 窗口。
    """
    task = task_repository.find_by_no(db, task_no, lock=lock)
    if not task:
        raise BusinessError("AUDIT_TASK_NOT_FOUND", "审核任务不存在", status_code=404, details={"task_no": task_no})
    return task


def scenario_or_404(db: Session, scenario_code: str, version_no: int) -> ScenarioVersion:
    """按场景编码与版本号取已发布版本；不存在时 404。"""
    scenario = scenario_repository.find_published_version(db, scenario_code, version_no)
    if not scenario:
        raise BusinessError("SCENARIO_VERSION_NOT_FOUND", "已发布场景版本不存在", status_code=404)
    return scenario
