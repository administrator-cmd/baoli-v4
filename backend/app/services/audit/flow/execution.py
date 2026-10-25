"""当前执行记录的查询与身份核对。"""

from sqlalchemy import select
from sqlalchemy.orm import Session

from ....common.exceptions import BusinessError
from ....entity.tasks import AuditTask, NodeExecution


def running_execution(db: Session, task: AuditTask) -> NodeExecution | None:
    """本任务当前执行中的节点执行记录（最多一条，由 begin 与并发校验保证）。"""
    return db.scalar(
        select(NodeExecution)
        .where(NodeExecution.audit_task_id == task.id, NodeExecution.status == "RUNNING")
        .order_by(NodeExecution.id.desc())
    )


def execution_in_progress(db: Session, task: AuditTask, execution_id: int | None) -> NodeExecution:
    """本次执行记录。按 state 里的 id 取，取不到即显式失败（不模糊匹配「最新一条」）。"""
    if not isinstance(execution_id, int):
        raise BusinessError(
            "AUDIT_EXECUTION_MISSING",
            "审核流程缺少当前节点执行记录标识",
            status_code=500,
        )
    execution = db.get(NodeExecution, execution_id)
    if execution is None or execution.audit_task_id != task.id or execution.node_code != task.current_node:
        raise BusinessError(
            "AUDIT_EXECUTION_MISSING",
            "找不到当前节点的执行记录，本次执行未完成",
            status_code=500,
            details={"execution_id": execution_id, "node": task.current_node},
        )
    return execution
