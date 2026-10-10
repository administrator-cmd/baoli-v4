"""LLM 计算共用的事实定位、结果与输入记录，不包含业务求值。"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from .schema import CalculationRule


@dataclass(frozen=True)
class FactValue:
    """一个可用的事实的取值与它的出处。

    `fact_id` 与 `revision_no` 一起构成「这次计算用的是哪一版事实」的证据：
    事实不可变，重跑会产生新版本，因此计算必须记录它当时依据的版本，
    否则事后无法说明「这个数是按哪一版输入算出来的」。
    """

    fact_id: int
    fact_code: str
    revision_no: int
    value: Any
    unit: str | None
    currency: str | None
    source_execution_id: int | None
    period_scope: str
    statement_scope: str
    material_key: str | None
    # 实际输入版本涉及的材料；派生事实按历史输入链追溯，不能猜测适用材料。
    source_material_keys: tuple[str, ...] = ()
    value_type: str = "DECIMAL"


@dataclass
class EvaluationResult:
    """一次计算的执行结果。

    status 取 CALC_STATUS_* 之一。只有 COMPUTED 时 value 才有意义；
    其余状态下 `reason` 说明具体缺什么，调用方据此展示，不猜。
    """

    status: str
    llm_result: dict[str, Any]
    value: Decimal | None = None
    reason: str | None = None
    # 实际参与计算的输入事实（含版本），写进 input_snapshot_json。
    inputs: list[FactValue] = field(default_factory=list)
    missing_parameters: list[str] = field(default_factory=list)


def input_snapshot(
    *,
    rule: CalculationRule,
    result: EvaluationResult,
) -> dict[str, Any]:
    """本次计算的输入事实快照：写进 task_fact.input_snapshot_json。

    它记录的是实际用到的每一版事实（fact_id + revision_no），
    因此事后可以从一次计算追溯到它依据的每一个输入版本，而不是只看最终数。
    """
    return {
        "rule_code": rule.definition.rule_code,
        "result_code": rule.definition.result_code,
        "result_type": rule.definition.result_type,
        "unit": rule.definition.unit,
        "currency": rule.definition.currency,
        "formula": rule.definition.formula,
        "calculation_type": rule.definition.calculation_type,
        "execution_mode": rule.definition.execution_mode,
        "execution_engine": rule.definition.execution_engine,
        "judgement_instruction": rule.definition.judgement_instruction,
        "period_policy": rule.definition.period_policy,
        "inputs": [
            {
                "parameter_code": _parameter_code_of(rule, item),
                "fact_id": item.fact_id,
                "fact_code": item.fact_code,
                "revision_no": item.revision_no,
                "period_scope": item.period_scope,
                "statement_scope": item.statement_scope,
                "material_key": item.material_key,
                "source_material_keys": list(item.source_material_keys),
                "unit": item.unit,
                "currency": item.currency,
                "value": format(item.value, "f") if isinstance(item.value, Decimal) else item.value,
                "value_type": item.value_type,
                "source_execution_id": item.source_execution_id,
            }
            for item in result.inputs
        ],
        "missing_parameters": list(result.missing_parameters),
        "llm_result": result.llm_result,
    }


def _parameter_code_of(rule: CalculationRule, fact: FactValue) -> str | None:
    """这条事实对应哪个参数。口径取自编译期冻结的 `parameter_scopes`。"""
    for parameter in rule.parameters:
        scope = rule.parameter_scopes.get(parameter.parameter_code) or {}
        if parameter.source_type == "FIELD" and parameter.source_code == fact.fact_code:
            if (
                str(scope.get("period_scope") or "") == fact.period_scope
                and str(scope.get("statement_scope") or "") == fact.statement_scope
            ):
                return parameter.parameter_code
        if (
            parameter.source_type == "CALC_RESULT"
            and parameter.source_code == fact.fact_code
            and str(scope.get("period_scope") or "") == fact.period_scope
            and str(scope.get("statement_scope") or "") == fact.statement_scope
        ):
            return parameter.parameter_code
    return None


def fact_key(
    *,
    source_type: str,
    source_code: str,
    period_scope: str,
    statement_scope: str,
) -> tuple[str, str, str, str]:
    """事实定位键。计算参数与事实两侧必须用同一个函数构造，避免口径不一致。"""
    return (source_type, source_code, period_scope, statement_scope)
