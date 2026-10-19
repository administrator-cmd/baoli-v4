"""执行记录管理列表与详情查询。"""

from datetime import datetime
from typing import Any
from sqlalchemy import case, func, select
from sqlalchemy.orm import Session
from ...common.pagination import PageResult, page_result
from ...common.exceptions import BusinessError
from ...entity import AuditTask, BackgroundOperation, NodeExecution, ScenarioNode, ScenarioVersion
from .execution_history import execution_status_view


def list_executions_page(
    db: Session,
    *,
    task_no: str | None,
    node_code: str | None,
    status: str | None,
    started_from: datetime | None,
    started_to: datetime | None,
    page: int,
    page_size: int,
) -> PageResult[dict[str, Any]]:
    """节点执行记录分页列表（跨任务）。"""
    conditions = []
    if task_no:
        conditions.append(AuditTask.task_no == task_no)
    if node_code:
        conditions.append(NodeExecution.node_code == node_code)
    if status:
        if status == "INVALIDATED":
            conditions.append(NodeExecution.status == status)
        else:
            displayed_status = case((NodeExecution.status == "INVALIDATED", func.coalesce(
                NodeExecution.result_json["superseded_execution_status"].as_string(), NodeExecution.status,
            )), else_=NodeExecution.status)
            conditions.append(displayed_status == status)
    if started_from:
        conditions.append(NodeExecution.started_at >= started_from)
    if started_to:
        conditions.append(NodeExecution.started_at <= started_to)

    total = db.scalar(
        select(func.count())
        .select_from(NodeExecution)
        .join(AuditTask, AuditTask.id == NodeExecution.audit_task_id)
        .where(*conditions)
    ) or 0
    # 只取列表真正用到的列：scenario_version 带 200KB+ 的 manifest_json、audit_task 带
    # task_schema_json / checkpoint_json。整行 SELECT * 会让 ORDER BY 的 filesort 把整行
    # 塞进排序缓冲（MySQL 默认 256KB），少量执行记录就报 1038 Out of sort memory；
    # 列表页从不读这些大列。
    rows = db.execute(
        select(
            NodeExecution.id,
            NodeExecution.audit_task_id,
            NodeExecution.node_code,
            NodeExecution.sequence_no,
            NodeExecution.status,
            NodeExecution.started_at,
            NodeExecution.finished_at,
            NodeExecution.failure_code,
            NodeExecution.failure_message,
            NodeExecution.result_json,
            AuditTask.task_no,
            AuditTask.task_name,
            ScenarioNode.name.label("node_name"),
            ScenarioVersion.scenario_name,
            ScenarioVersion.version_no,
        )
        .join(AuditTask, AuditTask.id == NodeExecution.audit_task_id)
        .join(ScenarioVersion, ScenarioVersion.id == NodeExecution.scenario_version_id)
        .outerjoin(
            ScenarioNode,
            (ScenarioNode.scenario_version_id == NodeExecution.scenario_version_id)
            & (ScenarioNode.node_code == NodeExecution.node_code),
        )
        .where(*conditions)
        .order_by(NodeExecution.created_at.desc(), NodeExecution.id.desc())
        .limit(page_size)
        .offset((page - 1) * page_size)
    ).all()
    items = []
    for row in rows:
        duration_ms = None
        if row.started_at and row.finished_at:
            duration_ms = int((row.finished_at - row.started_at).total_seconds() * 1000)
        items.append(
            {
                "execution_id": row.id,
                "task_no": row.task_no,
                "task_name": row.task_name,
                "scenario_name": row.scenario_name,
                "scenario_version_no": row.version_no,
                "node_code": row.node_code,
                "node_name": row.node_name or row.node_code,
                "sequence_no": row.sequence_no,
                **execution_status_view(row.status, row.result_json),
                "started_at": row.started_at,
                "finished_at": row.finished_at,
                "duration_ms": duration_ms,
                "failure_code": row.failure_code,
                "failure_message": row.failure_message,
                "result_summary": row.result_json if isinstance(row.result_json, dict) else None,
            }
        )
    return page_result(items, total, page, page_size)


def execution_detail(db: Session, execution_id: int) -> dict[str, Any]:
    """节点执行详情：输入快照来源、结果、错误与关联的后台命令编号。"""
    execution = db.get(NodeExecution, execution_id)
    if not execution:
        raise BusinessError(
            "NODE_EXECUTION_NOT_FOUND",
            "执行记录不存在",
            status_code=404,
            details={"object_no": str(execution_id)},
        )
    task = db.get(AuditTask, execution.audit_task_id)
    scenario = db.get(ScenarioVersion, execution.scenario_version_id)
    node = db.scalar(
        select(ScenarioNode).where(
            ScenarioNode.scenario_version_id == execution.scenario_version_id,
            ScenarioNode.node_code == execution.node_code,
        )
    )
    related_operation = db.scalar(
        select(BackgroundOperation)
        .where(
            BackgroundOperation.object_no == (task.task_no if task else ""),
            BackgroundOperation.target_node == execution.node_code,
        )
        .order_by(BackgroundOperation.id.desc())
        .limit(1)
    )
    duration_ms = None
    if execution.started_at and execution.finished_at:
        duration_ms = int((execution.finished_at - execution.started_at).total_seconds() * 1000)
    return {
        "execution_id": execution.id,
        "task_no": task.task_no if task else None,
        "task_name": task.task_name if task else None,
        "task_status": task.status if task else None,
        "task_current_node": task.current_node if task else None,
        "input_revision": task.input_revision if task else None,
        "plan_bound": bool(task and task.plan_id) if task else None,
        "scenario_name": scenario.scenario_name if scenario else None,
        "scenario_version_no": scenario.version_no if scenario else None,
        "node_code": execution.node_code,
        "node_name": node.name if node else execution.node_code,
        "sequence_no": execution.sequence_no,
        **execution_status_view(execution.status, execution.result_json),
        "started_at": execution.started_at,
        "finished_at": execution.finished_at,
        "duration_ms": duration_ms,
        "failure_code": execution.failure_code,
        "failure_message": execution.failure_message,
        "result_summary": execution.result_json if isinstance(execution.result_json, dict) else None,
        "operation_no": related_operation.operation_no if related_operation else None,
        "created_at": execution.created_at,
    }
