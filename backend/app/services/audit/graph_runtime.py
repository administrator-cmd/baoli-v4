"""审核图的运行时装配与等待点校验。

图恢复身份只由 `thread_id` 决定；`graph_namespace` 属于业务批次信息，用来识别
「这个等待点还属不属于当前这一次流程」——材料替换或退回节点会换新的图实例，
沿用旧 thread 会复用已经废弃的等待点。

检查点读取失败分三类，调用方据此决定可重试还是终止：

- `PENDING`：确实停在该等待点；
- `NOT_WAITING`：能读到但没有该等待点 → 业务等待点已丢失，任务必须显式失败，
  不能新建空图伪装成恢复；
- `STORAGE_UNAVAILABLE`：连接或读取异常 → 只是本次操作失败，**不能**把任务判成失败
  并丢掉等待点。"""

from ...common.exceptions import BusinessError
from ...graphs.audit_graph import CHECKPOINT_STORAGE_UNAVAILABLE, probe_interrupt
from ...entity import AuditTask, WorkflowCheckpoint
from ..task_lookup import task_or_404
from .state import _fail_unrecoverable_checkpoint, _flow_checkpoint_state, _graph_namespace, _task_checkpoint
from sqlalchemy import select, update
from sqlalchemy.orm import Session


def _expected_interrupt_node(phase: str) -> str:
    if phase == "AWAITING_CONFIRMATION":
        return "await_node_confirmation"
    if phase == "WAITING_REVIEW":
        return "await_manual_review"
    raise BusinessError("AUDIT_CHECKPOINT_INVALID", "审核流程当前不处于可恢复等待状态", status_code=409)

def _mark_graph_runtime_failed(db: Session, task_no: str) -> None:
    """图基础设施在业务分支之外失败时，落一个终态，避免任务永远停在「执行中」。"""
    db.rollback()
    task = task_or_404(db, task_no, lock=True)
    task.status = "FAILED"
    task.pending_review_hash = None
    db.execute(
        update(WorkflowCheckpoint)
        .where(WorkflowCheckpoint.audit_task_id == task.id, WorkflowCheckpoint.status == "WAITING")
        .values(status="FAILED")
    )
    db.commit()

def _ensure_audit_interrupt(db: Session, task: AuditTask, expected_phase: str) -> str:
    try:
        checkpoint = _task_checkpoint(task)
        if checkpoint.get("phase") != expected_phase:
            code = "MANUAL_REVIEW_NOT_WAITING" if expected_phase == "WAITING_REVIEW" else "AUDIT_CONFIRMATION_NOT_WAITING"
            raise BusinessError(code, "任务当前不处于对应的审核等待状态", status_code=409)
        expected_node = _expected_interrupt_node(expected_phase)
        graph_namespace = _graph_namespace(task)
        expected_state = _flow_checkpoint_state(task)
    except BusinessError as exc:
        if exc.code in {"AUDIT_CHECKPOINT_MISSING", "AUDIT_CHECKPOINT_INVALID", "REVIEW_CHECKPOINT_MISSING"}:
            _fail_unrecoverable_checkpoint(db, task, exc.message)
        raise
    persisted = db.scalar(
        select(WorkflowCheckpoint).where(
            WorkflowCheckpoint.audit_task_id == task.id,
            WorkflowCheckpoint.thread_id == task.thread_id,
            WorkflowCheckpoint.checkpoint_ns == graph_namespace,
        )
    )
    if not persisted or persisted.status != "WAITING" or persisted.state_json != expected_state:
        _fail_unrecoverable_checkpoint(db, task, "审核流程检查点不存在或与任务状态不一致")
    # 检查点存储暂时不可达是可重试的技术故障，与“等待点确实丢失”必须分开：
    # 前者不能把任务判成失败并丢掉等待点，后者才需要显式终止。
    probe = probe_interrupt(task.thread_id, expected_node)
    if probe == "STORAGE_UNAVAILABLE":
        raise BusinessError(
            CHECKPOINT_STORAGE_UNAVAILABLE,
            "审核流程检查点存储暂时不可达，本次操作未完成，请稍后重试",
            status_code=500,
        )
    if probe != "PENDING":
        _fail_unrecoverable_checkpoint(db, task, "审核流程检查点不存在或已丢失，不能重新创建后伪装为恢复")
    return graph_namespace
