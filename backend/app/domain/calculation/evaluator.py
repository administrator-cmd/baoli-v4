"""计算规则的执行：只执行编译好的受限 AST。

运行期只读 manifest 里冻结的编译产物，不再解析原始算式、不再读 Excel。
每一次取值都要求一个**已存在的、当前有效的事实**；取不到就显式返回不可判定，
不用 0 顶替、不跳过该条计算、不把缺失当成「差值为 0 所以合格」。

数值口径
--------
- 全程 Decimal。float 的二进制误差会让「恰好等于阈值」的判定随机翻转；
- 除法使用 28 位精度（Decimal 默认上下文），**结果不做静默舍入**：
  舍入到几位是业务口径，由判定区间与展示层决定，求值层不替业务决定；
- 除零不是错误结果而是**明确的业务不可用状态**：分母为 0 时返回
  DIVISION_BY_ZERO，由调用方决定如何呈现（例如「无法计算，分母为 0」），
  绝不返回 0 或 None 让调用方误读。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, DivisionByZero, InvalidOperation, localcontext
from typing import Any

from ...errors import BusinessError
from .expression import Binary, Identifier, NumberLiteral, Unary
from .schema import (
    CALC_STATUS_COMPUTED,
    CALC_STATUS_DIVISION_BY_ZERO,
    CALC_STATUS_INPUT_MISSING,
    CALC_STATUS_UNDECIDABLE,
    CalculationRule,
    CalculationParameter,
    PeriodScope,
)


# 求值精度。28 位是 Decimal 默认上下文的值，足够容纳「亿元」量级的除法中间结果，
# 又不会因为精度不足在链式计算里累积误差。它不构成业务舍入口径：
# 结果的最终位数由展示与判定决定，求值层不做业务舍入。
CALCULATION_PRECISION = 28


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
    value: Decimal
    unit: str | None
    currency: str | None
    source_execution_id: int | None
    period_scope: str
    statement_scope: str
    material_key: str | None


@dataclass
class EvaluationResult:
    """一次计算的执行结果。

    status 取 CALC_STATUS_* 之一。只有 COMPUTED 时 value 才有意义；
    其余状态下 `reason` 说明具体缺什么，调用方据此展示，不猜。
    """

    status: str
    value: Decimal | None = None
    reason: str | None = None
    # 实际参与计算的输入事实（含版本），写进 input_snapshot_json。
    inputs: list[FactValue] = field(default_factory=list)
    missing_parameters: list[str] = field(default_factory=list)


def _parameter_map(rule: CalculationRule) -> dict[str, CalculationParameter]:
    return {item.parameter_code: item for item in rule.parameters}


def _resolve_parameter(
    *,
    rule: CalculationRule,
    parameter: CalculationParameter,
    facts: dict[tuple[str, str, str, str], FactValue],
) -> tuple[FactValue | None, str | None]:
    """解析一个参数到具体事实。

    定位口径（期间 + 报表口径）来自**编译期冻结的 `parameter_scopes`**：
    字段来源取字段的声明，上游结果来源取上游定义的结果期间与报表口径。
    参数自己不再声明这两项，因此不存在「参数写本期、字段写上期」的矛盾。

    返回 (事实, 缺失原因)。缺失原因是一句能直接展示给业务的话，
    不含技术细节，也不含「使用默认值」这类兜底说明——因为没有兜底。
    """
    scope = rule.parameter_scopes.get(parameter.parameter_code) or {}
    period_scope = str(scope.get("period_scope") or "")
    statement_scope = str(scope.get("statement_scope") or "")
    key = (
        parameter.source_type,
        parameter.source_code or "",
        period_scope,
        statement_scope,
    )
    fact = facts.get(key)
    if fact is None:
        scope_label = _scope_label(period_scope)
        if parameter.source_type == "CALC_RESULT":
            return None, (
                f"上游计算结果“{parameter.source_code}”（{scope_label}）尚不可用，"
                "本次无法计算。上游计算未执行、未判定或失败都会导致这种情况"
            )
        return None, (
            f"字段“{parameter.source_code}”（{scope_label}）没有当前有效的抽取事实，"
            "本次无法计算。请确认该字段已抽取成功，且期间口径与配置一致"
        )
    return fact, None


def _scope_label(period_scope: str) -> str:
    from .schema import PERIOD_SCOPE_LABELS

    return PERIOD_SCOPE_LABELS.get(period_scope, period_scope)


def _evaluate_node(
    node: Any,
    *,
    values: dict[str, Decimal],
) -> Decimal:
    """递归求值受限 AST。

    node 是 manifest 里冻结的 JSON 结构。这里只认四种 kind，出现别的 kind 说明
    编译产物被改写或版本不兼容——明确报错，不尝试解释。
    """
    kind = node.get("kind") if isinstance(node, dict) else None
    if kind == "NUMBER":
        return Decimal(str(node["value"]))
    if kind == "IDENTIFIER":
        name = node["name"]
        if name not in values:
            raise BusinessError(
                "CALC_EVALUATION_VALUE_MISSING",
                f"算式求值时参数“{name}”没有取值",
                status_code=500,
                details={"parameter": name},
            )
        return values[name]
    if kind == "UNARY":
        operand = _evaluate_node(node["operand"], values=values)
        if node["operator"] == "-":
            return -operand
        raise BusinessError(
            "CALC_EVALUATION_OPERATOR_UNSUPPORTED",
            f"算式里出现不支持的一元运算符“{node['operator']}”",
            status_code=500,
        )
    if kind == "BINARY":
        left = _evaluate_node(node["left"], values=values)
        right = _evaluate_node(node["right"], values=values)
        operator = node["operator"]
        if operator == "+":
            return left + right
        if operator == "-":
            return left - right
        if operator == "*":
            return left * right
        if operator == "/":
            if right == 0:
                raise _DivisionByZero()
            return left / right
        raise BusinessError(
            "CALC_EVALUATION_OPERATOR_UNSUPPORTED",
            f"算式里出现不支持的运算符“{operator}”",
            status_code=500,
        )
    raise BusinessError(
        "CALC_EVALUATION_NODE_UNKNOWN",
        f"算式的编译产物包含未知节点类型“{kind}”，无法执行",
        status_code=500,
        details={"kind": kind},
    )


class _DivisionByZero(Exception):
    """除零的内部信号。

    它不是「计算错误」而是「业务不可用」：分母为 0 时这个指标本身没有定义，
    调用方要把它呈现成明确状态，而不是把异常当成系统故障。
    """


def evaluate_rule(
    *,
    rule: CalculationRule,
    facts: dict[tuple[str, str, str, str], FactValue],
) -> EvaluationResult:
    """执行一条计算规则。

    `facts` 的键是 (source_type, source_code, period_scope, statement_scope)：
    计算参数按这四个维度定位事实，因此同一个字段在「本期」与「上期」是两个不同的键，
    不会互相覆盖。上游计算结果的键用 source_type=CALC_RESULT + result_code。
    """
    parameters = _parameter_map(rule)
    values: dict[str, Decimal] = {}
    inputs: list[FactValue] = []
    missing: list[str] = []
    primary_reason: str | None = None
    for code, parameter in parameters.items():
        if parameter.source_type == "CONSTANT":
            from .compiler import constant_decimal

            values[code] = constant_decimal(parameter)
            continue
        fact, reason = _resolve_parameter(rule=rule, parameter=parameter, facts=facts)
        if fact is None:
            # 参数缺事实时**一律**不可计算：编译期要求公式里的每个标识符都必须有参数声明，
            # 因此 AST 一定引用它，缺任何一个都算不出结果。
            # `required=False` 只表示「这条输入不是业务必填」，**不表示**「缺了也能算」：
            # 按必填与否分流会让非必填参数缺失时继续走到 AST 求值，抛出一个 500 级错误，
            # 而契约承诺的是「明确的业务不可用状态」。这里统一返回 INPUT_MISSING，
            # 并在原因里点明该参数是非必填。
            missing.append(code)
            if len(missing) == 1:
                # 只保留第一条缺失原因作为主因，避免把同一件事说三遍；
                # 完整缺失参数集合在 missing_parameters 里。
                primary_reason = (
                    reason
                    if parameter.required
                    else f"{reason}（该参数声明为非必填，但公式引用了它，缺少时同样无法计算）"
                )
            continue
        values[code] = fact.value
        inputs.append(fact)

    if missing:
        return EvaluationResult(
            status=CALC_STATUS_INPUT_MISSING,
            reason=primary_reason,
            inputs=inputs,
            missing_parameters=missing,
        )

    try:
        with localcontext() as context:
            context.prec = CALCULATION_PRECISION
            value = _evaluate_node(rule.ast, values=values)
    except _DivisionByZero:
        return EvaluationResult(
            status=CALC_STATUS_DIVISION_BY_ZERO,
            reason=(
                f"计算规则“{rule.definition.rule_name}”（{rule.definition.rule_code}）的算式"
                f"“{rule.definition.formula}”出现除数为 0，该指标本次没有定义，无法判定"
            ),
            inputs=inputs,
        )
    except InvalidOperation as exc:
        return EvaluationResult(
            status=CALC_STATUS_UNDECIDABLE,
            reason=(
                f"计算规则“{rule.definition.rule_name}”（{rule.definition.rule_code}）的算式"
                f"“{rule.definition.formula}”无法完成数值计算：{exc}"
            ),
            inputs=inputs,
        )
    except (DivisionByZero, ZeroDivisionError):
        return EvaluationResult(
            status=CALC_STATUS_DIVISION_BY_ZERO,
            reason=(
                f"计算规则“{rule.definition.rule_name}”（{rule.definition.rule_code}）的算式"
                f"“{rule.definition.formula}”出现除数为 0，该指标本次没有定义，无法判定"
            ),
            inputs=inputs,
        )
    return EvaluationResult(status=CALC_STATUS_COMPUTED, value=value, inputs=inputs)


def input_snapshot(
    *,
    rule: CalculationRule,
    result: EvaluationResult,
) -> dict[str, Any]:
    """本次计算的输入事实快照：写进 task_fact.input_snapshot_json。

    它记录的是**实际用到的每一版事实**（fact_id + revision_no），
    因此事后可以从一次计算追溯到它依据的每一个输入版本，而不是只看最终数。
    """
    return {
        "rule_code": rule.definition.rule_code,
        "result_code": rule.definition.result_code,
        "expression_hash": rule.expression_hash,
        "formula": rule.definition.formula,
        "calculation_type": rule.definition.calculation_type,
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
                "unit": item.unit,
                "currency": item.currency,
                "value": format(item.value, "f"),
                "source_execution_id": item.source_execution_id,
            }
            for item in result.inputs
        ],
        "missing_parameters": list(result.missing_parameters),
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


def period_scopes_of(rule: CalculationRule) -> set[str]:
    """这条计算规则用到的全部期间口径（由来源自身声明，编译期冻结）。"""
    return {
        str(scope.get("period_scope"))
        for scope in rule.parameter_scopes.values()
        if scope.get("period_scope")
    }


def statement_scopes_of(rule: CalculationRule) -> set[str]:
    """这条计算规则用到的全部报表口径（合并 / 母公司）。"""
    return {
        str(scope.get("statement_scope"))
        for scope in rule.parameter_scopes.values()
        if scope.get("statement_scope")
    }


def required_field_codes(rule: CalculationRule) -> set[str]:
    """这条计算规则需要的全部字段事实编号（不含上游计算结果）。"""
    return {
        item.source_code
        for item in rule.parameters
        if item.source_type == "FIELD" and item.source_code
    }


def period_scope_label(scope: str) -> str:
    """期间口径的中文名（本期 / 上期 / 期初 / 期末 / 期初期末平均）。"""
    return _scope_label(scope)
