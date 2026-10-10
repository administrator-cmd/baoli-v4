"""计算配置编译：解析真实输入来源和定位口径，核对编号与依赖，不执行业务计算。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ...common.exceptions import BusinessError
from .schema import (
    CalculationDefinition,
    CalculationParameter,
    CalculationRule,
    JudgementBand,
)


@dataclass(frozen=True)
class FieldReference:
    """编译期可见的一个字段抽取目标。

    它来自当前场景的字段配置，是计算参数的 FIELD 来源；不依赖 Excel 表名或固定布局。
    """

    field_code: str
    field_key: str
    name: str
    value_type: str
    unit: str | None
    currency: str | None
    material_key: str
    material_name: str
    # 取数期间与报表口径：由字段自己声明，编译期把它们解析成参数的定位口径。
    # 计算参数引用来源字段的声明，不重复设置；不涉及这些口径时使用 NOT_APPLICABLE。
    period_scope: str
    statement_scope: str


@dataclass
class CompileOutcome:
    """编译结果与供查看的待确认说明。"""

    rules: list[CalculationRule]
    # 待确认事项：它们有明确的业务对象与属性，可以经既有裁决入口作答。
    # 保留非停用 PENDING 计算规则的业务说明，不作为发布审批条件。
    pending: list[dict[str, Any]] = field(default_factory=list)


def _resolved_scope_of(
    *,
    parameter: CalculationParameter,
    field_catalog: dict[str, FieldReference],
    definitions_by_code: dict[str, "CalculationDefinition"],
) -> dict[str, str]:
    """一个参数解析后的定位口径（期间 + 报表口径）。

    - `FIELD`：取当前场景中来源字段声明的取数期间与报表口径；
    - `CALC_RESULT`：取上游计算定义声明的结果期间与报表口径；
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
    """参数引用的目标必须真实存在。"""
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
    node_instance_code: str | dict[str, str],
) -> CompileOutcome:
    """核对业务定义、参数来源及依赖，冻结输入定位和规则归属。"""
    # 停用规则只保留原始配置，不解析参数、判定条件或计算依赖。
    definitions = [definition for definition in definitions if definition.enabled]
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
                "CALC_PARAMETERS_MISSING",
                f"计算规则“{definition.rule_code}”缺少输入参数配置；"
                "计算输入必须明确声明取值来源",
                details={"rule_code": definition.rule_code},
            )
        parameter_by_code = {item.parameter_code: item for item in parameters}
        if len(parameter_by_code) != len(parameters):
            raise BusinessError(
                "CALC_PARAMETER_CODE_DUPLICATE",
                f"计算规则“{definition.rule_code}”的输入参数配置里存在重复的 parameter_code",
                details={"rule_code": definition.rule_code},
            )
        for parameter in parameters:
            _validate_source_reference(
                definition=definition,
                parameter=parameter,
                field_catalog=field_catalog,
                result_owner=result_owner,
            )
        bands = bands_by_rule.get(definition.rule_code, [])
        # 判定标准的缺项由本条规则执行时报告，不阻断发布。
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
                node_instance_code=(node_instance_code[definition.rule_code]
                                    if isinstance(node_instance_code, dict) else node_instance_code),
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
