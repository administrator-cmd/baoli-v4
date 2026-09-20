"""审核图等待点的启动。

任务创建、材料替换/补传/移除之后，都必须把业务事务与图等待点对齐：先落
WorkflowCheckpoint（业务侧权威记录），再让图真正停到 `await_node_confirmation`，
最后核对等待点确实存在。任一步失败都把任务显式标为 FAILED，**不**留下
「看起来可恢复、实际没有等待点」的任务。

启动等待点是审核命令层的事，因此放在这里而不是任务服务：任务服务只需要在
材料变化后调用它，不必成为审核层的依赖。
"""

from ...errors import BusinessError, exception_summary
from ...models import AuditTask, WorkflowCheckpoint
from ...domain.identity import public_no
from . import flow as audit_flow
from ...graphs.audit_graph import audit_interrupt_is_pending, open_audit_interrupt
from sqlalchemy import update
from sqlalchemy.orm import Session
import logging


logger = logging.getLogger(__name__)


def start_audit_interrupt(db: Session, task: AuditTask) -> None:
    """只在明确的任务启动或材料替换时新建图等待点。"""
    checkpoint: WorkflowCheckpoint | None = None
    try:
        previous = task.checkpoint_json
        if not isinstance(previous, dict) or not isinstance(previous.get("node_index"), int):
            raise BusinessError("AUDIT_CHECKPOINT_INVALID", "审核流程检查点内容无效", status_code=500)
        graph_namespace = f"audit:{public_no('flow')}"
        state = {
            "flow": "AUDIT",
            "phase": "AWAITING_CONFIRMATION",
            "current_node": task.current_node,
            "node_index": previous["node_index"],
        }
        task.checkpoint_json = {
            "node_index": previous["node_index"],
            "current_node": task.current_node,
            "phase": "AWAITING_CONFIRMATION",
            "graph_checkpoint_ns": graph_namespace,
        }
        checkpoint = WorkflowCheckpoint(
            audit_task_id=task.id,
            thread_id=task.thread_id,
            checkpoint_ns=graph_namespace,
            status="WAITING",
            state_json=state,
        )
        db.add(checkpoint)
        db.commit()
        open_audit_interrupt(task.task_no, task.thread_id, graph_namespace, db, audit_flow)
        if not audit_interrupt_is_pending(task.thread_id, graph_namespace, "await_node_confirmation"):
            raise BusinessError("AUDIT_INTERRUPT_MISSING", "审核流程未产生节点确认等待点", status_code=500)
    except BusinessError:
        task.status = "FAILED"
        if checkpoint:
            checkpoint.status = "FAILED"
        db.execute(
            update(WorkflowCheckpoint)
            .where(WorkflowCheckpoint.audit_task_id == task.id, WorkflowCheckpoint.status == "WAITING")
            .values(status="FAILED")
        )
        db.commit()
        raise
    except Exception as exc:
        logger.exception("审核流程启动发生未处理异常", extra={"task_no": task.task_no})
        task.status = "FAILED"
        if checkpoint:
            checkpoint.status = "FAILED"
        db.execute(
            update(WorkflowCheckpoint)
            .where(WorkflowCheckpoint.audit_task_id == task.id, WorkflowCheckpoint.status == "WAITING")
            .values(status="FAILED")
        )
        db.commit()
        raise BusinessError(
            "AUDIT_INTERRUPT_INIT_FAILED",
            f"审核流程启动失败，任务已标记为失败（{exception_summary(exc)}）",
            status_code=500,
        ) from exc
