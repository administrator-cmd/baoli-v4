"""LLM 计算的配置编译、事实定位和结果契约。"""

from .compiler import CompileOutcome, FieldReference, compile_calculation_rules
from .facts import EvaluationResult, FactValue, fact_key, input_snapshot
from .schema import (
    CALC_STATUS_COMPUTED,
    CALC_STATUS_DIVISION_BY_ZERO,
    CALC_STATUS_INPUT_MISSING,
    CALC_STATUS_UNDECIDABLE,
    PERIOD_SCOPE_LABELS,
    CalculationDefinition,
    CalculationExecutionMode,
    CalculationParameter,
    CalculationRule,
    JudgementBand,
    assert_calculation_bands,
    assert_llm_engine,
)

__all__ = [
    "CALC_STATUS_COMPUTED", "CALC_STATUS_DIVISION_BY_ZERO", "CALC_STATUS_INPUT_MISSING",
    "CALC_STATUS_UNDECIDABLE", "PERIOD_SCOPE_LABELS", "CompileOutcome",
    "CalculationDefinition", "CalculationExecutionMode", "CalculationParameter", "CalculationRule",
    "EvaluationResult", "FactValue", "FieldReference", "JudgementBand",
    "assert_calculation_bands", "assert_llm_engine", "compile_calculation_rules",
    "fact_key", "input_snapshot",
]
