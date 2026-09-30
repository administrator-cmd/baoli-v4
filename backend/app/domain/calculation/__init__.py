"""计算节点：受限表达式引擎、编译与执行。

子模块分工：

- `expression`  词法/语法分析与受限 AST。禁止 eval、禁止执行任意代码；
- `schema`      三个 Sheet（计算定义 / 计算参数 / 计算判定）的配置契约与枚举；
- `compiler`    发布期编译：解析公式、校验参数引用与单位币种、构建依赖 DAG、检查区间重叠；
- `evaluator`   运行期执行：只执行编译好的 AST，用 Decimal，显式处理除零与缺失事实；
- `judgement`   把计算结果落到判定区间上。

边界：RAG 不参与本模块的任何一步。检索只用于原文定位与证据，数值一律来自
字段抽取事实与计算结果事实。
"""

from .compiler import CompileOutcome, FieldReference, compile_calculation_rules
from .evaluator import EvaluationResult, FactValue, evaluate_rule, fact_key, input_snapshot
from .expression import (
    Binary,
    Expression,
    Identifier,
    NumberLiteral,
    Unary,
    ast_to_payload,
    identifiers,
    is_identifier,
    parse_expression,
)
from .judgement import JUDGEMENT_MATCHED, JUDGEMENT_UNMATCHED, JudgementResult, judge
from .schema import (
    CALC_STATUS_COMPUTED,
    CALC_STATUS_DIVISION_BY_ZERO,
    CALC_STATUS_INPUT_MISSING,
    CALC_STATUS_UNDECIDABLE,
    PERIOD_SCOPE_LABELS,
    CalculationDefinition,
    CalculationParameter,
    CalculationRule,
    JudgementBand,
    assert_bands_disjoint,
)

__all__ = [
    "Binary",
    "CALC_STATUS_COMPUTED",
    "CALC_STATUS_DIVISION_BY_ZERO",
    "CALC_STATUS_INPUT_MISSING",
    "CALC_STATUS_UNDECIDABLE",
    "CompileOutcome",
    "CalculationDefinition",
    "CalculationParameter",
    "CalculationRule",
    "EvaluationResult",
    "Expression",
    "FactValue",
    "FieldReference",
    "Identifier",
    "JUDGEMENT_MATCHED",
    "JUDGEMENT_UNMATCHED",
    "JudgementBand",
    "JudgementResult",
    "NumberLiteral",
    "PERIOD_SCOPE_LABELS",
    "Unary",
    "assert_bands_disjoint",
    "ast_to_payload",
    "compile_calculation_rules",
    "evaluate_rule",
    "fact_key",
    "identifiers",
    "input_snapshot",
    "is_identifier",
    "judge",
    "parse_expression",
]
