"""计算节点的配置契约（计算定义 / 计算参数 / 计算判定）。

三个 Sheet 用 `rule_code` 关联，这三份契约就是它们的**列协议**：列名、内部 key、
类型、必填性与允许值。profile 只声明「这张表有哪些列」，取值合法性由这里判定——
两份都写一遍必然会漂移。

取值一律来自业务配置，本模块**不提供任何默认值**：
- 没有默认单位、没有默认币种、没有默认期间口径；
- 没有默认阈值、没有默认风险等级；
- 缺什么就报什么，不代填。
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ...errors import BusinessError


# 枚举

# 计算类型。它描述这条计算**产出什么形态的值**，决定判定与展示方式。
# 首期只有这几类真实样本用得上的；新增类型必须同时补上判定口径，不能先加类型后补语义。
CalculationType = Literal[
    "RATIO",       # 比率：两个金额相除，结果是无量纲或有单位比率
    "AMOUNT",      # 金额：加减得到的结果金额
    "GROWTH",      # 增长率：(本期-上期)/上期
    "DIFFERENCE",  # 差额：两个金额相减
]

# 参数取值来源。
#   FIELD       引用字段抽取事实（03/字段抽取 的 field_code）
#   CALC_RESULT 引用上游计算结果（另一条计算规则的 result_code）
#   CONSTANT    字面常量（必须显式给出 value，系统不给默认值）
ParameterSourceType = Literal["FIELD", "CALC_RESULT", "CONSTANT"]

# 期间口径。财报的同一个科目在不同期间是不同的数，因此每个参数都必须声明取哪一期。
#   CURRENT / PRIOR          本期 / 上期
#   OPENING / CLOSING        期初 / 期末
#   AVERAGE_OPENING_CLOSING  期初期末平均值，即 (期初 + 期末) / 2
PeriodScope = Literal["CURRENT", "PRIOR", "OPENING", "CLOSING", "AVERAGE_OPENING_CLOSING"]

PERIOD_SCOPE_LABELS: dict[str, str] = {
    "CURRENT": "本期",
    "PRIOR": "上期",
    "OPENING": "期初",
    "CLOSING": "期末",
    "AVERAGE_OPENING_CLOSING": "期初期末平均值",
}

# 合并口径。同一科目在合并报表与母公司报表里是不同的数，不能混用。
StatementScope = Literal["CONSOLIDATED", "PARENT"]

STATEMENT_SCOPE_LABELS: dict[str, str] = {
    "CONSOLIDATED": "合并报表",
    "PARENT": "母公司报表",
}

# 主体范围。首期只支持单一主体口径，多主体合并取数不在首期范围内。
EntityScope = Literal["SELF"]

ENTITY_SCOPE_LABELS: dict[str, str] = {"SELF": "本主体"}

# 结果类型。AMOUNT 与 DECIMAL 都是「数值」，但业务含义不同：AMOUNT 是金额
# （带单位与币种），DECIMAL 是无量纲数值（例如倍数、天数）。分开表达是为了让
# 单位校验能按类型判断，而不是靠有没有填单位来猜。
ResultType = Literal["DECIMAL", "PERCENT", "INTEGER", "AMOUNT"]

# 判定的比较基准。
#   RESULT     直接拿计算结果比
#   ABS_RESULT 拿计算结果的绝对值比（例如「偏离幅度」类判定）
ComparisonBasis = Literal["RESULT", "ABS_RESULT"]

# 判定区间命中后形成的规则结论。它必须由业务显式配置：判定文本中的“良好”
# “预警”等词没有稳定的系统语义，运行期不能据此猜测通过或异常。
CalculationResultStatus = Literal["PASSED", "ABNORMAL", "PENDING_REVIEW"]

COMPARISON_BASIS_LABELS: dict[str, str] = {
    "RESULT": "计算结果",
    "ABS_RESULT": "计算结果的绝对值",
}

# 容差口径。它决定「上下界是绝对值还是相对值」，是**必须显式声明**的业务口径：
# `0.5%` 与 `0.5` 在同一个判定里含义完全不同，而从数值本身看不出来。
#   ABSOLUTE  上下界是绝对数值（例如金额差 0.5 元）
#   PERCENT   上下界是相对比例（例如偏差 0.5%），运行期按「偏差 ÷ 比较基数」判定
# NONE 用于不涉及容差的判定（例如纯区间判定）。
ToleranceKind = Literal["NONE", "ABSOLUTE", "PERCENT"]

# 业务配置状态。与材料/字段/规则同一套三态口径。
CONFIG_STATUS_VALUES = ("CLEAR", "PENDING", "DISABLED")
CONFIG_STATUS_LABELS = {"CLEAR": "已明确", "PENDING": "待确认", "DISABLED": "停用"}

# 计算结果的运行状态。它与规则结果状态同一口径（见 domain.capabilities.RULE_STATUSES），
# 但只取计算真正可能出现的几种，不复制整份集合。
CALC_STATUS_COMPUTED = "COMPUTED"
CALC_STATUS_UNDECIDABLE = "UNDECIDABLE"
CALC_STATUS_DIVISION_BY_ZERO = "DIVISION_BY_ZERO"
CALC_STATUS_INPUT_MISSING = "INPUT_MISSING"


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _decimal_or_error(value: Any, *, field: str, context: str) -> Decimal:
    """把配置里的数值解析成 Decimal。

    不用 float：金额与比率的判定必须精确，float 的二进制误差会让边界判定在
    临界值上随机翻转。解析失败时明确报错并指出是哪个字段的哪个值。
    """
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool) or value is None:
        raise BusinessError(
            "CALC_NUMBER_INVALID",
            f"{context}的「{field}」不是有效数值",
            details={"field": field, "value": value},
        )
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        # 配置里出现浮点说明上游把数值转成了 float；按十进制字面量还原，不引入二进制误差。
        return Decimal(str(value))
    text = str(value).strip()
    if not text:
        raise BusinessError(
            "CALC_NUMBER_EMPTY",
            f"{context}的「{field}」为空；该字段必须显式填写数值，系统不提供默认值",
            details={"field": field},
        )
    try:
        return Decimal(text)
    except InvalidOperation as exc:
        raise BusinessError(
            "CALC_NUMBER_INVALID",
            f"{context}的「{field}」不是有效数值：{text}",
            details={"field": field, "value": text},
        ) from exc


def _optional_decimal(value: Any, *, field: str, context: str) -> Decimal | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    return _decimal_or_error(value, field=field, context=context)


class CalculationParameter(StrictModel):
    """计算参数：一条计算规则引用的一个输入。

    公式里的每个标识符都必须对应到这里声明的 `parameter_code`，而且必须**精确匹配**：
    系统不剥离「本期/上期」这类前缀去猜，因为同一个科目在不同期间是不同的数，
    猜错期间等于把上期的数当成本期用，且结果看起来完全正常。
    """

    parameter_code: str = Field(min_length=1, max_length=80)
    parameter_name: str = Field(min_length=1, max_length=255)
    source_type: ParameterSourceType
    # 引用目标：FIELD 时是字段编号，CALC_RESULT 时是上游计算规则的 result_code。
    # CONSTANT 时可以为空，但必须给出 constant_value。
    source_code: str | None = Field(default=None, max_length=80)
    # 主体范围。期间口径与报表口径**不在这里**：它们由来源自身声明——
    # FIELD 来源看字段的声明（03），CALC_RESULT 来源看上游计算定义的结果期间与报表口径（06）。
    # 参数再声明一遍会形成「两处各说一套」：参数写本期、字段写上期，谁赢没有依据。
    entity_scope: EntityScope = "SELF"
    required: bool = True
    # 单位与币种：CONSTANT 时来自本行；FIELD/CALC_RESULT 时必须与上游一致，编译期核对。
    unit: str | None = Field(default=None, max_length=40)
    currency: str | None = Field(default=None, max_length=40)
    constant_value: str | None = Field(default=None, max_length=80)
    remark: str = ""

    @model_validator(mode="after")
    def validate_source(self) -> "CalculationParameter":
        if self.source_type == "CONSTANT":
            if self.constant_value is None or not str(self.constant_value).strip():
                raise ValueError(
                    f"参数“{self.parameter_code}”的来源类型是常量，必须显式填写常量值；系统不提供默认值"
                )
            # 常量必须能解析成 Decimal，否则运行期才发现会让整条计算无法判定。
            _decimal_or_error(
                self.constant_value,
                field="constant_value",
                context=f"参数“{self.parameter_code}”",
            )
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
    `lower_inclusive` / `upper_inclusive` 显式声明：**不设默认值**，因为
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
    # 容差口径：上下界是绝对值还是相对比例。必须显式声明（口径见上面的 ToleranceKind），
    # 不涉及容差的区间判定填 NONE。
    tolerance_kind: ToleranceKind = "NONE"
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
        lower = _optional_decimal(
            self.lower_value, field="lower_value", context=f"判定“{self.condition_code}”"
        )
        upper = _optional_decimal(
            self.upper_value, field="upper_value", context=f"判定“{self.condition_code}”"
        )
        if lower is None and upper is None:
            raise ValueError(
                f"判定“{self.condition_code}”的上下界都为空：整条数轴都被这一条覆盖，"
                "它会让所有结果都落到同一个结论上，请显式填写区间"
            )
        if lower is not None and upper is not None and lower > upper:
            raise ValueError(
                f"判定“{self.condition_code}”的下界 {lower} 大于上界 {upper}，区间不成立"
            )
        return self

    def contains(self, value: Decimal) -> bool:
        lower = self.lower
        upper = self.upper
        if lower is not None:
            if value < lower or (value == lower and not self.lower_inclusive):
                return False
        if upper is not None:
            if value > upper or (value == upper and not self.upper_inclusive):
                return False
        return True

    def overlaps(self, other: "JudgementBand") -> bool:
        """两条区间是否有公共点。边界相接但至少一侧开时不算重叠。"""
        low_a, high_a = self.lower, self.upper
        low_b, high_b = other.lower, other.upper
        if low_a is not None and high_b is not None:
            if low_a > high_b or (low_a == high_b and not (self.lower_inclusive and other.upper_inclusive)):
                return False
        if low_b is not None and high_a is not None:
            if low_b > high_a or (low_b == high_a and not (other.lower_inclusive and self.upper_inclusive)):
                return False
        return True

    def describe(self) -> str:
        lower = self.lower
        upper = self.upper
        if lower is None:
            return f"{'≤' if self.upper_inclusive else '<'} {upper}"
        if upper is None:
            return f"{'≥' if self.lower_inclusive else '>'} {lower}"
        left = "[" if self.lower_inclusive else "("
        right = "]" if self.upper_inclusive else ")"
        return f"{left}{lower}, {upper}{right}"


class CalculationDefinition(StrictModel):
    """一条计算规则的定义。

    `result_code` 是这条计算结果的业务编号，也是**下游引用的身份**：后续计算规则
    与风险规则都通过它引用本结果。它必须显式填写，不能由 rule_code 顶替——
    同一场景里 rule_code 是规则身份，result_code 是结果身份，两者是两件事。
    """

    rule_code: str = Field(min_length=1, max_length=80)
    rule_name: str = Field(min_length=1, max_length=255)
    calculation_type: CalculationType
    formula: str = Field(min_length=1, max_length=2000)
    result_code: str = Field(min_length=1, max_length=80)
    result_type: ResultType
    unit: str | None = Field(default=None, max_length=40)
    currency: str | None = Field(default=None, max_length=40)
    # 「结果期间」：这条计算**算出来的数**属于哪一期，不是取数期间（取数期间在字段上）。
    # 跨期指标（(本期-上期)/上期）不属于任何单一期间，因此必须由业务声明，不能推断。
    period_policy: PeriodScope
    # 计算结果的报表口径：派生事实按它落库，下游引用按它定位。
    statement_scope: StatementScope
    status: str
    remark: str = ""
    # 业务说明类配置事实：不参与执行判定，但导出与页面要看到业务真正写下的内容。
    # 它们是**配置**而非计算语义，因此不进入 AST、不参与单位币种核对。
    category: str = ""
    description: str = ""
    risk_level_text: str = ""
    bands_text: str = ""
    verification_logic: str = ""
    review_advice: str = ""

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
    """编译后的一条完整计算规则：定义 + 参数 + 判定区间 + 编译好的 AST。"""

    definition: CalculationDefinition
    parameters: list[CalculationParameter]
    bands: list[JudgementBand]
    # 编译产物：受限 AST 的 JSON 形式与它的表达式版本哈希。
    ast: dict[str, Any]
    expression_hash: str
    # 每个参数**解析后**的定位口径：由来源自身决定（字段看字段声明，上游结果看上游定义）。
    # 编译期解析一次并冻结，运行期直接按它建立字段事实的定位键——
    # 运行期再去推断期间等于每次执行都重新决定一次口径，重跑可能落到另一期。
    parameter_scopes: dict[str, dict[str, str]] = Field(default_factory=dict)
    # 依赖的上游 result_code（由 CALC_RESULT 参数与风险引用共同决定），供 DAG 使用。
    depends_on_result_codes: list[str] = Field(default_factory=list)
    depends_on_field_codes: list[str] = Field(default_factory=list)
    # 该计算规则所在的节点实例码。
    node_instance_code: str


def assert_bands_disjoint(rule_code: str, bands: list[JudgementBand]) -> None:
    """判定区间必须两两不相交。

    区间重叠时同一个计算结果会同时命中两条判定，系统没有依据在它们之间挑一条：
    按书写顺序取第一条会让「后面那条永远不会命中」这件事完全不可见。
    因此重叠一律在**发布期**阻断，不留给运行期。
    """
    for index, left in enumerate(bands):
        for right in bands[index + 1 :]:
            if left.overlaps(right):
                raise BusinessError(
                    "CALC_JUDGEMENT_BAND_OVERLAP",
                    f"计算规则“{rule_code}”的判定区间重叠："
                    f"“{left.condition_code}”{left.describe()} 与 "
                    f"“{right.condition_code}”{right.describe()} 有公共部分，"
                    "同一个结果会同时命中两条判定。请明确边界（开/闭）或调整区间后重新发布",
                    details={
                        "rule_code": rule_code,
                        "left": {"condition_code": left.condition_code, "band": left.describe()},
                        "right": {"condition_code": right.condition_code, "band": right.describe()},
                    },
                )
