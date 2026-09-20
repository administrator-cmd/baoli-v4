"""审核任务与场景版本的读取入口。

放在这里而不是放在某个业务服务里：审核命令层、图运行时、视图与任务服务都要用它，
把它挂在其中一个服务上会让其他服务为了一个 404 查询反向依赖那个服务，形成环。
本模块只依赖 models 与 errors，不依赖任何服务。
"""

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..errors import BusinessError
from ..models import AuditTask, ScenarioVersion


def task_or_404(db: Session, task_no: str, *, lock: bool = False) -> AuditTask:
    """按任务编号取任务；取不到时明确 404。

    `lock=True` 用于写路径：先锁定对象行，再做状态检查与幂等判断，
    避免检查与修改之间出现 TOCTOU 窗口。
    """
    statement = select(AuditTask).where(AuditTask.task_no == task_no)
    if lock:
        statement = statement.with_for_update()
    task = db.scalar(statement)
    if not task:
        raise BusinessError("AUDIT_TASK_NOT_FOUND", "审核任务不存在", status_code=404, details={"task_no": task_no})
    return task


def scenario_or_404(db: Session, scenario_code: str, version_no: int) -> ScenarioVersion:
    scenario = db.scalar(
        select(ScenarioVersion).where(
            ScenarioVersion.scenario_code == scenario_code,
            ScenarioVersion.version_no == version_no,
            ScenarioVersion.status == "PUBLISHED",
        )
    )
    if not scenario:
        raise BusinessError("SCENARIO_VERSION_NOT_FOUND", "已发布场景版本不存在", status_code=404)
    return scenario
