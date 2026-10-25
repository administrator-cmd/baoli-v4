"""风险结果列表与业务结论视图组装。"""

from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, aliased

from ...common.exceptions import BusinessError
from ...entity.plans import AuditPlan
from ...entity.scenarios import ScenarioVersion
from ...entity.tasks import AuditTask, NodeExecution, RuleResult
from ..audit import value_store as task_value_service
from ..audit.value_files import lock_task_files


def list_risk_results_page(
    db: Session,
    *,
    task_no: str | None,
    scenario_code: str | None,
    plan_code: str | None,
    risk_level: str | None,
    status: str | None,
    page: int,
    page_size: int,
) -> dict[str, Any]:
    """按真实风险结果读取当前有效批次，不依赖业务节点类型。"""
    later_execution = aliased(NodeExecution)
    latest_execution = (
        select(func.max(later_execution.id))
        .where(
            later_execution.audit_task_id == NodeExecution.audit_task_id,
            later_execution.node_code == NodeExecution.node_code,
            later_execution.status != "INVALIDATED",
        )
        .correlate(NodeExecution)
        .scalar_subquery()
    )
    # 风险身份来自任务冻结规则的明确角色/算子，等级为空不表示它不是风险规则。
    # JSON_CONTAINS 将 rule_key 与角色限定在同一条规则中，避免串配其他规则的属性。
    risk_rule = or_(
        func.json_contains(
            AuditTask.manifest_json,
            func.json_object("rule_key", RuleResult.rule_key, "rule_role", "RISK"),
            "$.rules",
        ) == 1,
        func.json_contains(
            AuditTask.manifest_json,
            func.json_object("rule_key", RuleResult.rule_key, "operator", "RISK_TRIGGER"),
            "$.rules",
        ) == 1,
    )
    base_query = (
        select(RuleResult.id)
        .select_from(RuleResult)
        .join(NodeExecution, NodeExecution.id == RuleResult.node_execution_id)
        .join(AuditTask, AuditTask.id == RuleResult.audit_task_id)
        .join(ScenarioVersion, ScenarioVersion.id == AuditTask.scenario_version_id)
        .outerjoin(AuditPlan, AuditPlan.id == AuditTask.plan_id)
        .where(
            risk_rule,
            NodeExecution.id == latest_execution,
        )
    )
    risk_level_expr = RuleResult.risk_level
    conditions = []
    if task_no:
        conditions.append(AuditTask.task_no == task_no)
    if scenario_code:
        conditions.append(ScenarioVersion.scenario_code == scenario_code)
    if plan_code:
        conditions.append(AuditPlan.plan_code == plan_code)
    if status:
        conditions.append(RuleResult.status == status)
    # 等级下拉的取值范围：与当前筛选同一作用域，但不含 risk_level 条件本身
    # （否则选定一个等级后下拉里只剩它自己）。取值范围必须来自真实数据：
    # 各场景可以使用自己的风险等级名称，查询不预设等级集合。
    level_conditions = list(conditions)
    if risk_level:
        conditions.append(risk_level_expr == risk_level)
    risk_levels = sorted(
        {
            str(item)
            for item in db.scalars(
                base_query.with_only_columns(risk_level_expr).distinct()
                .where(*level_conditions)
            ).all()
            if item
        }
    )

    total = db.scalar(
        base_query.with_only_columns(func.count())
        .where(*conditions)
    ) or 0
    # 只取列表真正用到的列：scenario_version 带 200KB+ 的 manifest_json，
    # 整行 SELECT * 会让 ORDER BY 的 filesort 把整行塞进排序缓冲（MySQL 默认 256KB），
    # 少量结果就报 1038 Out of sort memory；列表页不读 manifest_json。
    rows = db.execute(
        base_query.with_only_columns(
            RuleResult.id,
            RuleResult.audit_task_id,
            RuleResult.created_at,
            AuditTask.task_no,
            AuditTask.task_name,
            ScenarioVersion.scenario_name,
            ScenarioVersion.version_no,
            AuditPlan.plan_name,
            AuditPlan.id.label("plan_id"),
        )
        .where(*conditions)
        .order_by(RuleResult.created_at.desc(), RuleResult.id.desc())
        .limit(page_size)
        .offset((page - 1) * page_size)
    ).all()
    hit_count = db.scalar(
        base_query.with_only_columns(func.count())
        .where(*conditions, RuleResult.status == "ABNORMAL")
    ) or 0
    # 上面只查询分页索引。结束旧只读快照，锁住本页任务后，再读结论索引和 CSV，
    # 避免 MySQL REPEATABLE READ 继续把旧清单与已更新的固定文件组合起来。
    db.rollback()
    for owner_no in sorted({row.task_no for row in rows}):
        lock_task_files(db, owner_no)
    results_by_id = {result.id: result for result in db.scalars(
        select(RuleResult).where(RuleResult.id.in_([row.id for row in rows]))
    )}
    # 取值按任务分组的 CSV：一次读一份文件，不按行重复读。
    task_owners: dict[int, AuditTask] = {}
    for result in results_by_id.values():
        if result.audit_task_id in task_owners:
            continue
        owner = db.get(AuditTask, result.audit_task_id)
        if owner is None:
            raise BusinessError("AUDIT_TASK_NOT_FOUND", "规则结果所属审核任务不存在", status_code=500)
        task_owners[result.audit_task_id] = owner
    items = []
    for row in rows:
        result = results_by_id[row.id]
        # 风险等级是索引列，其余业务结论（处理建议、来源规则）在任务 CSV。
        data = task_value_service.value_of(db, task_owners[result.audit_task_id], task_value_service.RECORD_RULE_RESULT, result.id)
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
