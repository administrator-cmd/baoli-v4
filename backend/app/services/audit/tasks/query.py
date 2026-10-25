"""审核任务管理列表与选项查询。"""

from datetime import datetime
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from ....common.pagination import PageResult, page_result
from ....entity.plans import AuditPlan
from ....entity.scenarios import ScenarioNode, ScenarioVersion
from ....entity.tasks import AuditTask


def list_tasks_page(
    db: Session,
    *,
    name: str | None,
    scenario_code: str | None,
    plan_code: str | None,
    status: str | None,
    current_node: str | None,
    created_from: datetime | None,
    created_to: datetime | None,
    page: int,
    page_size: int,
) -> PageResult[dict[str, Any]]:
    """审核任务分页列表（真实后端筛选与 total）。"""
    conditions = []
    if name:
        like = f"%{name.strip()}%"
        conditions.append(or_(AuditTask.task_name.like(like), AuditTask.task_no.like(like)))
    if scenario_code:
        conditions.append(ScenarioVersion.scenario_code == scenario_code)
    if plan_code:
        conditions.append(AuditPlan.plan_code == plan_code)
    if status:
        conditions.append(AuditTask.status == status)
    if current_node:
        conditions.append(AuditTask.current_node == current_node)
    if created_from:
        conditions.append(AuditTask.created_at >= created_from)
    if created_to:
        conditions.append(AuditTask.created_at <= created_to)

    # 排序阶段只取任务 ID，避免完整规则库、方案配置等 JSON 占用 MySQL 排序缓冲区。
    page_statement = (
        select(AuditTask.id)
        .join(ScenarioVersion, ScenarioVersion.id == AuditTask.scenario_version_id)
        .outerjoin(AuditPlan, AuditPlan.id == AuditTask.plan_id)
        .where(*conditions)
    )
    total = db.scalar(
        select(func.count())
        .select_from(AuditTask)
        .join(ScenarioVersion, ScenarioVersion.id == AuditTask.scenario_version_id)
        .outerjoin(AuditPlan, AuditPlan.id == AuditTask.plan_id)
        .where(*conditions)
    ) or 0
    page_ids = list(db.scalars(
        page_statement.order_by(AuditTask.created_at.desc(), AuditTask.id.desc())
        .limit(page_size)
        .offset((page - 1) * page_size)
    ))
    # 读取本页实际展示字段，不在数据库中对这些内容再次排序。
    rows = db.execute(
        select(
            AuditTask.id,
            AuditTask.task_no,
            AuditTask.task_name,
            AuditTask.remark,
            AuditTask.current_node,
            AuditTask.status,
            AuditTask.created_at,
            AuditTask.updated_at,
            AuditTask.task_schema_json["plan_name"].label("frozen_plan_name"),
            ScenarioVersion.scenario_code,
            ScenarioVersion.scenario_name,
            ScenarioVersion.version_no.label("scenario_version_no"),
            ScenarioNode.name.label("current_node_name"),
            AuditPlan.plan_code,
        )
        .join(ScenarioVersion, ScenarioVersion.id == AuditTask.scenario_version_id)
        .outerjoin(
            ScenarioNode,
            (ScenarioNode.scenario_version_id == AuditTask.scenario_version_id)
            & (ScenarioNode.node_code == AuditTask.current_node),
        )
        .outerjoin(AuditPlan, AuditPlan.id == AuditTask.plan_id)
        .where(AuditTask.id.in_(page_ids))
    ).all()
    rows_by_id = {row.id: row for row in rows}
    items = []
    for task_id in page_ids:
        row = rows_by_id[task_id]
        frozen_plan_name = row.frozen_plan_name
        items.append(
            {
                "task_no": row.task_no,
                "task_name": row.task_name,
                "remark": row.remark,
                "scenario_code": row.scenario_code,
                "scenario_name": row.scenario_name,
                "scenario_version_no": row.scenario_version_no,
                "plan_code": row.plan_code,
                "plan_name": frozen_plan_name if isinstance(frozen_plan_name, str) and frozen_plan_name else None,
                "current_node": row.current_node,
                "current_node_name": row.current_node_name if row.current_node_name is not None else row.current_node,
                "status": row.status,
                "created_at": row.created_at,
                "updated_at": row.updated_at,
            }
        )
    return page_result(items, total, page, page_size)


def list_task_options(db: Session, *, keyword: str | None, limit: int = 50) -> list[dict[str, Any]]:
    """任务下拉选项：按名称或编号模糊匹配，最多 limit 条，不含已删除任务。"""
    conditions = []
    if keyword:
        like = f"%{keyword.strip()}%"
        conditions.append(or_(AuditTask.task_name.like(like), AuditTask.task_no.like(like)))
    rows = db.execute(
        select(AuditTask.task_no, AuditTask.task_name, AuditTask.status, ScenarioVersion.scenario_name)
        .join(ScenarioVersion, ScenarioVersion.id == AuditTask.scenario_version_id)
        .where(*conditions)
        .order_by(AuditTask.created_at.desc(), AuditTask.id.desc())
        .limit(limit)
    ).all()
    return [
        {
            "task_no": row.task_no,
            "task_name": row.task_name,
            "scenario_name": row.scenario_name,
            "status": row.status,
        }
        for row in rows
    ]
