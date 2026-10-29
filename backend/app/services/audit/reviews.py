"""人工复核裁决与依赖结果的重算。

复核分批（不可打乱）：存在字段确认待办时本批只包含字段事实，否则包含全部待办。判卷、提交与
裁决必须用同一集合，否则用户会先裁决一条即将被字段纠错失效的规则。材料类型待复核不参与这个
分批——它是独立事项，捆进字段批次会让两条互不相干的业务问题互相决定对方批次的哈希。

裁决后按实际事实引用使受影响的规则执行批次失效，保留人工确认值，重新执行生效规则。

失效一律保留历史：旧批次标 INVALIDATED，旧事实与旧复核仍可追溯，不原地清空。"""

from typing import Any

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from ...constants.execution import (
    FACT_KIND_DERIVED,
    FACT_KIND_EXTRACTED,
    NODE_RULE_EXECUTION,
    ORIGIN_DOCUMENT_EXTRACTION,
)
from ...constants.status import FIELD_STATUS_REVIEWED
from ...domain.manifest import enabled_extraction_field_keys
from ...domain.rule_library import active, execution_container_of
from ...entity.operations import BackgroundOperation
from ...entity.tasks import (
    AuditTask,
    ManualReview,
    NodeExecution,
    RuleResult,
    TaskFact,
    TaskFieldValue,
)
from ..system.operation_types import OBJECT_AUDIT_TASK
from .execution_history import invalidate_executions
from .shared import _current_upload_map, _task_manifest


def _decision_command_no(db: Session, task: AuditTask) -> str | None:
    """本次裁决对应的后台命令编号。

    裁决由后台操作驱动：取该任务当前 RUNNING 的操作号用于对账；取不到就写 None，
    既不编造命令号，也不把最近一次已完成的操作号当成本次命令。
    """
    operation = db.scalar(
        select(BackgroundOperation)
        .where(
            BackgroundOperation.object_type == OBJECT_AUDIT_TASK,
            BackgroundOperation.object_no == task.task_no,
            BackgroundOperation.status == "RUNNING",
        )
        .order_by(BackgroundOperation.id.desc())
    )
    return operation.operation_no if operation else None




def _invalidate_validation_results(
    db: Session,
    task: AuditTask,
    manifest: dict[str, Any],
    changed_field_keys: set[str],
) -> dict[str, Any]:
    """字段事实改变后只失效实际引用它的节点及 DAG 后继，并撤销派生事实。

    旧批次的待复核项同时失效：新结果批次是否需要人工判断由新结果自己决定，
    旧复核项不能继续挂在待办里，也不能自动贴到新结果上。
    返回真实失效数量，供纠错接口如实回报影响范围。

    普通规则按 field_key 建立引用，计算规则按字段 source_ref 建立引用；报告会展示
    任务字段，因此启用了报告规则时也是字段的直接消费者。之后只沿 DAG 向后传播。
    """
    affected_codes = _field_change_affected_node_codes(
        manifest,
        changed_field_keys,
        set(task.task_schema_json["enabled_rule_keys"]),
    )
    counts: dict[str, Any] = {
        "invalidated_executions": 0,
        "invalidated_rule_results": 0,
        "invalidated_reviews": 0,
        "affected_node_codes": sorted(affected_codes),
    }
    if not affected_codes:
        return counts
    execution_ids = list(
        db.scalars(
            select(NodeExecution.id).where(
                NodeExecution.audit_task_id == task.id,
                NodeExecution.node_code.in_(affected_codes),
                NodeExecution.status != "INVALIDATED",
            )
        ).all()
    )
    if not execution_ids:
        return counts
    counts.update(_invalidate_execution_records(db, task, execution_ids))
    return counts


def _invalidate_execution_records(
    db: Session,
    task: AuditTask,
    execution_ids: list[int],
) -> dict[str, int]:
    """失效指定执行及其规则、字段待办和该批次产生的全部当前事实。"""
    counts = {"invalidated_executions": 0, "invalidated_rule_results": 0, "invalidated_reviews": 0}
    if not execution_ids:
        return counts
    rule_result_ids = list(
        db.scalars(select(RuleResult.id).where(RuleResult.node_execution_id.in_(execution_ids))).all()
    )
    counts["invalidated_rule_results"] = len(rule_result_ids)
    field_value_ids = list(
        db.scalars(
            select(TaskFieldValue.id).where(
                TaskFieldValue.audit_task_id == task.id,
                TaskFieldValue.extraction_execution_id.in_(execution_ids),
                TaskFieldValue.is_current.is_(True),
            )
        ).all()
    )
    if field_value_ids:
        counts["invalidated_reviews"] += db.execute(
            update(ManualReview)
            .where(ManualReview.field_value_id.in_(field_value_ids), ManualReview.status == "PENDING")
            .values(status="INVALIDATED")
        ).rowcount or 0
        db.execute(
            update(TaskFieldValue)
            .where(TaskFieldValue.id.in_(field_value_ids), TaskFieldValue.is_current.is_(True))
            .values(is_current=False)
        )
    # 执行批次既可能产生计算派生事实，也可能产生抽取统一事实。只撤销 DERIVED 会让
    # 已失效抽取节点的旧字段继续留在 TaskFact 当前位，后续规则仍能读取到它。
    db.execute(
        update(TaskFact)
        .where(
            TaskFact.audit_task_id == task.id,
            TaskFact.source_execution_id.in_(execution_ids),
            TaskFact.is_current.is_(True),
        )
        .values(is_current=False)
    )
    counts["invalidated_executions"] = invalidate_executions(db, NodeExecution.id.in_(execution_ids), reason="字段纠错后重算")
    return counts


def _field_change_affected_node_codes(manifest: dict[str, Any], changed_field_keys: set[str], enabled_rule_keys: set[str]) -> set[str]:
    """字段被本任务实际生效项引用时，重新遍历规则库执行批次。"""
    if not changed_field_keys:
        return set()
    for rule in manifest.get("rules") or []:
        if rule["rule_key"] not in enabled_rule_keys or not active(rule):
            continue
        referenced = {i["field_key"] for i in rule.get("inputs") or []} | set(rule.get("outputs") or [])
        if referenced & changed_field_keys or rule.get("kind") == "REPORT":
            return {NODE_RULE_EXECUTION}
    return set()


def _next_node_for_recalculation(db: Session, task: AuditTask, manifest: dict[str, Any], nodes: list[dict[str, Any]], *, completed_node_code: str | None = None) -> dict[str, Any] | None:
    """重新执行失效的规则批次，不寻找业务阶段或人工复核节点。"""
    db.flush()
    completed = db.scalar(select(NodeExecution.id).where(NodeExecution.audit_task_id == task.id,
                          NodeExecution.node_code == NODE_RULE_EXECUTION, NodeExecution.status == "SUCCEEDED"))
    return None if completed is not None else next(item for item in nodes if item["node_code"] == NODE_RULE_EXECUTION)


def _invalidate_retry_results(
    db: Session,
    task: AuditTask,
    nodes: list[dict[str, Any]],
    current_index: int,
) -> None:
    """重试失效模型产出，保留人工确认和人工纠错的当前字段版本。"""
    manifest = _task_manifest(task)
    affected_nodes = {NODE_RULE_EXECUTION}
    execution_ids = list(
        db.scalars(
            select(NodeExecution.id).where(
                NodeExecution.audit_task_id == task.id,
                NodeExecution.node_code.in_(affected_nodes),
                NodeExecution.status != "INVALIDATED",
            )
        ).all()
    )
    if execution_ids:
        invalidate_executions(db, NodeExecution.id.in_(execution_ids), reason="用户重新执行审核")
        db.execute(
            update(TaskFact)
            .where(
                TaskFact.audit_task_id == task.id,
                TaskFact.fact_kind == FACT_KIND_DERIVED,
                TaskFact.source_execution_id.in_(execution_ids),
                TaskFact.is_current.is_(True),
            )
            .values(is_current=False)
        )

    # 节点重试已经改变了当前执行/待办集合，旧批次哈希无论重试的是哪类节点都不再有效。
    # 这一步不能放在字段抽取分支末尾，否则普通规则节点和无字段归属的历史节点会提前返回，
    # 页面继续携带旧哈希提交复核时只能得到过期批次冲突。

    # 统一容器重跑只失效方案生效规则的字段产出。
    extraction_codes = {execution_container_of(manifest)["node_code"]}
    affected_extraction_codes = extraction_codes & affected_nodes
    if not affected_extraction_codes:
        return
    # 执行契约包含保留的停用定义，不能直接作为重试取数范围。
    # 与实际抽取共用启停口径，只失效受影响节点在本任务中启用的抽取输出。
    enabled_rule_keys = set(task.task_schema_json["enabled_rule_keys"])
    affected_rule_keys = [
        rule["rule_key"] for rule in manifest["rules"]
        if rule["rule_key"] in enabled_rule_keys
        and rule["node"] in affected_extraction_codes
    ]
    affected_field_keys = enabled_extraction_field_keys(manifest, affected_rule_keys) & {
        field["field_key"] for field in task.task_schema_json["fields"]
    }
    if not affected_field_keys:
        return
    current_upload_ids = [item.id for item in _current_upload_map(db, task).values()]
    field_value_ids = (
        list(
            db.scalars(
                select(TaskFieldValue.id).where(
                    TaskFieldValue.audit_task_id == task.id,
                    TaskFieldValue.scenario_version_id == task.scenario_version_id,
                    TaskFieldValue.field_key.in_(affected_field_keys),
                    TaskFieldValue.document_upload_id.in_(current_upload_ids),
                    TaskFieldValue.is_current.is_(True),
                    TaskFieldValue.origin == ORIGIN_DOCUMENT_EXTRACTION,
                    TaskFieldValue.status != FIELD_STATUS_REVIEWED,
                )
            ).all()
        )
        if current_upload_ids
        else []
    )
    if field_value_ids:
        # 重跑会新增事实版本；历史事实与历史证据不原地清空，当前候选和依据它的待复核项
        # 必须一起退出当前位。否则重试失败时，旧候选仍会被任务视图或后续节点读到。
        db.execute(
            update(ManualReview)
            .where(ManualReview.field_value_id.in_(field_value_ids), ManualReview.status == "PENDING")
            .values(status="INVALIDATED")
        )
        db.execute(
            update(TaskFieldValue)
            .where(TaskFieldValue.id.in_(field_value_ids), TaskFieldValue.is_current.is_(True))
            .values(is_current=False)
        )
    # 抽取型统一事实没有直接保存 TaskFieldValue 外键，因此按任务冻结字段定义把
    # 本次重试节点拥有的 field_key 转成 fact_code。两张事实表要独立失效：即使某张表
    # 因历史异常缺了当前行，另一张表里的旧值也不能继续作为规则或计算输入。
    affected_fact_codes = {
        str(field.get("source_ref") or field["field_key"])
        for field in (task.task_schema_json.get("fields") or [])
        if str(field.get("field_key") or "") in affected_field_keys
    }
    if affected_fact_codes:
        db.execute(
            update(TaskFact)
            .where(
                TaskFact.audit_task_id == task.id,
                TaskFact.fact_kind == FACT_KIND_EXTRACTED,
                TaskFact.fact_code.in_(affected_fact_codes),
                TaskFact.is_current.is_(True),
                TaskFact.source_execution_id.is_not(None),
                TaskFact.status != FIELD_STATUS_REVIEWED,
            )
            .values(is_current=False)
        )
