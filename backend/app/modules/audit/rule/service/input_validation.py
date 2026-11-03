"""缺失输入的声明与补取校验；不执行公式或业务判断。"""

import re
from collections.abc import Callable
from decimal import Decimal
from typing import Any

from app.common.exceptions import BusinessError
from app.constants.execution import FACT_KIND_DERIVED
from app.constants.status import EXECUTABLE_FIELD_STATUSES, PRODUCED_RESULT_STATUSES
from app.domain import fact_projection
from app.domain.execution_mode import MODE_CALCULATE, MODE_CROSS_SCOPE_COMPARE, MODE_VALUE_COMPARE
from app.modules.audit.shared import _locatable_text


def _invalid(message: str, **details: Any) -> BusinessError:
    return BusinessError("AGENT_VERDICT_INVALID", message, details=details)


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
            # 比较规则可直接判断“未载明”；只有计算必须取得实际数值。
            if rule.get("execution_mode") != MODE_CALCULATE and item.get("status") != "NOT_EXECUTED":
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
        derived = {item.get("fact_code"): item for item in facts if item.get("fact_kind") == FACT_KIND_DERIVED}
        for parameter in parameters:
            if parameter.get("source_type") != "CALC_RESULT":
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
    return [{"input_id": f"input-{index}", **item} for index, item in enumerate(unique.values(), 1)]


def _validate_resolution_source(
    resolution: dict[str, Any], requirement: dict[str, Any], reference: dict[str, Any],
    facts: list[dict[str, Any]],
) -> None:
    input_id = requirement["input_id"]
    value = resolution.get("value")
    if value is None or value == "":
        raise _invalid("补取输入必须提供有效取值，不能用空值解除缺项", input_id=input_id)
    if reference.get("artifact_id") is not None:
        if (requirement["kind"] == "FIELD"
                and reference.get("material_key") != requirement.get("material_key")):
            raise _invalid("补取产物与缺失字段的声明材料不一致", input_id=input_id)
        raw_value = resolution.get("raw_value")
        quote = reference.get("quote_text")
        if (not isinstance(raw_value, str) or not _locatable_text(raw_value)
                or not isinstance(quote, str) or _locatable_text(raw_value) not in _locatable_text(quote)):
            raise _invalid("补取输入的 raw_value 必须出现在该输入的真实引文中", input_id=input_id)
        # 仅核对数字写法；单位换算属于模型的计算过程，不在补取时改写来源值。
        numeric_text = raw_value.strip().replace("，", ",")
        numeric_pattern = r"[+-]?(?:(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?"
        if (requirement.get("value_type") in {"DECIMAL", "AMOUNT", "PERCENT", "INTEGER"}
                or isinstance(value, (int, float, Decimal)) and not isinstance(value, bool)
                or isinstance(value, str) and (
                    re.fullmatch(numeric_pattern, value) or re.fullmatch(numeric_pattern, numeric_text))):
            if not re.fullmatch(numeric_pattern, numeric_text):
                raise _invalid("数值补取的 raw_value 必须填写引文中的数字原文，单位和换算写入计算依据", input_id=input_id)
            try:
                number = Decimal(str(value))
            except ArithmeticError as exc:
                raise _invalid("补取值必须与数字原文一致", input_id=input_id) from exc
            if not number.is_finite() or number != Decimal(numeric_text.replace(",", "")):
                raise _invalid("补取值与数字原文不一致；单位换算应在计算步骤中完成", input_id=input_id)
        return
    fact = next((item for item in facts if item.get("fact_id") == reference.get("fact_id")), None)
    if fact is None:
        raise _invalid("补取输入必须引用真实产物或有效字段/计算事实，不能仅引用规则结论", input_id=input_id)
    executable = (fact.get("status") == "COMPUTED" if fact.get("fact_kind") == FACT_KIND_DERIVED
                  else fact.get("status") in EXECUTABLE_FIELD_STATUSES)
    source_value = fact_projection.decode_value(fact.get("normalized_value"))
    if not executable or source_value is None:
        raise _invalid("补取输入引用的事实没有有效取值", input_id=input_id, fact_id=fact.get("fact_id"))
    if requirement["kind"] == "FIELD" and any(
        fact.get(key) != requirement.get(key) for key in ("field_key", "material_key")
    ):
        raise _invalid("补取事实与缺失字段身份不一致", input_id=input_id)
    if requirement["kind"] == "CALC_RESULT" and fact.get("fact_code") != requirement["missing_input"]:
        raise _invalid("补取事实与缺失计算结果身份不一致", input_id=input_id)
    if not same_fact_value(value, source_value, fact):
        raise _invalid("补取值与引用事实的真实取值不一致", input_id=input_id)


def validate_input_resolutions(
    raw: dict[str, Any], requirements: list[dict[str, Any]], facts: list[dict[str, Any]],
    resolve_evidence: Callable[[dict[str, Any]], list[dict[str, Any]]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """逐项核对补取值与来源，成功结论必须覆盖全部缺项。"""
    resolutions = raw.get("input_resolutions", [])
    if not isinstance(resolutions, list):
        raise _invalid("input_resolutions 必须是数组")
    by_id = {item["input_id"]: item for item in requirements}
    resolved: dict[str, dict[str, Any]] = {}
    references: list[dict[str, Any]] = []
    for item in resolutions:
        if not isinstance(item, dict) or set(item) - {"input_id", "value", "raw_value", "evidence"}:
            raise _invalid("补取输入必须包含 input_id、value 和单条 evidence，可选 raw_value")
        input_id = item.get("input_id")
        if not isinstance(input_id, str) or input_id not in by_id or input_id in resolved:
            raise _invalid("补取输入身份未知或重复", input_id=input_id)
        evidence = item.get("evidence")
        if not isinstance(evidence, dict) or evidence.get("output_code") is not None:
            raise _invalid("补取证据属于输入，必须为单条引用且不填 output_code", input_id=input_id)
        sources = resolve_evidence({"evidence": [evidence]})
        if len(sources) != 1:
            raise _invalid("补取输入必须定位到唯一来源，请补全产物或事实身份", input_id=input_id)
        reference = sources[0]
        _validate_resolution_source(item, by_id[input_id], reference, facts)
        source = {key: value for key, value in reference.items() if key != "tool_call_id"}
        resolved[input_id] = {
            "input_id": input_id, "parameter_code": by_id[input_id].get("parameter_code"),
            "missing_input": by_id[input_id].get("missing_input"),
            "value": item["value"], "raw_value": item.get("raw_value"), "evidence": source,
        }
        references.append(reference)
    unresolved = [item for item in requirements if item["input_id"] not in resolved]
    if raw.get("status") in PRODUCED_RESULT_STATUSES and unresolved:
        raise _invalid(
            "必要输入尚未逐项补齐，不能保存正式结论；请补取真实值或返回 EVIDENCE_INSUFFICIENT",
            missing_inputs=unresolved,
        )
    return list(resolved.values()), references
