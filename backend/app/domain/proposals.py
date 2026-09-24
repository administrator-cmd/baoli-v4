from collections import Counter
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


# 大写金额（DECIMAL_CN）与金额（DECIMAL）分开：前者保留大写原文，
# 只有业务确认了单位、币种与换算口径后才能得到可核验数值，不能按字符串比较。
ValueType = Literal["STRING", "DECIMAL", "DECIMAL_CN", "DATE", "BOOLEAN", "CLAUSE"]
# 本轮只支持这两种适用条件；其他复杂条件必须明确指出未支持，不能由模型自行改变适用范围。
ApplicabilityKind = Literal["ALWAYS", "MATERIALS_UPLOADED"]
# 业务问题的作答形式：选择题 / 数值输入 / 文本输入。缺少阈值等参数时用输入控件，不编造选项。
QuestionInputKind = Literal["SELECT", "DECIMAL", "TEXT"]
# 当前不支持的取数/汇总能力。它们必须变成明确报错，不能取第一份文件或用模型估算。
UnsupportedCapabilityCode = Literal[
    "SUM_ALL_FILES",
    "MULTI_FILE_AGGREGATE",
    "CROSS_PERIOD_TOTAL",
    "MULTI_CONTRACT_GROUP",
    "CONSTANT_COMPARISON",
    "EXTERNAL_LOOKUP",
]
AllowedOperator = Literal[
    "MATERIAL_EXISTS",
    "FILE_QUALITY_CHECK",
    "LLM_EXTRACT",
    "FIELD_EQUAL",
    "FIELD_NOT_EQUAL",
    "DATE_COMPARE",
    "DECIMAL_COMPARE",
    # 加总比较：左侧一个字段与右侧多个加数之和比较（「字段 + 字段 + …」）。
    "DECIMAL_SUM_COMPARE",
    # 大小写金额一致性：当前缺少已确认的换算口径，运行时明确不可判定。
    "DECIMAL_CN_EQUAL",
    # 单字段非空校验（对照内容为空的检查行）。
    "FIELD_NOT_EMPTY",
    # 条款存在性检查：判断条款本身是否存在，不按抽取文字是否非空判断。
    "CLAUSE_ABSENT",
    "LLM_VALIDATE",
    "RISK_TRIGGER",
    "REPORT_RENDER",
]
# 风险疑点的来源节点：风险只能挂在材料质量或两条校验链路上，且必须与来源审核规则所在工作表一致。
SourceNodeKind = Literal["MATERIAL_QUALITY", "INTERNAL_VALIDATE", "CROSS_VALIDATE"]
# 取数期间：同一个科目在不同期间是不同的数，因此每个字段都必须声明它取哪一期。
# 计算参数不再重复声明这一项——字段自己声明，是唯一的来源。
PeriodScope = Literal["CURRENT", "PRIOR", "OPENING", "CLOSING", "AVERAGE_OPENING_CLOSING"]
# 报表口径：同一科目在合并报表与母公司报表里是不同的数，不能混用。
StatementScope = Literal["CONSOLIDATED", "PARENT"]

# **字段**声明的口径：比计算结果的口径多一个「不适用」。
#
# 合同场景没有期间与合并/母公司的概念，03_字段 的这两列留空即表达「不适用」，
# 因此字段这一侧必须能承载它。计算结果那一侧（PeriodScope / StatementScope）
# 不需要：计算定义声明的是「这个指标算出来属于哪一期」，不存在「不适用」。
FieldPeriodScope = Literal[
    "CURRENT", "PRIOR", "OPENING", "CLOSING", "AVERAGE_OPENING_CLOSING", "NOT_APPLICABLE"
]
FieldStatementScope = Literal["CONSOLIDATED", "PARENT", "NOT_APPLICABLE"]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SourceRef(StrictModel):
    sheet_key: str
    row_no: int = Field(ge=1)
    cells: list[str] = Field(min_length=1)
    # 单元格地址 -> 该格完整原文（不截断）。预览与来源追溯要看到用户真正写下的内容，
    # 因此由后端按工作簿原文确定性填充；模型不得生成，也不进入模型响应契约。
    texts: dict[str, str] = Field(default_factory=dict)


class MaterialProposal(StrictModel):
    candidate_ref: str
    source_id: str | None = None
    name: str
    required: bool
    # 「业务说明」列是业务含义的一部分，必须随材料一起保存，不能接收后丢弃。
    description: str = ""
    # 业务配置状态：已明确 / 待确认 / 停用。它不是 PM 原状态，也不是系统运行或发布状态；
    # 待确认且未停用的行可以保存草稿与修正，但不能发布为可执行版本。
    config_status: str | None = None
    source: SourceRef


class FieldProposal(StrictModel):
    candidate_ref: str
    source_id: str | None = None
    name: str
    value_type: ValueType
    unit: str | None = None
    # 币种是金额比较与加总的口径之一。留空表示未声明，不表示人民币：
    # 币种未知时系统不做数值比较或求和，只在证据不足中如实表达。
    currency: str | None = None
    material_ref: str
    required: bool
    instruction: str
    # 产物来源：该字段的取值由哪种处理产物提供（当前仅印章识别需要显式声明）。
    # 主体核对必须按这一声明确定「待核对主体取自哪份材料的哪种产物」，
    # 不能靠字段名里是否含某些汉字来猜。留空表示字段由常规字段抽取提供。
    artifact_source: str | None = None
    # 取数期间与报表口径：**由字段自己声明**。计算参数不再重复声明这两项，
    # 因此这里是「这个数取的是哪一期、哪个口径」的唯一来源。
    # 留空（合同场景的常态）落成显式的「不适用」，不是 NULL。
    period_scope: FieldPeriodScope
    statement_scope: FieldStatementScope
    config_status: str | None = None
    source: SourceRef


class RuleInputProposal(StrictModel):
    field_ref: str
    material_ref: str


class ApplicabilityProposal(StrictModel):
    """规则的结构化适用条件；比较规则留空表示始终适用。"""

    kind: ApplicabilityKind


class UnsupportedCapability(StrictModel):
    """当前不支持的取数/汇总要求。

    模型只有在审核要求确实要求跨多份材料汇总、跨期间合计、多合同分组、字段与常量
    比较等能力时才填这里。后端据此明确报错，既不生成"取第一份文件"的可执行规则，
    也不改成让大模型估算。
    """

    code: UnsupportedCapabilityCode
    detail: str = Field(min_length=1)


class RuleProposal(StrictModel):
    candidate_ref: str
    # 业务规则编号：直接来自 Excel 的「规则ID」列（FSCV-001、FIDX-006 …）。
    # 它是新版本的规则身份；rule_key 只是历史身份，不再由这里派生。
    # 旧格式没有编号列，此时为 None——不用派生值冒充业务编号。
    rule_code: str | None = Field(default=None, max_length=80)
    source_id: str | None = None
    name: str
    # 财报风险规则的来源：上游计算规则的结果编号。它与 source_rule_ref（同表内的
    # 检查规则候选引用）是两个口径，不能混在一个列表里——业务编号跨表引用，
    # 候选引用只在本次导入内部有效。
    source_rule_codes: list[str] = Field(default_factory=list)
    # 规则归属的**节点实例码**（用户配置的 `scenario_draft_node.node_code`）。
    # 它不再是能力类型：同一场景可以有两个同类节点（两张报表各一个字段抽取），
    # 规则必须说清自己落在哪一个实例上。能力类型在同一条规则的 `node_type` 里。
    node: str = Field(min_length=1, max_length=80)
    # 能力类型：决定这条规则由谁执行、能挂哪张 Sheet。它来自统一模板协议的
    # `node_types`。旧格式（节点码即能力类型）缺这一列时按 `node` 取值读取。
    node_type: str | None = Field(default=None, max_length=40)
    operator: AllowedOperator
    inputs: list[RuleInputProposal] = Field(default_factory=list)
    output_field_refs: list[str] = Field(default_factory=list)
    source_rule_ref: str | None = None
    # 风险疑点的「来源节点」由后端按单元格原文确定性写入，模型不参与判断；其他规则为 None。
    source_node: SourceNodeKind | None = None
    material_refs: list[str] = Field(default_factory=list)
    parameters: dict[str, Any] = Field(default_factory=dict)
    # 「审核要求」原文由后端从已映射的单元格确定性读取，模型不改写、不省略。
    requirement: str = ""
    applicability: ApplicabilityProposal | None = None
    # 非空表示这条规则要求的取数方式当前不支持，导入会明确报错而不是猜测执行。
    unsupported_capability: UnsupportedCapability | None = None
    # 以下字段只由节点版（contract_review_v3）的确定性解析填充：
    #   config_status：业务配置状态（已明确 / 待确认 / 停用）；
    #   check_operator：业务填写的「合格要求」原文取值，用于导出与预览；
    #   check_level / risk_level_value：检查等级与风险等级原文，两者独立保留、不相互覆盖；
    #   risk_names / source_rule_refs：前者沿用历史键名，现存关联风险编号；后者是风险来源检查引用；
    #   risk_condition：风险成立条件（任一关联检查不合格 / 全部关联检查不合格）；
    #   quality_supported：文件处理检查是否具备已确认的判定能力。
    # 这些是业务配置事实而非执行参数，因此不进入 parameters 契约。
    config_status: str | None = None
    check_operator: str | None = None
    check_level: str | None = None
    risk_level_value: str | None = None
    risk_condition: str | None = None
    risk_names: list[str] = Field(default_factory=list)
    source_rule_refs: list[str] = Field(default_factory=list)
    quality_supported: bool | None = None
    # 主体/签章一致性检查标记：由编译期按引用字段名确定性派生，不由模型填写。
    subject_check: bool | None = None
    source: SourceRef

    @model_validator(mode="after")
    def validate_date_parameters(self) -> "RuleProposal":
        if self.operator == "DATE_COMPARE" and self.parameters.get("formula_type") == "DECIMAL":
            raise ValueError("日期比较不能使用 DECIMAL 公式")
        return self


class ReportSectionProposal(StrictModel):
    candidate_ref: str
    section_name: str
    field_refs: list[str] = Field(default_factory=list)
    content_instruction: str
    # 节点版（contract_review_v3）的报告配置：一份报告由多行章节组成，
    # 同一报告编号的名称、材料范围、输出格式、结果排序必须完全一致。
    report_code: str | None = None
    report_name: str | None = None
    material_refs: list[str] = Field(default_factory=list)
    formats: list[str] = Field(default_factory=list)
    section_order: int | None = None
    content_type: str | None = None
    result_sort: str | None = None
    config_status: str | None = None
    source: SourceRef


class QuestionOption(StrictModel):
    label: str
    value: Any


class AnswerResolution(StrictModel):
    """业务问题的作答如何落到候选对象属性上。

    REPLACE：作答值本身就是该属性的完整取值（比较方向这类选择题用完整参数字典作答）。
    SET_PARAMETER：作答值是单个参数值，后端把它并入 base_parameters 的对应参数名。
    缺少金额差额阈值这类数值输入用它，避免让用户在页面里手写参数对象。
    """

    mode: Literal["REPLACE", "SET_PARAMETER"] = "REPLACE"
    parameter_key: str | None = None
    base_parameters: dict[str, Any] = Field(default_factory=dict)


class BusinessQuestion(StrictModel):
    question_ref: str
    title: str
    description: str
    target_type: Literal["MATERIAL", "FIELD", "RULE", "REPORT_SECTION"]
    target_ref: str
    property: str
    # 缺少阈值、口径一类数值时用输入控件作答，不为了凑够两个选项而编造候选值。
    input_kind: QuestionInputKind = "SELECT"
    options: list[QuestionOption] = Field(default_factory=list)
    # 由后端在"缺少必要参数"场景生成；模型不需要、也不应该自己填。
    resolve: AnswerResolution | None = None
    source: SourceRef

    @model_validator(mode="after")
    def validate_options(self) -> "BusinessQuestion":
        if self.input_kind == "SELECT" and len(self.options) < 2:
            raise ValueError("选择题必须至少提供两个真实候选选项")
        return self


class DisabledSourceColumn(StrictModel):
    key: str
    label: str
    value: str
    cell: str | None = None


class DisabledSourceRow(StrictModel):
    source: SourceRef
    sheet_name: str
    source_id: str | None = None
    columns: list[DisabledSourceColumn]


class SheetProposal(StrictModel):
    sheet_key: str
    # Sheet 角色由**统一模板协议**声明（base Excel 的 10 张业务表）。
    # 这里不做成封闭枚举：封闭枚举每加一张表就要改一次契约，而角色合法性已由
    # 统一协议与绑定校验兜底（见 import_service.update_sheet_bindings：
    # 角色必须存在于 unified_profile）。
    suggested_role: str = Field(min_length=1, max_length=60)
    column_mapping: dict[str, str]
    materials: list[MaterialProposal] = Field(default_factory=list)
    fields: list[FieldProposal] = Field(default_factory=list)
    rules: list[RuleProposal] = Field(default_factory=list)
    report_sections: list[ReportSectionProposal] = Field(default_factory=list)
    disabled_rows: list[DisabledSourceRow] = Field(default_factory=list)
    questions: list[BusinessQuestion] = Field(default_factory=list)


class CoverageRow(StrictModel):
    """一条来源业务记录的处置。disposition 是该行的主状态：

    CANDIDATE = 已形成候选；DISABLED = 停用且保留原始行；
    EXCLUDED = 明确排除并给出原因；QUESTION = 有业务内容但未形成候选。
    一行既形成候选又留下待处理问题时记 CANDIDATE，question_count 反映未决数量。
    """

    row_no: int = Field(ge=1)
    disposition: Literal["CANDIDATE", "DISABLED", "EXCLUDED", "QUESTION"]
    reason: str | None = None
    candidate_count: int = Field(default=0, ge=0)
    question_count: int = Field(default=0, ge=0)
    # 多行合并成一条候选时写目标 candidate_ref；当前契约是一行一条记录，故通常为 None。
    merged_into: str | None = None
    notes: list[str] = Field(default_factory=list)


class CoverageSheet(StrictModel):
    sheet_key: str
    sheet_name: str
    business_rows: int = Field(ge=0)
    rows: list[CoverageRow] = Field(default_factory=list)


class CoverageTotals(StrictModel):
    business_rows: int = Field(default=0, ge=0)
    candidate_rows: int = Field(default=0, ge=0)
    disabled_rows: int = Field(default=0, ge=0)
    excluded_rows: int = Field(default=0, ge=0)
    question_rows: int = Field(default=0, ge=0)


def coverage_totals(sheets: list[dict[str, Any]]) -> dict[str, int]:
    counts = Counter(row["disposition"] for sheet in sheets for row in sheet["rows"])
    return {
        "business_rows": sum(sheet["business_rows"] for sheet in sheets),
        "candidate_rows": counts["CANDIDATE"],
        "disabled_rows": counts["DISABLED"],
        "excluded_rows": counts["EXCLUDED"],
        "question_rows": counts["QUESTION"],
    }


class CoverageReport(StrictModel):
    """逐业务行覆盖对账：表头行之后的每条非空业务行都必须有明确处置，不得静默丢弃。"""

    sheets: list[CoverageSheet] = Field(default_factory=list)
    totals: CoverageTotals = Field(default_factory=CoverageTotals)


class CalculationCandidate(StrictModel):
    """一条计算规则候选：定义 + 参数 + 判定区间。

    它跨 Sheet（定义 / 参数 / 判定三张表按 rule_code 关联），因此挂在 bundle 顶层
    而不是某一张 Sheet 上。编译期由 manifest.compile_calculations 把它变成
    已发布配置里的计算段（含受限 AST）。

    这里只声明**结构契约**，业务语义（单位、期间、阈值）一律来自配置，
    本契约不提供任何默认值。
    """

    candidate_ref: str
    rule_code: str = Field(min_length=1, max_length=80)
    rule_name: str = Field(min_length=1, max_length=255)
    calculation_type: Literal["RATIO", "AMOUNT", "GROWTH", "DIFFERENCE"]
    formula: str = Field(min_length=1, max_length=2000)
    result_code: str = Field(min_length=1, max_length=80)
    result_type: Literal["DECIMAL", "PERCENT", "INTEGER", "AMOUNT"]
    unit: str | None = None
    currency: str | None = None
    # 「结果期间」：这条计算**算出来的数**属于哪一期，不是取数期间（取数期间在字段上）。
    # 跨期指标（(本期-上期)/上期）不属于任何单一期间，因此必须由业务声明，不能推断。
    period_policy: PeriodScope
    # 计算结果的报表口径。派生事实按它落库，下游引用与报告都读它。
    # 不给默认值：合并口径与母公司口径是不同的数，猜错会让下游引用到另一个数。
    statement_scope: StatementScope
    status: Literal["CLEAR", "PENDING", "DISABLED"]
    remark: str = ""
    parameters: list[dict[str, Any]] = Field(default_factory=list)
    bands: list[dict[str, Any]] = Field(default_factory=list)
    depends_on_result_codes: list[str] = Field(default_factory=list)
    # 公式里引用了但当前未定义的标识符。转换期收集、发布期阻断：
    # 有值时这条计算规则不能发布。
    unresolved_identifiers: list[str] = Field(default_factory=list)
    # 这条计算规则落在哪个**计算节点实例**上。计算三表按 Sheet 绑定到节点实例，
    # 因此实例码随候选一起带过来；同一场景可以有两个计算节点（两张报表各一个），
    # 用能力类型 `CALCULATION` 当实例码会把两条计算都挂到同一个节点上。
    node_code: str = Field(min_length=1, max_length=80)
    node_type: str = Field(default="CALCULATION", max_length=40)
    # 业务说明与来源原文：随候选一起保存，导出与页面展示要看到业务真正写下的内容。
    # 它们是**配置事实**，不参与执行判定，因此不进入已发布配置的计算段。
    category: str = ""
    description: str = ""
    risk_level_text: str = ""
    bands_text: str = ""
    verification_logic: str = ""
    review_advice: str = ""
    source: SourceRef


class ProposalBundle(StrictModel):
    sheets: list[SheetProposal]
    coverage: CoverageReport = Field(default_factory=CoverageReport)
    # 计算规则候选：财报场景有，合同审核路径为空。
    calculations: list[CalculationCandidate] = Field(default_factory=list)


ALLOWED_OPERATORS = set(AllowedOperator.__args__)
