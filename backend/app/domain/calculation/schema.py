"""计算节点的配置契约（计算定义 / 计算参数 / 计算判定）。

自由规则库由 LLM 定义业务方法和判断标准，参数按真实字段与上游结果关联。
LLM 直接解释业务方法，不要求四则表达式；旧程序计算配置必须重新生成。

取值一律来自业务配置：不代填具体单位、币种、期间口径、阈值或风险等级；期间和报表口径缺省只归一为 NOT_APPLICABLE。
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ...common.exceptions import BusinessError


# 枚举

# 参数取值来源。
#   FIELD       引用当前场景字段配置对应的抽取事实
#   CALC_RESULT 引用上游计算结果（另一条计算规则的 result_code）
#   CONSTANT    字面常量（必须显式给出 value，系统不给默认值）
ParameterSourceType = Literal["FIELD", "CALC_RESULT", "CONSTANT"]

# CHECK 沿用历史计算契约；VALUE_ONLY 明确只产生中间值，不形成审核结论。
CalculationExecutionMode = Literal["CHECK", "VALUE_ONLY"]

# 来源涉及期间时按业务配置定位；不涉及时由编译层归一为 NOT_APPLICABLE。
#   CURRENT / PRIOR          本期 / 上期
#   OPENING / CLOSING        期初 / 期末
#   AVERAGE_OPENING_CLOSING  期初期末平均值，即 (期初 + 期末) / 2
PeriodScope = Literal["CURRENT", "PRIOR", "OPENING", "CLOSING", "AVERAGE_OPENING_CLOSING", "NOT_APPLICABLE"]

PERIOD_SCOPE_LABELS: dict[str, str] = {
    "NOT_APPLICABLE": "不适用",
    "CURRENT": "本期",
    "PRIOR": "上期",
    "OPENING": "期初",
    "CLOSING": "期末",
    "AVERAGE_OPENING_CLOSING": "期初期末平均值",
}

# 来源涉及报表时按业务声明区分口径；不涉及时由编译层归一为 NOT_APPLICABLE。
StatementScope = Literal["CONSOLIDATED", "PARENT", "NOT_APPLICABLE"]

# 主体范围当前只支持单一主体口径，不支持多主体合并取数。
EntityScope = Literal["SELF"]

# 结果仍用 Decimal 保存。AMOUNT 表示带单位和币种的金额，INTEGER 要求结果为整数；
# DECIMAL/PERCENT 的数值尺度按业务公式和区间声明，不隐式取整或乘除 100。
ResultType = Literal["DECIMAL", "PERCENT", "INTEGER", "AMOUNT"]

# 判定的比较基准。
#   RESULT     直接拿计算结果比
#   ABS_RESULT 拿计算结果的绝对值比（例如「偏离幅度」类判定）
ComparisonBasis = Literal["RESULT", "ABS_RESULT"]

# 判定区间命中后形成的规则结论。它必须由业务显式配置：判定文本中的“良好”
# “预警”等词没有稳定的系统语义，运行期不能据此猜测通过或异常。
CalculationResultStatus = Literal["PASSED", "ABNORMAL", "PENDING_REVIEW"]

# 容差口径随业务方法交给 LLM 解释；程序不计算相对偏差或换算边界。
ToleranceKind = Literal["NONE", "ABSOLUTE", "PERCENT"]

# 业务配置状态。与材料/字段/规则同一套三态口径。
CONFIG_STATUS_VALUES = ("CLEAR", "PENDING", "DISABLED")

# 计算求值状态，独立于合格/异常结论。只有 VALUE_ONLY 求值成功时，
# COMPUTED 同时用于规则结果；求值失败在规则结果中记 EVIDENCE_INSUFFICIENT。
CALC_STATUS_COMPUTED = "COMPUTED"
CALC_STATUS_UNDECIDABLE = "UNDECIDABLE"
CALC_STATUS_DIVISION_BY_ZERO = "DIVISION_BY_ZERO"
CALC_STATUS_INPUT_MISSING = "INPUT_MISSING"


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


def assert_llm_engine(rule_code: str, execution_engine: Any) -> None:
    """旧配置必须重新生成，不能静默改用模型执行原程序规则。"""
    if execution_engine == "PROGRAM" or execution_engine is None:
        raise BusinessError(
            "CALC_PROGRAM_RETIRED",
            f"计算规则“{rule_code}”使用旧程序计算配置或未声明执行方式；"
            "程序计算引擎已移除，请通过 LLM 重新解析规则库并发布后再执行",
            details={"rule_code": rule_code, "execution_engine": execution_engine},
        )
    if execution_engine != "LLM":
        raise BusinessError(
            "CALC_EXECUTION_ENGINE_INVALID", f"计算规则“{rule_code}”的执行方式必须明确声明为 LLM",
            details={"rule_code": rule_code, "execution_engine": execution_engine},
        )


def _decimal_or_error(value: Any, *, field: str, context: str) -> Decimal:
    """把配置里的数值解析成 Decimal。

    不用 float：金额与比率的判定必须精确，float 的二进制误差会让边界判定在
    临界值上随机翻转。解析失败时明确报错并指出是哪个字段的哪个值。
    """
    if isinstance(value, bool) or value is None:
        raise BusinessError(
            "CALC_NUMBER_INVALID",
            f"{context}的「{field}」不是有效数值",
            details={"field": field, "value": value},
        )
    # 浮点配置同样按十进制字面量还原；所有输入类型统一检查有限性。
    text = str(value).strip()
    if not text:
        raise BusinessError(
            "CALC_NUMBER_EMPTY",
            f"{context}的「{field}」为空；该字段必须显式填写数值，系统不提供默认值",
            details={"field": field},
        )
    try:
        number = Decimal(text)
    except InvalidOperation as exc:
        raise BusinessError(
            "CALC_NUMBER_INVALID",
            f"{context}的「{field}」不是有效数值：{text}",
            details={"field": field, "value": text},
        ) from exc
    if not number.is_finite():
        raise BusinessError(
            "CALC_NUMBER_INVALID",
            f"{context}的「{field}」必须是有限数值：{text}",
            details={"field": field, "value": text},
        )
    return number


def _optional_decimal(value: Any, *, field: str, context: str) -> Decimal | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    return _decimal_or_error(value, field=field, context=context)


class CalculationParameter(StrictModel):
    """计算参数：一条计算规则引用的一个输入。

    业务方法需要的输入由配置显式声明。字段与上游结果按 source_code 精确引用，
    不根据名称或 Excel 工作表位置猜测来源；parameter_code 用于模型返回输入引用。
    """

    parameter_code: str = Field(min_length=1, max_length=80)
    parameter_name: str = Field(min_length=1, max_length=255)
    source_type: ParameterSourceType
    # 引用目标：FIELD 时是字段编号，CALC_RESULT 时是上游计算规则的 result_code。
    # CONSTANT 时可以为空，但必须给出 constant_value。
    source_code: str | None = Field(default=None, max_length=80)
    # 主体范围。期间口径与报表口径不在这里：它们由来源自身声明——
    # FIELD 来源看字段声明，CALC_RESULT 来源看上游计算定义声明。
    # 参数再声明一遍会形成「两处各说一套」：参数写本期、字段写上期，谁赢没有依据。
    entity_scope: EntityScope = "SELF"
    required: bool = True
    # 参数声明与事实实际单位、币种都传给模型，依据业务方法处理。
    unit: str | None = Field(default=None, max_length=40)
    currency: str | None = Field(default=None, max_length=40)
    # 数值、日期或文本常量按业务原文传给 LLM，不按旧算术引擎强制转成 Decimal。
    constant_value: str | None = Field(default=None, max_length=80)
    remark: str = ""

    @model_validator(mode="after")
    def validate_source(self) -> "CalculationParameter":
        if self.source_type == "CONSTANT":
            return self
        if not self.source_code or not str(self.source_code).strip():
            raise ValueError(
                f"参数“{self.parameter_code}”的来源类型是{self.source_type}，必须填写引用编号"
            )
        if self.constant_value is not None and str(self.constant_value).strip():
            raise ValueError(
                f"参数“{self.parameter_code}”同时填写了引用编号与常量值，无法确定取哪一个"
            )
        return self


class JudgementBand(StrictModel):
    """计算判定的一条区间。

    区间是 `[lower, upper]` 的闭/开组合，`None` 表示该侧无界。边界是否包含由
    `lower_inclusive` / `upper_inclusive` 显式声明：不设默认值，因为
    「<1 预警」与「≤1 预警」在临界值上是两个不同的业务结论。
    """

    condition_code: str = Field(min_length=1, max_length=80)
    lower_value: str | None = Field(default=None, max_length=80)
    lower_inclusive: bool
    upper_value: str | None = Field(default=None, max_length=80)
    upper_inclusive: bool
    comparison_basis: ComparisonBasis
    judgement: str = Field(min_length=1, max_length=255)
    result_status: CalculationResultStatus
    risk_level: str | None = Field(default=None, max_length=40)
    action: str | None = Field(default=None, max_length=255)
    tolerance_kind: ToleranceKind = Field(
        default="NONE",
        description="业务声明的容差口径，由 LLM 结合计算方法和判断标准解释，不由程序换算。",
    )
    remark: str = ""

    @property
    def lower(self) -> Decimal | None:
        return _optional_decimal(
            self.lower_value, field="lower_value", context=f"判定“{self.condition_code}”"
        )

    @property
    def upper(self) -> Decimal | None:
        return _optional_decimal(
            self.upper_value, field="upper_value", context=f"判定“{self.condition_code}”"
        )

    @model_validator(mode="after")
    def validate_band(self) -> "JudgementBand":
        # 仅检查数值格式；业务区间、容差和命中判定由 LLM 解释。
        _optional_decimal(self.lower_value, field="lower_value", context=f"判定“{self.condition_code}”")
        _optional_decimal(self.upper_value, field="upper_value", context=f"判定“{self.condition_code}”")
        return self

    def describe(self) -> str:
        lower = self.lower
        upper = self.upper
        if lower is None and upper is None:
            return "任意结果"
        if lower is None:
            return f"{'≤' if self.upper_inclusive else '<'} {upper}"
        if upper is None:
            return f"{'≥' if self.lower_inclusive else '>'} {lower}"
        left = "[" if self.lower_inclusive else "("
        right = "]" if self.upper_inclusive else ")"
        return f"{left}{lower}, {upper}{right}"


class CalculationDefinition(StrictModel):
    """一条计算规则的定义。

    `result_code` 是这条计算结果的业务编号，也是下游引用的身份：后续计算规则
    与风险规则都通过它引用本结果。它必须显式填写，不能由 rule_code 顶替——
    同一场景里 rule_code 是规则身份，result_code 是结果身份，两者是两件事。
    """

    rule_code: str = Field(min_length=1, max_length=80)
    rule_name: str = Field(min_length=1, max_length=255)
    calculation_type: str = Field(min_length=1)
    execution_engine: Literal["LLM"]
    execution_mode: CalculationExecutionMode = "CHECK"
    formula: str
    judgement_instruction: str = ""
    result_code: str = Field(min_length=1, max_length=80)
    result_type: ResultType
    unit: str | None = Field(default=None, max_length=40)
    currency: str | None = Field(default=None, max_length=40)
    # 「结果期间」：这条计算算出来的数属于哪一期，不是取数期间（取数期间在字段上）。
    # 跨期指标（(本期-上期)/上期）不属于任何单一期间；实际涉及期间时由业务声明，
    # 不涉及期间时使用 NOT_APPLICABLE。
    period_policy: PeriodScope = "NOT_APPLICABLE"
    # 计算结果的报表口径：派生事实按它落库，下游引用按它定位；不涉及时报表口径为
    # NOT_APPLICABLE。
    statement_scope: StatementScope = "NOT_APPLICABLE"
    status: str
    remark: str = ""
    # 保留业务说明原文，供 LLM 执行时读取。
    category: str = ""
    description: str = ""
    risk_level_text: str = ""
    bands_text: str = ""
    verification_logic: str = ""
    review_advice: str = ""

    @model_validator(mode="before")
    @classmethod
    def validate_engine(cls, value: Any) -> Any:
        if isinstance(value, dict):
            assert_llm_engine(str(value.get("rule_code") or ""), value.get("execution_engine"))
        return value

    @field_validator("status")
    @classmethod
    def validate_status(cls, value: str) -> str:
        normalized = str(value or "").strip().upper()
        if normalized not in CONFIG_STATUS_VALUES:
            raise ValueError(
                f"配置状态“{value}”不受支持，只能是 " + " / ".join(CONFIG_STATUS_VALUES)
            )
        return normalized

    @property
    def enabled(self) -> bool:
        """停用行不参与执行。与材料/字段/规则同一口径：停用是业务口径，不是运行状态。"""
        return self.status != "DISABLED"

    @property
    def pending(self) -> bool:
        return self.status == "PENDING"


class CalculationRule(StrictModel):
    """发布后的业务定义、输入定位和依赖，供 LLM 执行。"""

    definition: CalculationDefinition
    parameters: list[CalculationParameter]
    bands: list[JudgementBand]
    # 每个参数解析后的定位口径：由来源自身决定（字段看字段声明，上游结果看上游定义）。
    # 编译期解析一次并冻结，运行期直接按它建立字段事实的定位键——
    # 运行期再去推断期间等于每次执行都重新决定一次口径，重跑可能落到另一期。
    parameter_scopes: dict[str, dict[str, str]] = Field(default_factory=dict)
    # 依赖的上游 result_code（由 CALC_RESULT 参数与风险引用共同决定），供 DAG 使用。
    depends_on_result_codes: list[str] = Field(default_factory=list)
    depends_on_field_codes: list[str] = Field(default_factory=list)
    # 该计算规则所在的节点实例码。
    node_instance_code: str


def assert_calculation_bands(
    rule_code: str, execution_mode: CalculationExecutionMode, bands: list[JudgementBand],
    *, judgement_instruction: str = "",
) -> None:
    """核对计算用途与判定配置，不能丢弃标准或为纯计算伪造标准。"""
    if execution_mode == "VALUE_ONLY":
        if bands or judgement_instruction.strip():
            raise BusinessError(
                "CALC_VALUE_ONLY_HAS_BANDS",
                f"计算规则“{rule_code}”声明仅计算中间值，却配置了判断标准；请明确计算用途",
                details={"rule_code": rule_code},
            )
    elif not bands and not judgement_instruction.strip():
        raise BusinessError(
            "MANIFEST_CALC_BAND_MISSING",
            f"计算规则“{rule_code}”声明计算并判定，却没有可用的判断标准，不能发布",
            details={"rule_codes": [rule_code]},
        )
