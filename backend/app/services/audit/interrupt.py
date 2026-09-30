"""审核图等待点的启动。

任务创建、材料替换/补传/移除之后，都必须把业务事务与图等待点对齐：先落
WorkflowCheckpoint（业务侧权威记录），再让图真正停到 `await_node_confirmation`，
最后核对等待点确实存在。任一步失败都把任务显式标为 FAILED，**不**留下
「看起来可恢复、实际没有等待点」的任务。

启动等待点是审核命令层的事，因此放在这里而不是任务服务：任务服务只需要在
材料变化后调用它，不必成为审核层的依赖。
"""

from ...common.exceptions import BusinessError, exception_summary
from ...entities import AuditTask, BackgroundOperation, NodeExecution, WorkflowCheckpoint
from ...domain.identity import public_no
from . import flow as audit_flow
from ...graphs.audit_graph import audit_interrupt_is_pending, open_audit_interrupt
from sqlalchemy import select
from sqlalchemy.orm import Session
import logging


logger = logging.getLogger(__name__)


def _open_creation_interrupt(db: Session, task: AuditTask, operation_id: int) -> None:
    command = db.scalar(
        select(BackgroundOperation).where(BackgroundOperation.id == operation_id)
        .with_for_update().execution_options(populate_existing=True)
    )
    if command is None:
        raise BusinessError("TASK_COMMAND_MISSING", "任务创建命令记录不存在", status_code=500)
    if command.status == "SUCCEEDED":
        return
    if command.status == "FAILED":
        raise BusinessError(
            command.failure_code or "TASK_COMMAND_FAILED",
            command.failure_message or "任务等待点初始化失败",
            status_code=409,
        )
    if command.status != "PENDING":
        raise BusinessError("TASK_COMMAND_STATE_INVALID", "任务创建命令状态无效", status_code=500)
    command_result = command.result_json if isinstance(command.result_json, dict) else {}
    if not isinstance(command_result.get("initial_node"), str) or not isinstance(
        command_result.get("initial_checkpoint_ns"), str
    ):
        raise BusinessError("TASK_COMMAND_STATE_INVALID", "任务命令缺少初始节点或等待点身份", status_code=500)
    checkpoint_json = task.checkpoint_json
    if not isinstance(checkpoint_json, dict) or not isinstance(checkpoint_json.get("graph_checkpoint_ns"), str):
        raise BusinessError("AUDIT_CHECKPOINT_INVALID", "任务缺少创建时的流程检查点", status_code=500)
    if (
        task.status not in {"READY", "FAILED"}
        or task.current_node != command_result.get("initial_node")
        or checkpoint_json["graph_checkpoint_ns"] != command_result.get("initial_checkpoint_ns")
    ):
        # A later node or input revision is reachable only after the initial graph interrupt existed.
        return
    if task.status == "FAILED":
        execution_exists = db.scalar(
            select(NodeExecution.id).where(NodeExecution.audit_task_id == task.id).limit(1)
        ) is not None
        if execution_exists:
            return
        raise BusinessError("AUDIT_INTERRUPT_INIT_FAILED", "任务创建时的等待点初始化已经失败", status_code=409)
    namespace = checkpoint_json["graph_checkpoint_ns"]
    checkpoint = db.scalar(
        select(WorkflowCheckpoint).where(
            WorkflowCheckpoint.audit_task_id == task.id,
            WorkflowCheckpoint.thread_id == task.thread_id,
            WorkflowCheckpoint.checkpoint_ns == namespace,
        )
    )
    if checkpoint is None or checkpoint.status != "WAITING":
        raise BusinessError("AUDIT_CHECKPOINT_MISSING", "任务创建时的业务等待点不存在", status_code=500)
    if not audit_interrupt_is_pending(task.thread_id, namespace, "await_node_confirmation"):
        open_audit_interrupt(task.task_no, task.thread_id, namespace, db, audit_flow)
    if not audit_interrupt_is_pending(task.thread_id, namespace, "await_node_confirmation"):
        raise BusinessError("AUDIT_INTERRUPT_MISSING", "审核流程未产生节点确认等待点", status_code=500)


def _record_initialization_failure(
    db: Session, task: AuditTask, code: str, message: str, operation_id: int | None
) -> None:
    task.status = "FAILED"
    checkpoint_json = task.checkpoint_json
    namespace = checkpoint_json.get("graph_checkpoint_ns") if isinstance(checkpoint_json, dict) else None
    if isinstance(namespace, str):
        checkpoint = db.scalar(
            select(WorkflowCheckpoint).where(
                WorkflowCheckpoint.audit_task_id == task.id,
                WorkflowCheckpoint.thread_id == task.thread_id,
                WorkflowCheckpoint.checkpoint_ns == namespace,
                WorkflowCheckpoint.status.in_(("WAITING", "FAILED")),
            )
        )
        if checkpoint is not None:
            checkpoint.status = "FAILED"
            checkpoint.state_json = {**(checkpoint.state_json or {}), "init_failure_code": code}
    if operation_id is not None:
        command = db.get(BackgroundOperation, operation_id)
        if command is not None and command.status == "PENDING":
            command.status = "FAILED"
            command.phase = "INTERRUPT_FAILED"
            command.failure_code = code
            command.failure_message = message
    db.commit()


def resume_task_creation_interrupt(db: Session, task: AuditTask, operation_id: int) -> None:
    """Resume the same task after a crash between its SQL commit and graph interrupt."""
    try:
        _open_creation_interrupt(db, task, operation_id)
    except BusinessError as exc:
        _record_initialization_failure(db, task, exc.code, exc.message, operation_id)
        raise
    except Exception as exc:
        logger.exception("审核流程启动恢复发生未处理异常", extra={"task_no": task.task_no})
        _record_initialization_failure(db, task, "AUDIT_INTERRUPT_INIT_FAILED", exception_summary(exc), operation_id)
        raise BusinessError(
            "AUDIT_INTERRUPT_INIT_FAILED",
            f"审核流程启动失败，任务已标记为失败（{exception_summary(exc)}）",
            status_code=500,
        ) from exc


def start_audit_interrupt(db: Session, task: AuditTask, *, creation_operation_id: int | None = None) -> None:
    """只在明确的任务启动或材料替换时新建图等待点。"""
    sql_committed = False
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
        if creation_operation_id is not None:
            command = db.get(BackgroundOperation, creation_operation_id)
            if command is None:
                raise BusinessError("TASK_COMMAND_MISSING", "任务创建命令记录不存在", status_code=500)
            command.result_json = {
                **(command.result_json or {}),
                "initial_node": task.current_node,
                "initial_checkpoint_ns": graph_namespace,
            }
        db.commit()
        sql_committed = True
        if creation_operation_id is not None:
            _open_creation_interrupt(db, task, creation_operation_id)
        else:
            open_audit_interrupt(task.task_no, task.thread_id, graph_namespace, db, audit_flow)
            if not audit_interrupt_is_pending(task.thread_id, graph_namespace, "await_node_confirmation"):
                raise BusinessError("AUDIT_INTERRUPT_MISSING", "审核流程未产生节点确认等待点", status_code=500)
    except BusinessError as exc:
        if creation_operation_id is not None and not sql_committed:
            db.rollback()
            raise
        _record_initialization_failure(db, task, exc.code, exc.message, creation_operation_id)
        raise
    except Exception as exc:
        logger.exception("审核流程启动发生未处理异常", extra={"task_no": task.task_no})
        if creation_operation_id is not None and not sql_committed:
            db.rollback()
            raise BusinessError(
                "AUDIT_INTERRUPT_INIT_FAILED",
                f"审核流程启动失败，任务未落库（{exception_summary(exc)}）",
                status_code=500,
            ) from exc
        _record_initialization_failure(db, task, "AUDIT_INTERRUPT_INIT_FAILED", exception_summary(exc), creation_operation_id)
        raise BusinessError(
            "AUDIT_INTERRUPT_INIT_FAILED",
            f"审核流程启动失败，任务已标记为失败（{exception_summary(exc)}）",
            status_code=500,
        ) from exc
