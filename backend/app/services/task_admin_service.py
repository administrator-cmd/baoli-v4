"""审核任务的管理列表与基本资料维护（创建/复制在 task_service）。

- 列表查询是真实后端分页：筛选（名称/场景/方案/状态/创建时间）、排序、total。
- 方案列显示“方案名 第N版”；旧任务没有方案关联时返回 is_legacy_config=True，
  页面显示“历史任务配置”，不伪造关联、不改写历史结果。
- 删除是逻辑删除：历史事实、文件、报告与证据保留；有活跃后台操作时拒绝。
"""

import logging
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from ..common.exceptions import BusinessError
from ..entities import AuditPlan, AuditTask, ScenarioNode, ScenarioVersion
from .background_service import assert_no_active_operation
from .operation_types import OBJECT_AUDIT_TASK
from .operation_log_service import record_operation

logger = logging.getLogger(__name__)


def list_tasks_page(
    db: Session,
    *,
    name: str | None,
    scenario_code: str | None,
    plan_code: str | None,
    legacy: bool | None,
    status: str | None,
    current_node: str | None,
    created_from: datetime | None,
    created_to: datetime | None,
    deleted: bool,
    page: int,
    page_size: int,
) -> dict[str, Any]:
    """审核任务分页列表（真实后端筛选与 total）。"""
    conditions = [AuditTask.deleted_at.is_not(None) if deleted else AuditTask.deleted_at.is_(None)]
    if name:
        like = f"%{name.strip()}%"
        conditions.append(or_(AuditTask.task_name.like(like), AuditTask.task_no.like(like)))
    if scenario_code:
        conditions.append(ScenarioVersion.scenario_code == scenario_code)
    if plan_code:
        conditions.append(AuditPlan.plan_code == plan_code)
    # legacy 是 bool | None：False（“仅方案任务”）与 None（不过滤）必须区分，
    # 写成 `if legacy:` 会让 False 与 None 同路、反条件筛选永远不生效。
    if legacy is not None:
        conditions.append(
            AuditTask.plan_id.is_(None) if legacy else AuditTask.plan_id.is_not(None)
        )
    if status:
        conditions.append(AuditTask.status == status)
    if current_node:
        conditions.append(AuditTask.current_node == current_node)
    if created_from:
        conditions.append(AuditTask.created_at >= created_from)
    if created_to:
        conditions.append(AuditTask.created_at <= created_to)

    base_statement = (
        select(AuditTask, ScenarioVersion, ScenarioNode, AuditPlan)
        .join(ScenarioVersion, ScenarioVersion.id == AuditTask.scenario_version_id)
        .outerjoin(
            ScenarioNode,
            (ScenarioNode.scenario_version_id == AuditTask.scenario_version_id)
            & (ScenarioNode.node_code == AuditTask.current_node),
        )
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
    rows = db.execute(
        base_statement.order_by(AuditTask.created_at.desc(), AuditTask.id.desc())
        .limit(page_size)
        .offset((page - 1) * page_size)
    ).all()
    items = []
    for task, scenario, node, plan in rows:
        schema = task.task_schema_json if isinstance(task.task_schema_json, dict) else {}
        frozen_plan_name = schema.get("plan_name")
        items.append(
            {
                "task_no": task.task_no,
                "task_name": task.task_name,
                "remark": task.remark,
                "scenario_code": scenario.scenario_code,
                "scenario_name": scenario.scenario_name,
                "scenario_version_no": scenario.version_no,
                "plan_code": plan.plan_code if plan else None,
                "plan_name": frozen_plan_name if isinstance(frozen_plan_name, str) and frozen_plan_name else None,
                "is_legacy_config": plan is None,
                "current_node": task.current_node,
                "current_node_name": node.name if node else task.current_node,
                "status": task.status,
                "deleted": task.deleted_at is not None,
                "created_at": task.created_at,
                "updated_at": task.updated_at,
            }
        )
    return {"items": items, "total": int(total), "page": page, "page_size": page_size}


def list_task_options(db: Session, *, keyword: str | None, limit: int = 50) -> list[dict[str, Any]]:
    """任务下拉选项：按名称或编号模糊匹配，最多 limit 条，不含已删除任务。"""
    conditions = [AuditTask.deleted_at.is_(None)]
    if keyword:
        like = f"%{keyword.strip()}%"
        conditions.append(or_(AuditTask.task_name.like(like), AuditTask.task_no.like(like)))
    rows = db.execute(
        select(AuditTask, ScenarioVersion)
        .join(ScenarioVersion, ScenarioVersion.id == AuditTask.scenario_version_id)
        .where(*conditions)
        .order_by(AuditTask.created_at.desc())
        .limit(limit)
    ).all()
    return [
        {
            "task_no": task.task_no,
            "task_name": task.task_name,
            "scenario_name": scenario.scenario_name,
            "status": task.status,
        }
        for task, scenario in rows
    ]


def update_task(db: Session, task_no: str, payload: dict[str, Any]) -> AuditTask:
    """编辑任务基本资料：名称与备注。

    执行快照（场景/方案版本、任务 schema）不可改；已完成任务需要更换输入时
    应复制为新任务，保留原任务报告。
    """
    task = db.scalar(select(AuditTask).where(AuditTask.task_no == task_no).with_for_update())
    if not task:
        raise BusinessError("AUDIT_TASK_NOT_FOUND", "审核任务不存在", status_code=404, details={"task_no": task_no})
    if task.deleted_at is not None:
        raise BusinessError("AUDIT_TASK_DELETED", "任务已被删除，请先恢复后再编辑", status_code=409, details={"task_no": task_no})
    if "task_name" in payload and payload["task_name"] is not None:
        name = str(payload["task_name"]).strip()
        if not name:
            raise BusinessError("TASK_NAME_REQUIRED", "任务名称不能为空", status_code=400)
        task.task_name = name
    if "remark" in payload:
        remark = payload.get("remark")
        task.remark = (str(remark).strip() or None) if remark is not None else None
    record_operation(
        db,
        object_type=OBJECT_AUDIT_TASK,
        object_no=task.task_no,
        object_name=task.task_name,
        operation_type="UPDATE",
        detail={"task_no": task.task_no},
    )
    db.commit()
    db.refresh(task)
    return task


def delete_task(db: Session, task_no: str) -> AuditTask:
    """逻辑删除：保留历史事实、文件、证据与报告；执行中的任务拒绝删除。"""
    task = db.scalar(select(AuditTask).where(AuditTask.task_no == task_no).with_for_update())
    if not task:
        raise BusinessError("AUDIT_TASK_NOT_FOUND", "审核任务不存在", status_code=404, details={"task_no": task_no})
    if task.deleted_at is not None:
        raise BusinessError("AUDIT_TASK_ALREADY_DELETED", "任务已在已删除列表中", status_code=409, details={"task_no": task_no})
    assert_no_active_operation(db, OBJECT_AUDIT_TASK, task.task_no)
    if task.status == "RUNNING":
        raise BusinessError("AUDIT_TASK_RUNNING", "任务正在执行，不能删除", status_code=409, details={"task_no": task_no})
    task.deleted_at = datetime.now(UTC)
    record_operation(
        db,
        object_type=OBJECT_AUDIT_TASK,
        object_no=task.task_no,
        object_name=task.task_name,
        operation_type="DELETE",
        detail={"task_no": task.task_no},
    )
    db.commit()
    db.refresh(task)
    return task


def restore_task(db: Session, task_no: str) -> AuditTask:
    """恢复已逻辑删除的任务；不在已删除列表时明确 409。"""
    task = db.scalar(
        select(AuditTask).where(AuditTask.task_no == task_no, AuditTask.deleted_at.is_not(None)).with_for_update()
    )
    if not task:
        raise BusinessError("AUDIT_TASK_NOT_DELETED", "任务不在已删除列表中，无需恢复", status_code=409, details={"task_no": task_no})
    task.deleted_at = None
    record_operation(
        db,
        object_type=OBJECT_AUDIT_TASK,
        object_no=task.task_no,
        object_name=task.task_name,
        operation_type="RESTORE",
        detail={"task_no": task.task_no},
    )
    db.commit()
    db.refresh(task)
    return task
