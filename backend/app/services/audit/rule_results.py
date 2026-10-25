"""规则业务结论、CSV 取值及引用证据的持久化；事务由执行流程管理。"""

from datetime import date, datetime
from decimal import Decimal
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
    "source_field_value_id", "source_rule_result_id",
)
# 页面按这两个键展示输入的原文与标准化结果；取值本身不是身份信息，但不能裁剪。
_INPUT_VALUE_KEYS = ("raw_value", "normalized_value")


def _json_safe(value: Any) -> Any:
    """JSON 列不接受 Decimal 与日期类型；取值按文本落库，页面照原样展示。"""
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    return value


def _diagnostic_inputs(inputs: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    return [
        {
            **{key: item.get(key) for key in _INPUT_DIAGNOSTIC_KEYS},
            **{key: _json_safe(item.get(key)) for key in _INPUT_VALUE_KEYS},
        }
        for item in (inputs or [])
        if isinstance(item, dict)
    ]


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
    if rule.get("calculation_ref"):
        payload["calculation_definition"] = rule["calculation_definition"]
        # 计算过程已经逐条保存在 basis，不再重复保存一份说明文本。
    if is_risk_rule(rule):
        # 风险上下文是模型判断依据的真实上游结论，属于业务结论而不是执行诊断。
        payload.update((verdict or {}).get("risk_context") or {})
        payload["risk_level"] = rule.get("risk_level_value") or (rule.get("parameters") or {}).get("risk_level")
        payload["action"] = (rule.get("parameters") or {}).get("action")
    if verdict is None:
        status = "NOT_EXECUTED"
        message = "；".join(str(item["message"]) for item in unresolved_items or [] if item.get("message")) or "规则未能形成执行结果"
    else:
        status = str(verdict.get("status") or "NOT_EXECUTED")
        message = str(verdict.get("message") or "")
    payload["status"] = status
    payload["message"] = message
    # 风险等级单独成列：风险结果列表要按它筛选排序，不能压在取值文件里。
    risk_level = payload.get("risk_level")
    # 循环状态与轮次已单独成列，诊断 JSON 不再重复保存。
    diagnostics = {
        "origin": "AGENT",
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
