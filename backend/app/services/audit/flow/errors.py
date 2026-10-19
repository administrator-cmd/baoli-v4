"""审核步骤与图运行时的错误记录；保存真实失败后原样暴露。"""

import logging
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any, Iterator
from sqlalchemy import select
from sqlalchemy.orm import Session
from ....common.exceptions import BusinessError, exception_summary
from ....entity import AuditTask, NodeExecution
from ..tasks.lookup import task_or_404
from ..state import _scenario_nodes, _task_checkpoint
from .execution import running_execution


logger = logging.getLogger(__name__)


def graph_error_state(db: Session, task_no: str, node_code: str, exc: BusinessError) -> dict[str, Any]:
    task = task_or_404(db, task_no)
    progress = _task_checkpoint(task)
    _, _, current_index = _scenario_nodes(db, task)
    return {
        "executed_node": node_code,
        "current_node": task.current_node,
        "node_index": current_index,
        "phase": progress.get("phase"),
        "last_error": {
            "code": exc.code,
            "message": exc.message,
            "status_code": exc.status_code,
            "details": exc.details,
        },
    }


def raise_graph_error(state: dict[str, Any]) -> None:
    error = state.get("last_error")
    if not isinstance(error, dict):
        return
    code = error.get("code")
    message = error.get("message")
    status_code = error.get("status_code")
    details = error.get("details")
    if not isinstance(code, str) or not isinstance(message, str) or not isinstance(status_code, int) or not isinstance(details, dict):
        raise BusinessError("AUDIT_GRAPH_RESULT_INVALID", "审核流程图返回了无效错误状态", status_code=500)
    raise BusinessError(code, message, status_code=status_code, details=details)


@contextmanager
def agent_step(db: Session, task_no: str) -> Iterator[None]:
    """Agent 路径单步的失败处理：先落成显式的节点失败，再原样抛出。

    模型调用与工具执行会失败（配置缺失、供应商超时、快照过期、凭据失效），也可能抛出未预期
    异常（唯一键冲突、JSON 序列化失败、工具参数错误）。两类都必须写进执行记录并把任务置
    BLOCKED，异常摘要原样保留——不记录的话任务与执行记录都停在 RUNNING，用户既不能重跑也
    看不到原因。

    错误码的分工是刻意的：`BusinessError` 的码原样上抛（快照冲突、输入缺失、工具参数非法……）；
    其余异常写 `AUDIT_NODE_UNEXPECTED_ERROR` 并把异常类型、消息与出错位置落进
    `failure_message`，同时以同一错误码抛出。这样图包装层不会把它再包成「检查点不可达」——
    那会把快照唯一键冲突、序列化失败这类真实缺陷伪装成存储故障。

    只对「确实有一次执行正在进行」的情况落失败：任务号、执行记录与图命名空间三者都对得上才写。
    对不上说明这次调用根本不属于当前流程（例如等待点已被材料替换取代），此时不碰任何记录，
    只把原错误抛出。
    """
    try:
        yield
    except BusinessError as exc:
        fail_running_execution(db, task_no, code=exc.code, message=exc.message)
        raise
    except Exception as exc:  # noqa: BLE001 - 未预期异常必须落库后再暴露
        logger.exception(
            "规则评估发生未处理异常 task_no=%s", task_no, exc_info=exc
        )
        summary = exception_summary(exc)
        fail_running_execution(
            db,
            task_no,
            code="AUDIT_NODE_UNEXPECTED_ERROR",
            message=summary,
        )
        raise BusinessError(
            "AUDIT_NODE_UNEXPECTED_ERROR",
            "规则评估发生未处理异常，本次未完成，失败原因见执行记录",
            status_code=500,
            details={"reason": summary},
        ) from exc


def fail_running_execution(
    db: Session,
    task_no: str,
    *,
    code: str,
    message: str,
    execution_id: int | None = None,
) -> None:
    """把当前执行记录写成 FAILED、任务置 BLOCKED，并提交。

    Agent 单步和整个图的收尾共用此处。指定批次时也覆盖已提交 SUCCEEDED、
    但任务仍为 RUNNING 的推进失败。已阻断、已完成或不属于本次调用的任务不改写。
    """
    db.rollback()
    task = db.scalar(select(AuditTask).where(AuditTask.task_no == task_no).with_for_update())
    execution = (db.get(NodeExecution, execution_id) if execution_id is not None
                 else running_execution(db, task) if task is not None else None)
    if (
        task is None
        or task.status != "RUNNING"
        or execution is None
        or execution.audit_task_id != task.id
        or execution.node_code != task.current_node
        or execution.status not in {"RUNNING", "SUCCEEDED"}
    ):
        return
    execution.status = "FAILED"
    execution.finished_at = datetime.now(UTC)
    execution.failure_code = code
    execution.failure_message = message
    task.status = "BLOCKED"
    db.flush()
    db.commit()
