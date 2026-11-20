"""审核任务管理列表与选项查询：SQL 在 crud。"""

from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from app.common.pagination import PageResult, page_result
from app.modules.audit.task import crud


class AuditTaskQueryService:
    """本实体的服务方法；db 由构造时注入。"""

    def __init__(self, db: Session | None = None) -> None:
        self.db = db

    def list_tasks_page(
        self,
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
        page_ids, rows_by_id, total = crud.AuditTaskCRUD(self.db).page_rows(
            name=name,
            scenario_code=scenario_code,
            plan_code=plan_code,
            status=status,
            current_node=current_node,
            created_from=created_from,
            created_to=created_to,
            page=page,
            page_size=page_size,
        )
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
                    "block_reason": row.block_reason if row.status in {"BLOCKED", "FAILED"} else None,
                    "created_at": row.created_at,
                    "updated_at": row.updated_at,
                }
            )
        return page_result(items, total, page, page_size)

    def list_task_options(self, *, keyword: str | None, limit: int = 50) -> list[dict[str, Any]]:
        """任务下拉选项：按名称或编号模糊匹配，最多 limit 条，不含已删除任务。"""
        return [
            {
                "task_no": row.task_no,
                "task_name": row.task_name,
                "scenario_name": row.scenario_name,
                "status": row.status,
            }
            for row in crud.AuditTaskCRUD(self.db).option_rows(keyword=keyword, limit=limit)
        ]
