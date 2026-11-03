"""风险结果列表与业务结论视图组装。"""

from typing import Any

from sqlalchemy.orm import Session

from app.common.exceptions import BusinessError
from app.modules.audit.field.service import value_store as task_value_service
from app.modules.audit.field.service.value_files import AuditFieldValueFilesService
from app.modules.audit.report import crud
from app.modules.audit.rule.crud import AuditRuleCRUD
from app.modules.audit.task.crud import AuditTaskCRUD
from app.modules.audit.task.model import AuditTask


class AuditReportRisksService:
    """本实体的服务方法；db 由构造时注入。"""

    def __init__(self, db: Session | None = None) -> None:
        self.db = db

    def list_risk_results_page(self, *,
        task_no: str | None,
        task_name: str | None,
        scenario_code: str | None,
        plan_code: str | None,
        risk_level: str | None,
        status: str | None,
        page: int,
        page_size: int,
    ) -> dict[str, Any]:
        """按真实风险结果读取当前有效批次，不依赖业务节点类型。"""
        # 当前批次口径与任务详情、报告共用一份实现。
        risk_levels, total, rows, hit_count = crud.AuditReportCRUD(self.db).risk_page_rows(
            task_no=task_no,
            task_name=task_name,
            scenario_code=scenario_code,
            plan_code=plan_code,
            risk_level=risk_level,
            status=status,
            page=page,
            page_size=page_size,
        )
        # 上面只查询分页索引。结束旧只读快照，锁住本页任务后，再读结论索引和 CSV，
        # 避免 MySQL REPEATABLE READ 继续把旧清单与已更新的固定文件组合起来。
        crud.AuditReportCRUD(self.db).rollback()
        for owner_no in sorted({row.task_no for row in rows}):
            AuditFieldValueFilesService(self.db).lock_task_files( owner_no)
        results_by_id = {result.id: result for result in AuditRuleCRUD(self.db).by_ids([row.id for row in rows])}
        # 取值按任务分组的 CSV：一次读一份文件，不按行重复读。
        task_owners: dict[int, AuditTask] = {}
        for result in results_by_id.values():
            if result.audit_task_id in task_owners:
                continue
            owner = AuditTaskCRUD(self.db).get_of(AuditTask, result.audit_task_id)
            if owner is None:
                raise BusinessError("AUDIT_TASK_NOT_FOUND", "规则结果所属审核任务不存在", status_code=500)
            task_owners[result.audit_task_id] = owner
        items = []
        for row in rows:
            result = results_by_id[row.id]
            # 风险等级是索引列，其余业务结论（处理建议、来源规则）在任务 CSV。
            data = task_value_service.AuditFieldValueStoreService(self.db).value_of(task_owners[result.audit_task_id], task_value_service.RECORD_RULE_RESULT, result.id)
            items.append(
                {
                    "task_no": row.task_no,
                    "task_name": row.task_name,
                    "scenario_name": row.scenario_name,
                    "scenario_version_no": row.version_no,
                    "plan_name": row.plan_name,
                    "rule_key": result.rule_key,
                    "rule_code": result.rule_code if result.rule_code != result.rule_key else None,
                    "name": result.rule_name,
                    "risk_level": result.risk_level,
                    "status": result.status,
                    "message": result.message,
                    "suggestion": data.get("action"),
                    "source_rule_key": data.get("source_rule_key"),
                    "source_rule_name": data.get("source_rule_name"),
                    "result_id": row.id,
                    "created_at": row.created_at,
                }
            )
        return {
            "items": items,
            "total": int(total),
            "hit_count": int(hit_count),
            "risk_levels": risk_levels,
            "page": page,
            "page_size": page_size,
        }
