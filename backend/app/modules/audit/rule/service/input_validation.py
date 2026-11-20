"""已声明的取值缺项与事实比较；不执行公式或业务判断。"""

from decimal import Decimal
from typing import Any

from app.constants.execution import FACT_KIND_DERIVED
from app.constants.status import EXECUTABLE_FIELD_STATUSES, FIELD_STATUS_NOT_PRESENT
from app.domain.audit import fact_projection
from app.domain.configuration.execution_mode import MODE_CALCULATE, MODE_CROSS_SCOPE_COMPARE, MODE_VALUE_COMPARE


def same_fact_value(left: Any, right: Any, definition: dict[str, Any]) -> bool:
    """按声明类型比较取值；十进制不经过浮点数。"""
    left = fact_projection.canonical_fact_value(left, definition)
    right = fact_projection.canonical_fact_value(right, definition)
    if definition.get("value_type") in {"DECIMAL", "AMOUNT", "PERCENT", "INTEGER"}:
        return left is not None and right is not None and Decimal(left) == Decimal(right)
    return left == right


def missing_rule_inputs(
    rule: dict[str, Any], inputs: list[dict[str, Any]], facts: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """上下文与保存校验共用同一份缺项身份。"""
    calculation = rule.get("calculation_definition") or {}
    parameters = calculation.get("parameters") or []
    by_field: dict[tuple[str | None, str | None], list[dict[str, Any]]] = {}
    for parameter in parameters:
        if parameter.get("source_type") == "FIELD":
            by_field.setdefault((parameter.get("resolved_field_key"), parameter.get("resolved_material_key")), []).append(parameter)
    missing: list[dict[str, Any]] = []
    if rule.get("execution_mode") in {MODE_CALCULATE, MODE_VALUE_COMPARE, MODE_CROSS_SCOPE_COMPARE}:
        for item in inputs:
            # “未载明”是可供存在性检查使用的业务事实；计算仍需要实际数值。
            if rule.get("execution_mode") != MODE_CALCULATE and item.get("status") == FIELD_STATUS_NOT_PRESENT:
                continue
            if item.get("status") in EXECUTABLE_FIELD_STATUSES and item.get("normalized_value") is not None:
                continue
            bound_parameters = by_field.get((item.get("field_key"), item.get("material_key")), [{}])
            for parameter in bound_parameters:
                missing.append({
                    "kind": "FIELD", "parameter_code": parameter.get("parameter_code"),
                    "field_key": item.get("field_key"), "material_key": item.get("material_key"),
                    "missing_input": item.get("field_name") or item.get("field_key"),
                    "value_type": item.get("value_type"),
                    "status": item.get("status") or "NOT_EXECUTED",
                })
    if rule.get("execution_mode") == MODE_CALCULATE:
        input_pairs = {(item.get("field_key"), item.get("material_key")) for item in inputs}
        derived = {item.get("fact_code"): item for item in facts if item.get("fact_kind") == FACT_KIND_DERIVED}
        for parameter in parameters:
            source_type = parameter.get("source_type")
            if source_type == "FIELD" and (parameter.get("resolved_field_key"), parameter.get("resolved_material_key")) not in input_pairs:
                missing.append({
                    "kind": "FIELD", "parameter_code": parameter.get("parameter_code"),
                    "field_key": parameter.get("resolved_field_key"), "material_key": parameter.get("resolved_material_key"),
                    "missing_input": parameter.get("source_code"), "status": "NOT_EXECUTED",
                })
            if source_type != "CALC_RESULT":
                continue
            code = parameter.get("source_code")
            fact = derived.get(code, {})
            if fact.get("status") == "COMPUTED" and fact.get("normalized_value") is not None:
                continue
            missing.append({
                "kind": "CALC_RESULT", "parameter_code": parameter.get("parameter_code"),
                "missing_input": code, "status": fact.get("status") or "NOT_EXECUTED",
            })
    for gap in [*(calculation.get("input_gaps") or []), *(rule.get("input_gaps") or [])]:
        missing.append({
            "kind": "DECLARED_GAP", "parameter_code": gap.get("parameter_code"),
            "missing_input": gap.get("missing_input"), "reason": gap.get("reason"),
            "status": "NOT_BOUND",
        })
    unique: dict[tuple[Any, ...], dict[str, Any]] = {}
    for item in missing:
        parameter_code = item.get("parameter_code")
        identity = ("PARAMETER", parameter_code) if parameter_code else tuple(item.get(key) for key in (
            "kind", "parameter_code", "field_key", "material_key", "missing_input", "reason",
        ))
        existing = unique.get(identity)
        if existing is None:
            unique[identity] = item
        elif item.get("reason"):
            reasons = existing.setdefault("binding_reasons", [])
            if item["reason"] not in reasons:
                reasons.append(item["reason"])
    return list(unique.values())
