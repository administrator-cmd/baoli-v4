"""当前执行批次的读取入口：SQL 在 AuditExecutionCRUD，本模块只做委托。"""

from typing import Any, Iterable

from sqlalchemy.orm import Session

from app.domain.audit.execution_scope import pending_rerun_keys_of
from app.modules.audit.execution.crud import AuditExecutionCRUD, json_text
from app.modules.audit.execution.model import NodeExecution
from app.modules.audit.shared import _task_manifest

__all__ = [
    "json_text",
    "current_execution_ids",
    "current_executions",
    "latest_execution",
    "current_execution_subquery",
    "pending_rerun_predicate",
    "pending_rerun_by_execution",
]


def current_execution_ids(
    db: Session, task_ids: Iterable[int], *, node_codes: Iterable[str] | None = None,
) -> set[int]:
    """每个（任务, 节点）最新一条未失效执行批次的 id。"""
    return AuditExecutionCRUD(db).current_ids(
        {int(task_id) for task_id in task_ids},
        list(node_codes) if node_codes is not None else None,
    )


def current_executions(db: Session, task) -> dict[str, NodeExecution]:
    """任务各节点的当前批次对象；节点范围取任务冻结的场景节点。"""
    allowed = {item["node_code"] for item in _task_manifest(task)["nodes"]}
    current_ids = current_execution_ids(db, [task.id], node_codes=allowed)
    return {item.node_code: item for item in AuditExecutionCRUD(db).executions_by_ids(current_ids)}


def latest_execution(
    db: Session, task_id: int, *, node_code: str | None = None,
) -> NodeExecution | None:
    """任务或指定节点最近一次未失效执行；续跑与规则上下文使用。"""
    return AuditExecutionCRUD(db).latest_for_task(task_id, node_code)


def current_execution_subquery(task_id_column: Any, node_code_column: Any):
    """相关子查询：同一任务同一节点最新未失效批次的 id。"""
    return AuditExecutionCRUD.current_execution_subquery(task_id_column, node_code_column)


def pending_rerun_predicate(rule_key_column: Any) -> Any:
    """SQL 判据：结果所属批次仍把这条规则列为待重跑。"""
    return AuditExecutionCRUD.pending_rerun_predicate(rule_key_column)


def pending_rerun_by_execution(
    db: Session, execution_ids: Iterable[int],
) -> dict[int, set[str]]:
    """各执行批次尚未完成重跑的规则键；只读 JSON 里的那一列，不整行载入诊断。"""
    rows = AuditExecutionCRUD(db).pending_rerun_rows({int(execution_id) for execution_id in execution_ids})
    return {
        int(execution_id): pending_rerun_keys_of(raw, execution_id=int(execution_id))
        for execution_id, raw in rows
    }
