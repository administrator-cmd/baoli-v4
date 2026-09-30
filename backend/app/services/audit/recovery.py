"""Read-only eligibility for rebuilding a failed audit node's graph interrupt."""

from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ...common.exceptions import BusinessError
from ...entities import AuditTask, NodeExecution, WorkflowCheckpoint
from ..background_service import assert_no_active_operation, latest_operation
from ..operation_types import OBJECT_AUDIT_TASK
from .state import _scenario_nodes


@dataclass(frozen=True)
class FailedNodeRecovery:
    nodes: list[dict[str, Any]]
    current_index: int
    old_checkpoint: WorkflowCheckpoint
    latest_execution: NodeExecution | None
    source_operation_no: str | None
    init_failure_code: str | None


def failed_node_recovery(db: Session, task: AuditTask) -> FailedNodeRecovery:
    """Validate the same stored evidence used by both task detail and recovery command."""
    if task.deleted_at is not None:
        raise BusinessError("AUDIT_TASK_DELETED", "任务已被删除，不能恢复失败节点", status_code=409)
    if task.status != "FAILED":
        raise BusinessError(
            "AUDIT_TASK_NOT_RECOVERABLE",
            "只有检查点丢失或等待点初始化失败的任务才能恢复",
            status_code=409,
        )

    assert_no_active_operation(db, OBJECT_AUDIT_TASK, task.task_no)
    running_execution = db.scalar(
        select(NodeExecution).where(
            NodeExecution.audit_task_id == task.id,
            NodeExecution.status == "RUNNING",
        )
    )
    if running_execution is not None:
        raise BusinessError("AUDIT_NODE_RUNNING", "当前任务仍有执行中的节点，不能恢复", status_code=409)

    operation = latest_operation(db, OBJECT_AUDIT_TASK, task.task_no)
    _, nodes, current_index = _scenario_nodes(db, task)
    checkpoint_json = task.checkpoint_json
    if (
        not isinstance(checkpoint_json, dict)
        or checkpoint_json.get("current_node") != task.current_node
        or checkpoint_json.get("node_index") != current_index
        or checkpoint_json.get("phase") != "AWAITING_CONFIRMATION"
        or not isinstance(checkpoint_json.get("graph_checkpoint_ns"), str)
    ):
        raise BusinessError(
            "AUDIT_CHECKPOINT_INVALID",
            "任务业务检查点与当前节点不一致，不能恢复",
            status_code=409,
        )
    old_checkpoint = db.scalar(
        select(WorkflowCheckpoint).where(
            WorkflowCheckpoint.audit_task_id == task.id,
            WorkflowCheckpoint.thread_id == task.thread_id,
            WorkflowCheckpoint.checkpoint_ns == checkpoint_json["graph_checkpoint_ns"],
        )
    )
    if (
        old_checkpoint is None
        or old_checkpoint.status != "FAILED"
        or not isinstance(old_checkpoint.state_json, dict)
        or old_checkpoint.state_json.get("current_node") != task.current_node
        or old_checkpoint.state_json.get("node_index") != current_index
        or old_checkpoint.state_json.get("phase") != "AWAITING_CONFIRMATION"
    ):
        raise BusinessError(
            "AUDIT_CHECKPOINT_INVALID",
            "旧审核流程检查点不存在或状态不符合恢复条件",
            status_code=409,
        )

    checkpoint_missing = (
        operation is not None
        and operation.operation_type == "NODE_EXECUTE"
        and operation.status == "FAILED"
        and operation.failure_code == "AUDIT_CHECKPOINT_MISSING"
        and operation.target_node == task.current_node
        and operation.input_version == str(task.input_revision)
    )
    init_failure_code = old_checkpoint.state_json.get("init_failure_code")
    if not checkpoint_missing and not isinstance(init_failure_code, str):
        raise BusinessError(
            "AUDIT_TASK_NOT_RECOVERABLE",
            "没有可核对的检查点丢失或等待点初始化失败记录，不能恢复该任务",
            status_code=409,
        )

    latest_execution = db.scalar(
        select(NodeExecution)
        .where(
            NodeExecution.audit_task_id == task.id,
            NodeExecution.node_code == task.current_node,
            NodeExecution.status != "INVALIDATED",
        )
        .order_by(NodeExecution.id.desc())
    )
    if latest_execution is not None and latest_execution.status not in {"FAILED", "BLOCKED"}:
        raise BusinessError(
            "AUDIT_EXECUTION_NOT_RECOVERABLE",
            "当前节点已有有效执行结果，不能恢复等待点",
            status_code=409,
        )
    return FailedNodeRecovery(
        nodes=nodes,
        current_index=current_index,
        old_checkpoint=old_checkpoint,
        latest_execution=latest_execution,
        source_operation_no=operation.operation_no if checkpoint_missing else None,
        init_failure_code=init_failure_code if isinstance(init_failure_code, str) else None,
    )


def can_recover_failed_node(db: Session, task: AuditTask) -> bool:
    if task.deleted_at is not None or task.status != "FAILED":
        return False
    try:
        failed_node_recovery(db, task)
    except BusinessError as exc:
        if exc.status_code >= 500:
            raise
        return False
    return True
