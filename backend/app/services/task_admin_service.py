"""审核任务的管理列表与基本资料维护（创建/复制在 task_service）。

- 列表查询是真实后端分页：筛选（名称/场景/方案/状态/创建时间）、排序、total。
- 方案名称读取任务创建时的冻结快照。
- 删除永久移除任务及其关联数据、文件；有活跃后台操作时拒绝。
"""

import logging
import shutil
from datetime import datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.orm import Session

from ..common.pagination import PageResult, page_result
from ..common.exceptions import BusinessError
from ..entity import (
    AuditPlan, AuditReport, AuditTask, BackgroundOperation, DocumentUpload,
    Evidence, ManualReview, NodeExecution, RuleResult, ScenarioNode,
    ScenarioVersion, StoredArtifact, TaskDocument, TaskFact, TaskFieldValue,
)
from .background_service import assert_no_active_operation
from .operation_types import OBJECT_AUDIT_TASK
from .operation_log_service import record_operation
from .storage_service import resolve_storage_path

logger = logging.getLogger(__name__)


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


def update_task(db: Session, task_no: str, payload: dict[str, Any]) -> AuditTask:
    """编辑任务基本资料：名称与备注。

    执行快照（场景/方案版本、任务 schema）不可改；已完成任务需要更换输入时
    应复制为新任务，保留原任务报告。
    """
    task = db.scalar(select(AuditTask).where(AuditTask.task_no == task_no).with_for_update())
    if not task:
        raise BusinessError("AUDIT_TASK_NOT_FOUND", "审核任务不存在", status_code=404, details={"task_no": task_no})
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


def delete_task(db: Session, task_no: str) -> None:
    """永久删除任务、全部输入版本和审核数据，保留通用日志及创建/复制命令。

    文件目录先在原存储目录内改名；数据库事务失败时恢复目录。
    提交后清理文件若失败，明确报告已删除数据及尚未清理的路径。
    """
    task = db.scalar(select(AuditTask).where(AuditTask.task_no == task_no).with_for_update())
    if not task:
        raise BusinessError("AUDIT_TASK_NOT_FOUND", "审核任务不存在", status_code=404, details={"task_no": task_no})
    assert_no_active_operation(db, OBJECT_AUDIT_TASK, task.task_no)
    if task.status == "RUNNING":
        raise BusinessError("AUDIT_TASK_RUNNING", "任务正在执行，不能删除", status_code=409, details={"task_no": task_no})
    task_directory = resolve_storage_path(f"tasks/{task.task_no}")
    task_root = resolve_storage_path("tasks")
    if task_directory.parent != task_root:
        raise BusinessError("STORAGE_PATH_INVALID", "任务文件目录不安全", status_code=500)
    staged_directory = None
    try:
        if task_directory.exists():
            deletion_directory = resolve_storage_path(f"tasks/.deleting-{task.task_no}-{uuid4().hex}")
            task_directory.rename(deletion_directory)
            staged_directory = deletion_directory

        # 材料位与上传、字段与证据存在双向外键，先解除当前引用再按依赖顺序删除。
        db.execute(update(TaskDocument).where(TaskDocument.audit_task_id == task.id).values(current_upload_id=None))
        db.execute(update(TaskFieldValue).where(TaskFieldValue.audit_task_id == task.id).values(evidence_id=None))
        for entity in (
            ManualReview, Evidence, TaskFieldValue, TaskFact, AuditReport,
            RuleResult, StoredArtifact, DocumentUpload, TaskDocument, NodeExecution,
        ):
            db.execute(delete(entity).where(entity.audit_task_id == task.id))
        db.execute(delete(BackgroundOperation).where(
            BackgroundOperation.object_type == OBJECT_AUDIT_TASK,
            BackgroundOperation.object_no == task.task_no,
        ))
        # 创建/复制命令按命令类型占位，不属于任务执行记录；保留它们用于旧请求幂等识别。
        record_operation(
            db,
            object_type=OBJECT_AUDIT_TASK,
            object_no=task.task_no,
            object_name=task.task_name,
            operation_type="DELETE",
            detail={"task_no": task.task_no, "permanent": True},
        )
        db.delete(task)
        db.commit()
    except Exception:
        db.rollback()
        if staged_directory is not None:
            staged_directory.rename(task_directory)
        raise

    if staged_directory is not None:
        try:
            shutil.rmtree(staged_directory)
        except OSError as exc:
            logger.exception("任务数据已删除，但文件清理失败 task_no=%s path=%s", task_no, staged_directory)
            raise BusinessError(
                "TASK_FILE_CLEANUP_FAILED",
                "任务数据已永久删除，但文件清理失败，请根据剩余目录排查",
                status_code=500,
                details={"task_no": task_no, "task_deleted": True, "remaining_directory": str(staged_directory)},
            ) from exc
