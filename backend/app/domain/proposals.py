from collections import Counter
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .calculation.schema import assert_llm_engine
from .parameter_contract import REPORT_SHARED_PROPERTIES, ReportContentType, ReportResultSort

# 普通数值 DECIMAL 不带财务假设；AMOUNT 与大写金额 DECIMAL_CN 保留单位、币种口径。
ValueType = Literal["STRING", "DECIMAL", "AMOUNT", "DECIMAL_CN", "DATE", "BOOLEAN", "CLAUSE"]
# 当前只支持这两种适用条件；其他复杂条件必须明确指出未支持，不能由模型自行改变适用范围。
ApplicabilityKind = Literal["ALWAYS", "MATERIALS_UPLOADED"]
# 业务问题的作答形式：选择题 / 数值输入 / 文本输入。缺少阈值等参数时用输入控件，不编造选项。
QuestionInputKind = Literal["SELECT", "DECIMAL", "TEXT"]
# 能力缺口声明码，包含历史配置使用的取数/汇总代码。
# 是否可执行取决于实际算子及输入来源，不能仅因业务涉及汇总或常量就声明不支持。
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
# 历史风险来源能力标签；可选声明按来源规则的实际节点核对，不按工作表归属核对。
# 计算结果通过 source_rule_codes 引用，不使用此标签表达。
SourceNodeKind = Literal["MATERIAL_QUALITY", "INTERNAL_VALIDATE", "CROSS_VALIDATE"]
# 取数期间：同一个科目在不同期间是不同的数；实际涉及期间时由字段声明，
# 不涉及则归一为 NOT_APPLICABLE。计算参数不再重复声明这一项。
PeriodScope = Literal["CURRENT", "PRIOR", "OPENING", "CLOSING", "AVERAGE_OPENING_CLOSING", "NOT_APPLICABLE"]
# 报表口径：同一科目在合并报表与母公司报表里是不同的数；实际涉及时报表字段声明。
StatementScope = Literal["CONSOLIDATED", "PARENT", "NOT_APPLICABLE"]

# 不涉及相应口径的业务无需填写，字段与计算结果内部归一为 NOT_APPLICABLE。
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
    # PENDING 仅保留原始说明，不构成业务审批或发布、执行限制。
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
    node_code: str | None = None
    material_ref: str
    required: bool
    instruction: str
    # 产物来源：该字段的取值由哪种处理产物提供（当前仅印章识别需要显式声明）。
    # 主体核对必须按这一声明确定「待核对主体取自哪份材料的哪种产物」，
    # 不能靠字段名里是否含某些汉字来猜。留空表示字段由常规字段抽取提供。
    artifact_source: str | None = None
    extraction_strategy: Literal["RAW_TEXT", "DETERMINISTIC", "STRUCTURED"] | None = None
    structure_schema: dict[str, Any] | None = None
    # 取数期间与报表口径：由字段自己声明。计算参数不再重复声明这两项，
    # 因此这里是「这个数取的是哪一期、哪个口径」的唯一来源。
    # 只有规则实际涉及期间/报表口径时才填写；省略表示不涉及，由编译层归一为 NOT_APPLICABLE。
    period_scope: FieldPeriodScope | None = None
    statement_scope: FieldStatementScope | None = None
    config_status: str | None = None
    source: SourceRef


class RuleInputProposal(StrictModel):
    field_ref: str
    material_ref: str


class ApplicabilityProposal(StrictModel):
    """规则的结构化适用条件；比较规则留空表示始终适用。"""

    kind: ApplicabilityKind


class UnsupportedCapability(StrictModel):
    """当前算子与真实输入无法满足的业务要求。

    LLM_VALIDATE 可按已提供的字段、结构化记录和常量进行汇总、条件计算或比较，
    这些业务方法本身不构成能力缺口。确需未提供的外部数据或未实现能力时才声明，
    后端对显式声明报错，不丢弃缺口或用虚构输入生成可执行规则。
    """

    code: UnsupportedCapabilityCode
    detail: str = Field(min_length=1)


class RuleProposal(StrictModel):
    candidate_ref: str
    # 业务规则编号：来自规则库实际提供的编号，不要求固定列名或编号前缀。
    # 它是新版本的规则身份；rule_key 只是历史身份，不再由这里派生。
    # 规则库未提供编号时为 None，不用派生值冒充业务编号。
    rule_code: str | None = Field(default=None, max_length=80)
    source_id: str | None = None
    name: str
    # 风险来源的执行编号：可引用上游计算结果。它与 source_rule_ref 的候选引用不同，
    # 不按同表或跨表区分；候选引用只在本次导入内部有效。
    source_rule_codes: list[str] = Field(default_factory=list)
    # 规则归属的节点实例码（当前场景 config_json.nodes 中的 node_code）。
    # 它不再是能力类型：同一场景可以有两个同类节点（两张报表各一个字段抽取），
    # 规则必须说清自己落在哪一个实例上。能力类型在同一条规则的 `node_type` 里。
    node: str = Field(min_length=1, max_length=80)
    # 节点能力类型用于执行分发，实例 node 决定实际归属，与 Excel 表结构无关。
    node_type: str | None = Field(default=None, max_length=40)
    operator: AllowedOperator
    inputs: list[RuleInputProposal] = Field(default_factory=list)
    output_field_refs: list[str] = Field(default_factory=list)
    source_rule_ref: str | None = None
    # 风险疑点的来源节点；转换候选可声明，发布时按来源规则的实际节点核对。
    source_node: SourceNodeKind | None = None
    material_refs: list[str] = Field(default_factory=list)
    parameters: dict[str, Any] = Field(default_factory=dict)
    # 「审核要求」原文由后端从已映射的单元格确定性读取，模型不改写、不省略。
    requirement: str = ""
    applicability: ApplicabilityProposal | None = None
    # 能力说明仅供查看，不阻断导入或发布；执行时按实际能力处理。
    unsupported_capability: UnsupportedCapability | None = None
    # 以下字段是 Excel 场景配置事实，由转换候选保留，后端保留原单元格并校验：
    #   config_status：业务配置状态（已明确 / 待确认 / 停用）；
    #   check_operator：业务填写的「合格要求」原文取值，用于导出与预览；
    #   check_level / risk_level_value：检查等级与风险等级原文，两者独立保留、不相互覆盖；
    #   risk_names / source_rule_refs：前者沿用历史键名，现存关联风险编号；后者是风险来源检查引用；
    #   risk_condition：风险成立条件完整原文；
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



class ReportSectionProposal(StrictModel):
    candidate_ref: str
    section_name: str
    field_refs: list[str] = Field(default_factory=list)
    content_instruction: str
    # 场景报告配置：一份报告可由多个章节组成，
    # 同一报告编号的名称、材料范围、输出格式、结果排序必须完全一致。
    report_code: str | None = None
    report_name: str | None = None
    material_refs: list[str] = Field(default_factory=list)
    formats: list[str] = Field(default_factory=list)
    section_order: int | None = Field(default=None, ge=1)
    content_type: ReportContentType | None = None
    result_sort: ReportResultSort | None = None
    # 按风险从高到低排列的业务等级原文；没有明确顺序时不推测。
    risk_level_order: list[str] = Field(default_factory=list)
    config_status: str | None = None
    source: SourceRef


def report_property_target_refs(
    sections: dict[str, dict[str, Any]], target_ref: str, property_name: str,
) -> list[str]:
    """按明确报告编号定位共享属性的所有章节；无编号或章节专属属性只作用于原目标。"""
    target = sections[target_ref]
    code = target.get("report_code")
    if property_name not in REPORT_SHARED_PROPERTIES or not isinstance(code, str) or not code.strip():
        return [target_ref]
    return [
        ref for ref, section in sections.items()
        if isinstance(section.get("report_code"), str) and section["report_code"].strip() == code.strip()
    ]


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
    target_type: Literal["MATERIAL", "FIELD", "RULE", "REPORT_SECTION", "CALC_RULE"]
    target_ref: str
    property: str
    # 缺少阈值、口径一类数值时用输入控件作答，不为了凑够两个选项而编造候选值。
    input_kind: QuestionInputKind = "SELECT"
    options: list[QuestionOption] = Field(default_factory=list)
    # 问题作答的应用方式；Excel 转换模型可声明 SET_PARAMETER，后端核对目标后应用。
    resolve: AnswerResolution | None = None
    source: SourceRef
    # 后端标注不能应用为配置修改的说明，保留原文且不阻断整次导入。
    resolution_notice: str | None = None


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
    # 旧模型返回的停用候选原样存档，不解析它的执行结构。
    raw_configuration: dict[str, Any] | None = None


class SheetProposal(StrictModel):
    sheet_key: str
    # 保留候选集合的来源角色标记；动态规则库不按参考模板角色或列匹配。
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
    # 多行合并成一条候选时写目标 candidate_ref；每个来源行仍分别记录处置。
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
    """逐来源行覆盖对账：每条非空行均须有明确处置，表头和说明可显式排除。"""

    sheets: list[CoverageSheet] = Field(default_factory=list)
    totals: CoverageTotals = Field(default_factory=CoverageTotals)
    # 无法形成配置的单项保留原始响应与错误，不混入可执行候选或伪装成原始停用行。
    rejected_candidates: list[dict[str, Any]] = Field(default_factory=list)


class CalculationCandidate(StrictModel):
    """一条计算规则候选：定义 + 参数 + 判定区间。

    定义、参数与判定依据可来自同一表或多张表，因此挂在 bundle 顶层，
    每项保留真实来源。编译期由 manifest.compile_calculations 把它变成
    已发布配置里的业务定义、输入定位与依赖。

    这里只声明结构契约，业务语义（单位、期间、阈值）一律来自配置；
    期间仅在规则实际涉及时填写，缺省由编译层归一为不适用。历史候选的计算用途沿用 CHECK。
    """

    candidate_ref: str
    rule_code: str = Field(min_length=1, max_length=80)
    rule_name: str = Field(min_length=1, max_length=255)
    calculation_type: str = Field(min_length=1)
    execution_engine: Literal["LLM"]
    # 历史候选按原契约计算并判定；自由规则库模型可明确声明用途。
    execution_mode: Literal["CHECK", "VALUE_ONLY"] = "CHECK"
    formula: str = Field(min_length=1)
    judgement_instruction: str = ""
    result_code: str = Field(min_length=1, max_length=80)
    result_type: Literal["DECIMAL", "PERCENT", "INTEGER", "AMOUNT"]
    unit: str | None = None
    currency: str | None = None
    # 「结果期间」：这条计算算出来的数属于哪一期，不是取数期间（取数期间在字段上）。
    # 跨期指标可以省略结果期间；省略表示该结果不适用期间口径。
    period_policy: PeriodScope | None = None
    # 计算结果的报表口径。派生事实按它落库，下游引用与报告都读它。
    # 只有规则实际涉及报表口径时才填写；省略表示不涉及，由编译层归一为 NOT_APPLICABLE。
    statement_scope: StatementScope | None = None
    status: Literal["CLEAR", "PENDING", "DISABLED"]
    remark: str = ""
    parameters: list[dict[str, Any]] = Field(default_factory=list)
    bands: list[dict[str, Any]] = Field(default_factory=list)
    depends_on_result_codes: list[str] = Field(default_factory=list)
    # 保留历史候选记录的未解析参数问题；新 LLM 配置不通过公式词法分析生成它。
    unresolved_identifiers: list[str] = Field(default_factory=list)
    # 计算引用当前场景的实际节点实例，不能用 CALCULATION 能力类型替代实例码。
    node_code: str = Field(min_length=1, max_length=80)
    node_type: str = Field(default="CALCULATION", max_length=40)
    # 业务说明与来源原文随候选及发布定义保存，供 LLM 使用和页面、导出展示。
    category: str = ""
    description: str = ""
    risk_level_text: str = ""
    bands_text: str = ""
    verification_logic: str = ""
    review_advice: str = ""
    source: SourceRef

    @model_validator(mode="before")
    @classmethod
    def validate_engine(cls, value: Any) -> Any:
        if isinstance(value, dict):
            assert_llm_engine(str(value.get("rule_code") or ""), value.get("execution_engine"))
        return value


class ProposalBundle(StrictModel):
    sheets: list[SheetProposal]
    coverage: CoverageReport = Field(default_factory=CoverageReport)
    # 计算规则候选：任何场景均可按业务需要配置。
    calculations: list[CalculationCandidate] = Field(default_factory=list)
