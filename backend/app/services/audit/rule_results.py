"""规则业务结论、CSV 取值及引用证据的持久化；事务由执行流程管理。"""

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ...domain import tool_catalog
from ...domain.manifest import comparison_label
from ...domain.rule_library import is_risk_rule
from ...entity.tasks import AuditTask, NodeExecution, RuleResult
from . import value_store as task_value_service
from .evidence import save_artifact_references


def _agent_standard_of(rule: dict[str, Any]) -> dict[str, Any]:
    """本条规则的审核标准：比对什么、按什么方式比、阈值与口径。

    与节点路径的 `standard` 同构，取自已发布配置（不可变），不由模型改写。
    """
    parameters = rule.get("parameters") if isinstance(rule.get("parameters"), dict) else {}
    comparison = parameters.get("operator")
    return {
        "rule_code": rule.get("rule_code") or rule.get("rule_key"),
        "operator": rule.get("operator"),
        "operator_label": tool_catalog.OPERATOR_LABELS.get(rule.get("operator"), rule.get("operator")),
        "requirement": rule.get("requirement") or None,
        "comparison": comparison,
        "comparison_label": comparison_label(rule.get("operator", ""), comparison),
        "threshold": parameters.get("threshold"),
        "threshold_unit": parameters.get("threshold_unit"),
        "check_level": rule.get("check_level"),
    }


# 诊断里的输入摘要只保留身份、状态与证据引用；业务取值以任务 CSV 为准，
# 不在数据库里重复保存一份。
_INPUT_DIAGNOSTIC_KEYS = (
    "field_key", "material_key", "value_type", "unit", "currency", "material_state",
    "period_scope", "statement_scope", "entity_scope", "origin", "status",
    "fact_id", "source_field_value_id", "source_rule_result_id",
)


def _diagnostic_inputs(inputs: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """诊断只保存身份与来源记录 id。

    业务取值以任务 CSV 为准，不在每条结果里重复保存；展示时按这些 id 读取当时的版本。
    """
    return [
        {key: item.get(key) for key in _INPUT_DIAGNOSTIC_KEYS}
        for item in (inputs or [])
        if isinstance(item, dict)
    ]


def restore_diagnostic_inputs(
    db: Session, task: AuditTask, payload: dict[str, Any]
) -> dict[str, Any]:
    """把诊断输入里的来源记录 id 还原成当时的取值。

    标准化取值按事实记录读取，原文按字段取值记录读取；两者都是不可变的历史记录，
    因此还原出来就是保存那一刻的取值。旧结果仍带着复制值，这里不改写它们。
    """
    inputs = payload.get("inputs")
    if not isinstance(inputs, list):
        return payload
    restored = []
    for item in inputs:
        if not isinstance(item, dict):
            restored.append(item)
            continue
        values = dict(item)
        fact_id = values.get("fact_id")
        if isinstance(fact_id, int) and not isinstance(fact_id, bool):
            values["normalized_value"] = task_value_service.value_of(
                db, task, task_value_service.RECORD_TASK_FACT, fact_id,
            ).get("value")
        source_id = values.get("source_field_value_id")
        if isinstance(source_id, int) and not isinstance(source_id, bool):
            values["raw_value"] = task_value_service.value_of(
                db, task, task_value_service.RECORD_TASK_FIELD_VALUE, source_id,
            ).get("raw_value")
        values.setdefault("raw_value", None)
        values.setdefault("normalized_value", None)
        restored.append(values)
    return {**payload, "inputs": restored}


def save_rule_result(
    db: Session,
    *,
    task: AuditTask,
    execution: NodeExecution,
    rule: dict[str, Any],
    verdict: dict[str, Any] | None,
    loop_status: str,
    unresolved_items: list[dict[str, Any]] | None,
    evaluation_id: str | None,
    inputs: list[dict[str, Any]] | None = None,
    output_values: list[tuple[str, int, dict[str, Any]]] | None = None,
    turn_count: int = 0,
) -> RuleResult:
    """保存本条规则的业务结论与执行诊断。

    业务结论与引用工具的比较/逐材料依据写进任务 CSV；表里只留身份、状态与诊断。
    完整工具内容只在内存里用，不落库。
    """
    record = db.scalar(
        select(RuleResult).where(
            RuleResult.node_execution_id == execution.id,
            RuleResult.rule_key == rule["rule_key"],
        )
    )
    payload = {
        "outputs": (verdict or {}).get("outputs") or [],
        "presentation_version": (verdict or {}).get("presentation_version"),
        "basis": (verdict or {}).get("basis") or [],
        "missing_items": (verdict or {}).get("missing_items") or [],
        "next_action": (verdict or {}).get("next_action") or "",
        "tool_findings": (verdict or {}).get("tool_findings") or [],
    }
    risk_level = None
    rule_role = "RISK" if is_risk_rule(rule) else "CHECK"
    if rule.get("calculation_ref"):
        payload["calculation_definition"] = rule["calculation_definition"]
        # 计算过程已经逐条保存在 basis，不再重复保存一份说明文本。
    if rule_role == "RISK":
        # 风险上下文是模型判断依据的真实上游结论，属于业务结论而不是执行诊断。
        payload.update((verdict or {}).get("risk_context") or {})
        risk_level = rule.get("risk_level_value") or (rule.get("parameters") or {}).get("risk_level")
        payload["action"] = (rule.get("parameters") or {}).get("action")
    if verdict is None:
        status = "NOT_EXECUTED"
        message = "；".join(str(item["message"]) for item in unresolved_items or [] if item.get("message")) or "规则未能形成执行结果"
    else:
        status = str(verdict.get("status") or "NOT_EXECUTED")
        message = str(verdict.get("message") or "")
    # 循环状态与轮次已单独成列，诊断 JSON 不再重复保存。
    diagnostics = {
        "evaluation_id": evaluation_id,
        "standard": _agent_standard_of(rule),
        "inputs": _diagnostic_inputs(inputs),
        "unresolved_items": unresolved_items or [],
        "execution_issues": (verdict or {}).get("execution_issues") or [],
    }
    if record is not None:
        record.status = status
        record.message = message
        record.risk_level = risk_level
        record.rule_role = rule_role
        record.loop_status = loop_status
        record.turn_count = turn_count
        # 本次保存重新声明执行来源；批量路径在保存完成后附加自己的标记。
        # 单条重跑不能继承旧批量标记，否则恢复汇总会误把独立调用去重。
        previous = {key: value for key, value in (record.result_json or {}).items()
                    if key != "extraction_batch"}
        record.result_json = {**previous, **diagnostics}
    else:
        record = RuleResult(
            audit_task_id=task.id,
            node_execution_id=execution.id,
            rule_key=rule["rule_key"],
            rule_code=rule.get("rule_code") or rule["rule_key"],
            rule_name=str(rule.get("name") or rule["rule_key"]),
            status=status,
            message=message,
            risk_level=risk_level,
            rule_role=rule_role,
            loop_status=loop_status,
            turn_count=turn_count,
            result_json=diagnostics,
        )
        db.add(record)
        db.flush()
    # 依据随业务值写 CSV。字段原文只写一次；规则引用被实际使用的字段/上游结果版本。
    output_values, payload["evidence"] = save_artifact_references(
        db, task, execution, rule, verdict, list(output_values or []),
    )
    # 所有引用核对成功后再合并产出与结论，一条规则只改写一次四表。
    task_value_service.append_values(
        db, task, [*(output_values or []), (task_value_service.RECORD_RULE_RESULT, record.id, payload)]
    )
    return record
