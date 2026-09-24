"""计算规则的编译：把三个 Sheet 的配置确定性编译成可执行的冻结结构。

编译发生在**发布期**，运行期只执行编译产物：

- 公式在发布期解析成受限 AST（见 expression.py），运行期不再看原始文本；
- 参数引用在发布期解析成确定的取值来源（字段事实 / 上游计算结果 / 常量）；
- 单位与币种在发布期核对，运行期不做任何换算；
- 计算之间的依赖在发布期构成 DAG 并检查循环；
- 判定区间在发布期检查重叠。

编译期**不做**的事：不猜期间口径、不代填单位币种、不把未声明的标识符当成字段名。
任何一个环节缺依据就抛 BusinessError 或产出 PENDING 待确认事项，绝不静默补默认值。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from ...errors import BusinessError
from .expression import (
    Binary,
    Expression,
    Identifier,
    NumberLiteral,
    Unary,
    ast_to_payload,
    identifiers,
    parse_expression,
)
from .schema import (
    CalculationDefinition,
    CalculationParameter,
    CalculationRule,
    JudgementBand,
    assert_bands_disjoint,
)


# 允许出现在算式里的期间前缀。它们不是参数名的一部分，而是**期间声明**。
# 之所以要在这里显式列出而不是靠字符串切分：切分规则一旦宽松，
# 「本期营业收入」与「营业收入本期」都会被切出同一个名字，而后者可能只是某个
# 真实存在的科目名。列不出来就报错，让人工确认。
PERIOD_PREFIXES: tuple[tuple[str, str], ...] = (
    ("本期", "CURRENT"),
    ("上期", "PRIOR"),
    ("期初", "OPENING"),
    ("期末", "CLOSING"),
    ("年初", "OPENING"),
    ("年末", "CLOSING"),
)

# 单位换算表**刻意留空**：系统不做单位换算。
# 这不是遗漏——换算需要业务确认口径（万元与元、千元与元），
# 代码自行按 10000 乘会让「币种未声明时不做数值比较」这条既有约定失效。
# 单位不一致时编译期直接报错，由业务在配置里把单位写一致。


@dataclass(frozen=True)
class FieldReference:
    """编译期可见的一个字段抽取目标。

    它来自 03_财报科目抽取规则库（或等价的字段配置），是计算参数的 FIELD 来源。
    """

    field_code: str
    field_key: str
    name: str
    value_type: str
    unit: str | None
    currency: str | None
    material_key: str
    material_name: str
    # 取数期间与报表口径：**由字段自己声明**，编译期把它们解析成参数的定位口径。
    # 参数表不再声明这两项，因此这里是唯一的来源；不给默认值——
    # 默认成「本期 / 合并」会让一个声明了上期的参数静默取到本期的数。
    period_scope: str
    statement_scope: str


@dataclass
class CompileOutcome:
    """编译结果与必须留给业务处理的待确认事项。"""

    rules: list[CalculationRule]
    # 待确认事项：它们有明确的业务对象与属性，可以经既有裁决入口作答。
    # 非停用的 PENDING 计算规则必须在这里留下可作答事项，否则会静默进已发布配置。
    pending: list[dict[str, Any]] = field(default_factory=list)


def _location(rule_code: str, column: str) -> str:
    return f"计算规则“{rule_code}”的「{column}」"


def _expression_hash(payload: dict[str, Any]) -> str:
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _collect_references(node: Expression) -> list[Identifier]:
    result: list[Identifier] = []

    def visit(item: Expression) -> None:
        if isinstance(item, Identifier):
            result.append(item)
        elif isinstance(item, Unary):
            visit(item.operand)
        elif isinstance(item, Binary):
            visit(item.left)
            visit(item.right)

    visit(node)
    return result


def _validate_parameter_references(
    *,
    definition: CalculationDefinition,
    ast: Expression,
    parameters_by_rule: dict[str, list[CalculationParameter]],
) -> None:
    """公式里的每个标识符都必须对应一个已声明的 parameter_code。

    不做「按名称模糊匹配」：`流动资产合计` 与 `流动资产` 是两个不同的引用，
    模糊匹配会把它们当成同一个，而结果看起来完全正常。解析不出来就报错。
    """
    used = identifiers(ast)
    declared = {item.parameter_code for item in parameters_by_rule.get(definition.rule_code, [])}
    unknown = [name for name in used if name not in declared]
    if unknown:
        raise BusinessError(
            "CALC_PARAMETER_UNDECLARED",
            f"计算规则“{definition.rule_code}”的公式引用了未声明的参数："
            + "、".join(f"“{name}”" for name in unknown)
            + "。公式里的每个标识符都必须在「计算参数」中声明；系统不按名称猜参数",
            details={
                "rule_code": definition.rule_code,
                "formula": definition.formula,
                "undeclared": unknown,
                "declared": sorted(declared),
            },
        )
    unused = sorted(declared - set(used))
    if unused:
        raise BusinessError(
            "CALC_PARAMETER_UNUSED",
            f"计算规则“{definition.rule_code}”声明了公式里没有用到的参数："
            + "、".join(f"“{name}”" for name in unused)
            + "。多余的声明会让参数表与实际计算不一致，请删除或补进公式",
            details={"rule_code": definition.rule_code, "unused": unused},
        )


def _resolved_scope_of(
    *,
    parameter: CalculationParameter,
    field_catalog: dict[str, FieldReference],
    definitions_by_code: dict[str, "CalculationDefinition"],
) -> dict[str, str]:
    """一个参数**解析后**的定位口径（期间 + 报表口径）。

    - `FIELD`：取字段自己声明的取数期间与报表口径（03_字段）；
    - `CALC_RESULT`：取上游计算定义的结果期间与报表口径（06_计算定义）；
    - `CONSTANT`：常量没有期间，返回空字典。

    引用不存在时由 `_validate_source_reference` 更早报错，因此这里取不到即内部矛盾。
    """
    if parameter.source_type == "FIELD":
        field = field_catalog.get(parameter.source_code or "")
        if field is None:
            return {}
        return {"period_scope": field.period_scope, "statement_scope": field.statement_scope}
    if parameter.source_type == "CALC_RESULT":
        owner_code = None
        for definition in definitions_by_code.values():
            if definition.result_code == parameter.source_code:
                owner_code = definition.rule_code
                break
        definition = definitions_by_code.get(owner_code or "")
        if definition is None:
            return {}
        return {
            "period_scope": definition.period_policy,
            "statement_scope": definition.statement_scope,
        }
    return {}


def _validate_source_reference(
    *,
    definition: CalculationDefinition,
    parameter: CalculationParameter,
    field_catalog: dict[str, FieldReference],
    result_owner: dict[str, str],
) -> None:
    """参数引用的目标必须真实存在，且单位币种口径与声明一致。"""
    where = f"计算规则“{definition.rule_code}”的参数“{parameter.parameter_code}”"
    if parameter.source_type == "FIELD":
        field = field_catalog.get(parameter.source_code or "")
        if field is None:
            raise BusinessError(
                "CALC_PARAMETER_FIELD_MISSING",
                f"{where}引用的字段编号“{parameter.source_code}”在字段抽取配置里不存在",
                details={
                    "rule_code": definition.rule_code,
                    "parameter_code": parameter.parameter_code,
                    "source_code": parameter.source_code,
                },
            )
        if field.value_type not in {"DECIMAL"}:
            # 计算只能取金额类字段。文本、日期、条款、大写金额都不参与算术：
            # 大写金额需要业务先确认换算口径（与 DECIMAL_CN_EQUAL 同一原因）。
            raise BusinessError(
                "CALC_PARAMETER_FIELD_TYPE_INVALID",
                f"{where}引用的字段“{field.name}”类型是 {field.value_type}，"
                "只有金额（DECIMAL）字段可以参与计算。"
                + (
                    "大写金额需要业务先确认换算口径，系统不自行换算"
                    if field.value_type == "DECIMAL_CN"
                    else ""
                ),
                details={
                    "rule_code": definition.rule_code,
                    "parameter_code": parameter.parameter_code,
                    "field_code": field.field_code,
                    "value_type": field.value_type,
                },
            )
        _assert_unit_currency_declared(
            definition=definition,
            parameter=parameter,
            upstream_unit=field.unit,
            upstream_currency=field.currency,
            upstream_label=f"字段“{field.name}”",
        )
        return
    if parameter.source_type == "CALC_RESULT":
        owner = result_owner.get(parameter.source_code or "")
        if owner is None:
            raise BusinessError(
                "CALC_PARAMETER_RESULT_MISSING",
                f"{where}引用的计算结果编号“{parameter.source_code}”在当前场景里不存在",
                details={
                    "rule_code": definition.rule_code,
                    "parameter_code": parameter.parameter_code,
                    "source_code": parameter.source_code,
                },
            )
        if owner == definition.rule_code:
            raise BusinessError(
                "CALC_PARAMETER_SELF_REFERENCE",
                f"{where}引用了本规则自己的计算结果，这是自引用，无法计算",
                details={"rule_code": definition.rule_code, "result_code": parameter.source_code},
            )
        return
    # CONSTANT：数值合法性已在 schema 层校验；单位币种必须显式声明，
    # 否则与字段相加时无法判断口径是否一致。
    if not (parameter.unit or "").strip():
        raise BusinessError(
            "CALC_PARAMETER_UNIT_MISSING",
            f"{where}是常量但没有声明单位：常量与字段相加时必须能核对单位口径，"
            "系统不默认单位",
            details={"rule_code": definition.rule_code, "parameter_code": parameter.parameter_code},
        )


def _assert_unit_currency_declared(
    *,
    definition: CalculationDefinition,
    parameter: CalculationParameter,
    upstream_unit: str | None,
    upstream_currency: str | None,
    upstream_label: str,
) -> None:
    """声明口径与上游口径必须一致；上游未声明时明确报错，不代填。"""
    where = f"计算规则“{definition.rule_code}”的参数“{parameter.parameter_code}”"
    if not (upstream_unit or "").strip():
        raise BusinessError(
            "CALC_PARAMETER_UNIT_UNDECLARED",
            f"{upstream_label}没有声明单位，{where}无法核对口径。"
            "系统不做单位换算，也不默认单位；请先在字段配置里声明单位",
            details={"rule_code": definition.rule_code, "parameter_code": parameter.parameter_code},
        )
    if not (upstream_currency or "").strip():
        raise BusinessError(
            "CALC_PARAMETER_CURRENCY_UNDECLARED",
            f"{upstream_label}没有声明币种，{where}无法核对口径。"
            "系统不做汇率换算，也不默认人民币；请先在字段配置里声明币种",
            details={"rule_code": definition.rule_code, "parameter_code": parameter.parameter_code},
        )
    declared_unit = (parameter.unit or "").strip()
    declared_currency = (parameter.currency or "").strip()
    if declared_unit and declared_unit != (upstream_unit or "").strip():
        raise BusinessError(
            "CALC_PARAMETER_UNIT_MISMATCH",
            f"{where}声明的单位“{declared_unit}”与{upstream_label}的单位"
            f"“{upstream_unit}”不一致。系统不做单位换算，请把两边写成同一口径",
            details={"rule_code": definition.rule_code, "parameter_code": parameter.parameter_code},
        )
    if declared_currency and declared_currency != (upstream_currency or "").strip():
        raise BusinessError(
            "CALC_PARAMETER_CURRENCY_MISMATCH",
            f"{where}声明的币种“{declared_currency}”与{upstream_label}的币种"
            f"“{upstream_currency}”不一致。系统不做汇率换算，请把两边写成同一口径",
            details={"rule_code": definition.rule_code, "parameter_code": parameter.parameter_code},
        )


def _assert_additive_unit_consistency(
    *,
    definition: CalculationDefinition,
    ast: Expression,
    parameters: dict[str, CalculationParameter],
    field_catalog: dict[str, FieldReference],
    result_unit: dict[str, tuple[str | None, str | None]],
) -> None:
    """加减法两侧的单位与币种必须完全一致。

    乘除不在此列：比率类公式（金额 ÷ 金额）两侧单位相同而结果无量纲，
    强行要求一致会拒绝掉全部真实指标。加减则必须一致——「元」加「万元」
    在任何业务口径下都不成立。
    """
    def unit_currency(name: str) -> tuple[str | None, str | None]:
        parameter = parameters.get(name)
        if parameter is None:
            raise BusinessError(
                "CALC_PARAMETER_UNDECLARED",
                f"计算规则“{definition.rule_code}”的公式引用了未声明的参数“{name}”",
                details={"rule_code": definition.rule_code, "name": name},
            )
        if parameter.source_type == "FIELD":
            field = field_catalog[parameter.source_code or ""]
            return field.unit, field.currency
        if parameter.source_type == "CALC_RESULT":
            return result_unit.get(parameter.source_code or "", (None, None))
        return parameter.unit, parameter.currency

    def visit(node: Expression) -> None:
        if isinstance(node, Unary):
            visit(node.operand)
            return
        if not isinstance(node, Binary):
            return
        if node.operator in "+-":
            names = identifiers(node)
            pairs = {unit_currency(name) for name in names}
            units = {unit for unit, _ in pairs}
            currencies = {currency for _, currency in pairs}
            if len(units) > 1 or len(currencies) > 1:
                raise BusinessError(
                    "CALC_ADDEND_UNIT_MISMATCH",
                    f"计算规则“{definition.rule_code}”的加减法两侧单位或币种不一致："
                    + "、".join(
                        f"“{name}”({unit or '未声明'} / {currency or '未声明'})"
                        for name in names
                    )
                    + "。系统不做单位换算，也不做汇率换算",
                    details={"rule_code": definition.rule_code, "expression": definition.formula},
                )
        visit(node.left)
        visit(node.right)

    visit(ast)


def _detect_cycles(dependencies: dict[str, set[str]]) -> None:
    """计算之间的依赖必须是无环图。

    环意味着「A 依赖 B、B 又依赖 A」：两者都永远拿不到输入，而调度器会在
    就绪节点集合为空时无事可做——那会被误读成「全部完成」。因此在发布期阻断。
    """
    state: dict[str, int] = {}
    stack: list[str] = []

    def visit(node: str) -> None:
        marker = state.get(node, 0)
        if marker == 2:
            return
        if marker == 1:
            cycle = stack[stack.index(node) :] + [node]
            raise BusinessError(
                "CALC_DEPENDENCY_CYCLE",
                "计算结果之间的依赖形成环，无法确定计算顺序：" + " → ".join(cycle),
                details={"cycle": cycle},
            )
        state[node] = 1
        stack.append(node)
        for target in sorted(dependencies.get(node, set())):
            visit(target)
        stack.pop()
        state[node] = 2

    for node in sorted(dependencies):
        visit(node)


def compile_calculation_rules(
    *,
    definitions: list[CalculationDefinition],
    parameters_by_rule: dict[str, list[CalculationParameter]],
    bands_by_rule: dict[str, list[JudgementBand]],
    field_catalog: dict[str, FieldReference],
    node_instance_code: str,
) -> CompileOutcome:
    """把三个 Sheet 的配置编译成冻结的计算规则集合。

    调用方负责把三张表的行按 rule_code 分组；这里只做编译与校验。
    """
    _assert_unique_codes(definitions)
    definitions_by_code = {item.rule_code: item for item in definitions}
    result_owner = {item.result_code: item.rule_code for item in definitions}
    if len(result_owner) != len(definitions):
        duplicated = _duplicated_result_codes(definitions)
        raise BusinessError(
            "CALC_RESULT_CODE_DUPLICATE",
            "计算结果编号在场景内必须唯一，以下编号被多条计算规则使用："
            + "、".join(duplicated),
            details={"result_codes": duplicated},
        )

    compiled: list[CalculationRule] = []
    pending: list[dict[str, Any]] = []
    dependencies: dict[str, set[str]] = {}

    for definition in definitions:
        parameters = parameters_by_rule.get(definition.rule_code)
        if parameters is None:
            raise BusinessError(
                "CALC_PARAMETER_SHEET_MISSING",
                f"计算规则“{definition.rule_code}”在「计算参数」里没有任何参数行。"
                "公式里的每个标识符都必须声明取值来源",
                details={"rule_code": definition.rule_code},
            )
        ast = parse_expression(
            definition.formula,
            location=_location(definition.rule_code, "计算公式"),
        )
        _validate_parameter_references(
            definition=definition, ast=ast, parameters_by_rule=parameters_by_rule
        )
        parameter_by_code = {item.parameter_code: item for item in parameters}
        if len(parameter_by_code) != len(parameters):
            raise BusinessError(
                "CALC_PARAMETER_CODE_DUPLICATE",
                f"计算规则“{definition.rule_code}”的「计算参数」里存在重复的 parameter_code",
                details={"rule_code": definition.rule_code},
            )
        for parameter in parameters:
            _validate_source_reference(
                definition=definition,
                parameter=parameter,
                field_catalog=field_catalog,
                result_owner=result_owner,
            )
        result_unit = {
            other.result_code: (
                _declared_unit_of(other, parameters_by_rule.get(other.rule_code, [])),
                _declared_currency_of(other, parameters_by_rule.get(other.rule_code, [])),
            )
            for other in definitions
        }
        _assert_additive_unit_consistency(
            definition=definition,
            ast=ast,
            parameters=parameter_by_code,
            field_catalog=field_catalog,
            result_unit=result_unit,
        )
        bands = bands_by_rule.get(definition.rule_code, [])
        assert_bands_disjoint(definition.rule_code, bands)
        if definition.pending:
            pending.append(_pending_item(definition))
        if not definition.enabled:
            # 停用行不参与执行，也不进入依赖图。
            continue
        payload = ast_to_payload(ast)
        result_codes = sorted(
            {
                item.source_code
                for item in parameters
                if item.source_type == "CALC_RESULT" and item.source_code
            }
        )
        field_codes = sorted(
            {
                item.source_code
                for item in parameters
                if item.source_type == "FIELD" and item.source_code
            }
        )
        dependencies[definition.rule_code] = set(result_codes)
        compiled.append(
            CalculationRule(
                definition=definition,
                parameters=list(parameters),
                bands=list(bands),
                ast=payload,
                expression_hash=_expression_hash(payload),
                # 每个参数解析后的定位口径：FIELD 取字段声明，CALC_RESULT 取上游定义。
                # CONSTANT 没有期间，留空。
                parameter_scopes={
                    parameter.parameter_code: _resolved_scope_of(
                        parameter=parameter,
                        field_catalog=field_catalog,
                        definitions_by_code=definitions_by_code,
                    )
                    for parameter in parameters
                },
                depends_on_result_codes=result_codes,
                depends_on_field_codes=field_codes,
                node_instance_code=node_instance_code,
            )
        )

    _detect_cycles(
        {
            rule_code: {
                result_owner[result_code]
                for result_code in targets
                if result_code in result_owner
            }
            for rule_code, targets in dependencies.items()
        }
    )
    # 上游被停用或被排除时依赖断裂：显式报错，不自动跳过也不自动启用。
    enabled_codes = {item.definition.rule_code for item in compiled if item.definition.enabled}
    for rule_code, targets in dependencies.items():
        if rule_code not in enabled_codes:
            continue
        for result_code in targets:
            owner = result_owner.get(result_code)
            if owner is not None and owner not in enabled_codes:
                raise BusinessError(
                    "CALC_DEPENDENCY_DISABLED",
                    f"计算规则“{rule_code}”依赖的计算结果“{result_code}”所属规则"
                    f"“{owner}”已停用，依赖断裂。请启用上游规则或移除该依赖",
                    details={"rule_code": rule_code, "result_code": result_code, "owner": owner},
                )
    return CompileOutcome(rules=compiled, pending=pending)


def _declared_unit_of(
    definition: CalculationDefinition, parameters: list[CalculationParameter]
) -> str | None:
    """一条计算规则的产出单位。

    定义行显式声明时用它；否则取全部参数单位（它们必须已经一致，见加减法校验）。
    都取不到时返回 None——不代填「元」，由下游比较时显式报错。
    """
    if (definition.unit or "").strip():
        return definition.unit
    units = {item.unit for item in parameters if (item.unit or "").strip()}
    return units.pop() if len(units) == 1 else None


def _declared_currency_of(
    definition: CalculationDefinition, parameters: list[CalculationParameter]
) -> str | None:
    if (definition.currency or "").strip():
        return definition.currency
    currencies = {item.currency for item in parameters if (item.currency or "").strip()}
    return currencies.pop() if len(currencies) == 1 else None


def _assert_unique_codes(definitions: list[CalculationDefinition]) -> None:
    seen: set[str] = set()
    duplicated: list[str] = []
    for item in definitions:
        if item.rule_code in seen and item.rule_code not in duplicated:
            duplicated.append(item.rule_code)
        seen.add(item.rule_code)
    if duplicated:
        raise BusinessError(
            "CALC_RULE_CODE_DUPLICATE",
            "计算规则编号在场景内必须唯一，以下编号重复出现：" + "、".join(duplicated),
            details={"rule_codes": duplicated},
        )


def _duplicated_result_codes(definitions: list[CalculationDefinition]) -> list[str]:
    seen: set[str] = set()
    duplicated: list[str] = []
    for item in definitions:
        if item.result_code in seen and item.result_code not in duplicated:
            duplicated.append(item.result_code)
        seen.add(item.result_code)
    return sorted(duplicated)


def _pending_item(definition: CalculationDefinition) -> dict[str, Any]:
    """把一条待确认的计算规则变成可作答的待确认事项。

    与材料/字段/规则同一口径：非停用的 PENDING 行必须留下可作答事项，
    否则它会静默进入已发布配置。这里给出的作答选项是「确认执行 / 明确停用」，
    不预置任何业务取值。
    """
    return {
        "code": "CALC_RULE_PENDING",
        "rule_code": definition.rule_code,
        "rule_name": definition.rule_name,
        "message": f"计算规则“{definition.rule_name}”（{definition.rule_code}）的配置状态是「待确认」，"
        "尚未由业务确认口径，不能进入已发布配置",
        "question_title": f"计算规则“{definition.rule_name}”是否可以按当前口径执行？",
        "question_options": [
            {"label": "口径已确认，可以执行", "value": {"status": "CLEAR"}},
            {"label": "口径未确认，先停用该规则", "value": {"status": "DISABLED"}},
        ],
        "resolve": {
            "target_type": "CALC_RULE",
            "target_ref": definition.rule_code,
            "property": "config_status",
        },
        "input_kind": "SELECT",
    }


def constant_decimal(parameter: CalculationParameter) -> Decimal:
    """常量参数的取值。非法值在 schema 层已被拒绝，这里只是取用。"""
    return Decimal(str(parameter.constant_value).strip())
