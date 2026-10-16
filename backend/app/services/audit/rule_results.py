"""规则业务结论、CSV 取值及引用证据的持久化；事务由执行流程管理。"""

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ...domain import tool_catalog
from ...domain.manifest import comparison_label
from ...domain.rule_library import is_risk_rule
from ...entity import AuditTask, Evidence, NodeExecution, RuleResult
from .. import task_value_service
from .evidence import _copy_evidence_to_rule, save_artifact_references


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
    diagnostics = {
        "origin": "AGENT",
        "evaluation_id": evaluation_id,
        "loop_status": loop_status,
        "standard": _agent_standard_of(rule),
        "inputs": list(inputs or []),
        "unresolved_items": unresolved_items or [],
        "execution_issues": (verdict or {}).get("execution_issues") or [],
    }
    if record is not None:
        record.status = status
        record.message = message
        record.risk_level = risk_level
        record.result_json = {**(record.result_json or {}), **diagnostics}
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
            result_json=diagnostics,
        )
        db.add(record)
        db.flush()
    # 业务结论写进任务 CSV；表里只留身份、状态与诊断。
    task_value_service.append_values(
        db, task, [(task_value_service.RECORD_RULE_RESULT, record.id, payload)]
    )
    evidence_numbers = list(
        db.scalars(
            select(Evidence.evidence_no)
            .where(Evidence.audit_task_id == task.id, Evidence.rule_result_id == record.id)
            .order_by(Evidence.id)
        ).all()
    )
    if not evidence_numbers:
        source_evidence_ids = save_artifact_references(db, task, execution, record, rule, verdict)
        source_evidence_ids.extend([
            evidence_id
            for item in (inputs or [])
            if isinstance(item, dict)
            for evidence_id in (
                item.get("evidence_ids")
                or ([item["evidence_id"]] if item.get("evidence_id") else [])
            )
        ])
        if is_risk_rule(rule):
            source_evidence_ids.extend(
                evidence_id
                for source in payload.get("source_rules", [])
                for evidence_id in source.get("evidence_ids") or []
            )
        # 规则结论可以直接引用本批次 upstream_results；把被引用上游结果已有的证据
        # 复制到当前结果，页面仍沿用现有 Evidence 读取链，不新增另一套追溯结构。
        for reference in (verdict or {}).get("evidence") or []:
            source_evidence_ids.extend(reference.get("evidence_ids") or [])
            source_result_id = reference.get("rule_result_id") if isinstance(reference, dict) else None
            if source_result_id is None:
                continue
            source_evidence_ids.extend(
                int(evidence.id)
                for evidence in db.scalars(
                    select(Evidence).where(
                        Evidence.audit_task_id == task.id,
                        Evidence.rule_result_id == int(source_result_id),
                    )
                ).all()
            )
        _copy_evidence_to_rule(db, task, record, source_evidence_ids)
        evidence_numbers = list(db.scalars(select(Evidence.evidence_no).where(
            Evidence.audit_task_id == task.id, Evidence.rule_result_id == record.id).order_by(Evidence.id)))
    record.result_json = {**(record.result_json or {}), "evidence_nos": evidence_numbers}
    return record
