"""当前执行批次的读取入口：批次口径的 SQL 只在这里写。

任务详情、风险、报告、续跑与规则上下文都从这里取“当前批次”；判定本身在
`domain.execution_scope`，这里只按任务批量加载数据。
"""

from typing import Any, Iterable

from sqlalchemy import func, select
from sqlalchemy.orm import Session, aliased

from ...constants.status import EXECUTION_STATUS_INVALIDATED
from ...domain.execution_scope import pending_rerun_keys_of
from ...entity.tasks import NodeExecution
from .shared import _task_manifest


def json_text(column: Any, path: str) -> Any:
    """取 JSON 里的标量或数组文本；JSON_EXTRACT 会保留引号，必须再 UNQUOTE。"""
    return func.json_unquote(func.json_extract(column, path))


def current_execution_ids(
    db: Session, task_ids: Iterable[int], *, node_codes: Iterable[str] | None = None,
) -> set[int]:
    """每个（任务, 节点）最新一条未失效执行批次的 id。

    分组取最大 id 放到 SQL 里做：续跑会累积执行历史，把整批历史行读回 Python 再挑没有上限。
    """
    wanted = {int(task_id) for task_id in task_ids}
    if not wanted:
        return set()
    statement = (
        select(func.max(NodeExecution.id))
        .where(
            NodeExecution.audit_task_id.in_(wanted),
            NodeExecution.status != EXECUTION_STATUS_INVALIDATED,
        )
        .group_by(NodeExecution.audit_task_id, NodeExecution.node_code)
    )
    if node_codes is not None:
        statement = statement.where(NodeExecution.node_code.in_(list(node_codes)))
    return {int(item) for item in db.scalars(statement) if item is not None}


def current_executions(db: Session, task) -> dict[str, NodeExecution]:
    """任务各节点的当前批次对象；节点范围取任务冻结的场景节点。

    先按（任务, 节点）取最新未失效批次的主键，再只读这几条实体：
    执行历史会随续跑不断累积，整表载入再在 Python 里挑选没有上限。
    """
    allowed = {item["node_code"] for item in _task_manifest(task)["nodes"]}
    current_ids = current_execution_ids(db, [task.id], node_codes=allowed)
    if not current_ids:
        return {}
    return {
        item.node_code: item
        for item in db.scalars(select(NodeExecution).where(NodeExecution.id.in_(current_ids)))
    }


def latest_execution(
    db: Session, task_id: int, *, node_code: str | None = None,
) -> NodeExecution | None:
    """任务或指定节点最近一次未失效执行；续跑与规则上下文使用。"""
    statement = select(NodeExecution).where(
        NodeExecution.audit_task_id == task_id,
        NodeExecution.status != EXECUTION_STATUS_INVALIDATED,
    )
    if node_code is not None:
        statement = statement.where(NodeExecution.node_code == node_code)
    return db.scalar(statement.order_by(NodeExecution.id.desc()).limit(1))


def current_execution_subquery(task_id_column: Any, node_code_column: Any):
    """相关子查询：同一任务同一节点最新未失效批次的 id。

    跨任务列表无法先在 Python 里算当前批次，SQL 侧也必须用同一口径。
    """
    later = aliased(NodeExecution)
    return (
        select(func.max(later.id))
        .where(
            later.audit_task_id == task_id_column,
            later.node_code == node_code_column,
            later.status != EXECUTION_STATUS_INVALIDATED,
        )
        .correlate(NodeExecution)
        .scalar_subquery()
    )


def pending_rerun_predicate(rule_key_column: Any) -> Any:
    """SQL 判据：结果所属批次仍把这条规则列为待重跑。

    与 `domain.execution_scope.result_state` 同一口径：重跑清单里的规则写着重跑前的旧结论，
    列表分页、总数和命中数都在 SQL 里按无效处理，不能分页后再过滤。
    """
    return func.coalesce(
        func.json_contains(
            NodeExecution.result_json,
            func.json_quote(rule_key_column),
            "$.pending_rerun_keys",
        ),
        0,
    ) == 1


def pending_rerun_by_execution(
    db: Session, execution_ids: Iterable[int],
) -> dict[int, set[str]]:
    """各执行批次尚未完成重跑的规则键；只读 JSON 里的那一列，不整行载入诊断。"""
    wanted = {int(execution_id) for execution_id in execution_ids}
    if not wanted:
        return {}
    rows = db.execute(
        select(
            NodeExecution.id,
            json_text(NodeExecution.result_json, "$.pending_rerun_keys"),
        ).where(NodeExecution.id.in_(wanted))
    ).all()
    return {
        int(execution_id): pending_rerun_keys_of(raw, execution_id=int(execution_id))
        for execution_id, raw in rows
    }
