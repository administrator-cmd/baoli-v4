from collections import Counter
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from .parameter_contract import ReportContentType, ReportResultSort

# 普通数值 DECIMAL 不带财务假设；AMOUNT 与大写金额 DECIMAL_CN 保留单位、币种口径。
ValueType = Literal["STRING", "DECIMAL", "AMOUNT", "DECIMAL_CN", "DATE", "BOOLEAN", "CLAUSE"]
# 业务适用条件与来源标签按规则原文保留。
ApplicabilityKind = str
# 取数期间：同一个科目在不同期间是不同的数；实际涉及期间时由字段声明，
# 不涉及则归一为 NOT_APPLICABLE。计算参数不再重复声明这一项。
PeriodScope = str
# 报表口径：同一科目在合并报表与母公司报表里是不同的数；实际涉及时报表字段声明。
StatementScope = str

# 不涉及相应口径的业务无需填写，字段与计算结果内部归一为 NOT_APPLICABLE。
FieldPeriodScope = str
FieldStatementScope = str


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




class RuleProposal(StrictModel):
    candidate_ref: str
    # 业务规则编号：来自规则库实际提供的编号，不要求固定列名或编号前缀。
    # 业务编号与内部 rule_key 分开保存。
    # 规则库未提供编号时为 None，不用派生值冒充业务编号。
    rule_code: str | None = Field(default=None, max_length=80)
    source_id: str | None = None
    name: str
    # 风险来源的执行编号：可引用上游计算结果。它与 source_rule_ref 的候选引用不同，
    # 不按同表或跨表区分；候选引用只在本次导入内部有效。
    source_rule_codes: list[str] = Field(default_factory=list)
    # 业务动作名称仅作说明，不按固定节点或算子决定执行路径。
    operator: str = "LLM_EXECUTE"
    inputs: list[RuleInputProposal] = Field(default_factory=list)
    output_field_refs: list[str] = Field(default_factory=list)
    source_rule_ref: str | None = None
    material_refs: list[str] = Field(default_factory=list)
    parameters: dict[str, Any] = Field(default_factory=dict)
    # 「审核要求」原文由后端从已映射的单元格确定性读取，模型不改写、不省略。
    requirement: str = ""
    applicability: ApplicabilityProposal | None = None
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
    section_order: int | None = None
    content_type: str | None = None
    result_sort: str | None = None
    # 按风险从高到低排列的业务等级原文；没有明确顺序时不推测。
    risk_level_order: list[str] = Field(default_factory=list)
    config_status: str | None = None
    source: SourceRef


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
    # 停用候选原样存档，不解析它的执行结构。
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


class CoverageRow(StrictModel):
    """一条来源业务记录的处置。disposition 是该行的主状态：

    CANDIDATE = 已形成候选；DISABLED = 停用且保留原始行；
    EXCLUDED = 明确排除并给出原因；UNPARSED = 有业务内容但未形成候选。
    一行既有候选又有未导入项时记 CANDIDATE，具体失败保留在 notes 和 rejected_candidates。
    """

    row_no: int = Field(ge=1)
    disposition: Literal["CANDIDATE", "DISABLED", "EXCLUDED", "UNPARSED"]
    reason: str | None = None
    candidate_count: int = Field(default=0, ge=0)
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
    unparsed_rows: int = Field(default=0, ge=0)


def coverage_totals(sheets: list[dict[str, Any]]) -> dict[str, int]:
    counts = Counter(row["disposition"] for sheet in sheets for row in sheet["rows"])
    return {
        "business_rows": sum(sheet["business_rows"] for sheet in sheets),
        "candidate_rows": counts["CANDIDATE"],
        "disabled_rows": counts["DISABLED"],
        "excluded_rows": counts["EXCLUDED"],
        "unparsed_rows": counts["UNPARSED"],
    }


class CoverageReport(StrictModel):
    """逐来源行覆盖对账：每条非空行均须有明确处置，表头和说明可显式排除。"""

    sheets: list[CoverageSheet] = Field(default_factory=list)
    totals: CoverageTotals = Field(default_factory=CoverageTotals)
    # 无法形成配置的单项保留原始响应与错误，不混入可执行候选或伪装成原始停用行。
    rejected_candidates: list[dict[str, Any]] = Field(default_factory=list)


class CalculationCandidate(StrictModel):
    """计算业务定义；LLM 根据公式和完整要求执行，不使用固定计算/判定协议。"""

    candidate_ref: str
    rule_code: str = Field(min_length=1, max_length=80)
    rule_name: str = Field(min_length=1, max_length=255)
    formula: str
    requirement: str = ""
    # 任意场景的业务补充信息，保留原文标签与内容，不要求固定分档字段。
    business_context: dict[str, Any] = Field(default_factory=dict)
    result_code: str = Field(min_length=1, max_length=80)
    result_type: str
    unit: str | None = None
    currency: str | None = None
    period_policy: str | None = None
    statement_scope: str | None = None
    status: Literal["CLEAR", "PENDING", "DISABLED"]
    remark: str = ""
    parameters: list[dict[str, Any]] = Field(default_factory=list)
    depends_on_result_codes: list[str] = Field(default_factory=list)
    source: SourceRef


class ProposalBundle(StrictModel):
    sheets: list[SheetProposal]
    coverage: CoverageReport = Field(default_factory=CoverageReport)
    # 计算规则候选：任何场景均可按业务需要配置。
    calculations: list[CalculationCandidate] = Field(default_factory=list)
