"""当前执行记录的查询与身份核对。"""

from sqlalchemy.orm import Session

from app.common.exceptions import BusinessError
from app.modules.audit.execution.crud import AuditExecutionCRUD
from app.modules.audit.execution.model import NodeExecution
from app.modules.audit.task.model import AuditTask


def running_execution(db: Session, task: AuditTask) -> NodeExecution | None:
    """本任务当前执行中的节点执行记录（最多一条，由 begin 与并发校验保证）。"""
    return AuditExecutionCRUD(db).running_for_task(task.id)


def execution_in_progress(db: Session, task: AuditTask, execution_id: int | None) -> NodeExecution:
    """本次执行记录。按 state 里的 id 取，取不到即显式失败（不模糊匹配「最新一条」）。"""
    if not isinstance(execution_id, int):
        raise BusinessError(
            "AUDIT_EXECUTION_MISSING",
            "审核流程缺少当前节点执行记录标识",
            status_code=500,
        )
    execution = AuditExecutionCRUD(db).by_id(execution_id)
    if execution is None or execution.audit_task_id != task.id or execution.node_code != task.current_node:
        raise BusinessError(
            "AUDIT_EXECUTION_MISSING",
            "找不到当前节点的执行记录，本次执行未完成",
            status_code=500,
            details={"execution_id": execution_id, "node": task.current_node},
        )
    return execution
