from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.constants.execution import OPERATOR_LLM_EXECUTE
from app.domain.configuration.execution_mode import ExecutionMode

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


class SourceRowReference(BaseModel):
    """模型定位真实原文，不复制或改写原始单元格。"""

    sheet_name: str
    row_no: int


class SourceOrigin(SourceRowReference):
    """主来源行与补充来源行；原始行由程序从工作簿附上。"""

    model_config = ConfigDict(extra="allow")

    related_rows: list[SourceRowReference] = Field(
        default_factory=list,
        description="仅列出参与该定义的其他真实来源行，例如表头、跨行定义或共享业务说明。",
    )


class ProposalModel(BaseModel):
    """读取配置所需字段，模型补充的业务属性原样保留。"""

    model_config = ConfigDict(extra="allow")
    business_context: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "业务原文中无法由专用字段表达的补充属性。"
            "建议使用 source_attributes 保存原文标签和值，不能重复 value_type、required、"
            "extraction_strategy 或 instruction；不要把空白单元格改写成说明。"
        ),
    )


class SourcedProposal(ProposalModel):
    """五类业务对象保留来源；参数和引用对象不重复附加来源。"""

    origin: SourceOrigin | None = None


class BusinessAttribute(BaseModel):
    """Excel 中明确出现、但不直接驱动执行的一个业务属性。"""

    model_config = ConfigDict(extra="forbid")

    label: str
    value: Any


class FieldBusinessContext(BaseModel):
    """字段的补充业务事实；与字段抽取要求分开保存。"""

    model_config = ConfigDict(extra="allow")

    source_attributes: list[BusinessAttribute] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class MaterialProposal(SourcedProposal):
    candidate_ref: str
    source_id: str | None = None
    name: str
    required: bool
    # 「业务说明」列是业务含义的一部分，必须随材料一起保存，不能接收后丢弃。
    description: str = ""
    # 业务配置状态：已明确 / 待确认 / 停用。它不是 PM 原状态，也不是系统运行或发布状态；
    # PENDING 可保存和发布，但确认启用前暂不执行。
    config_status: str | None = None


class FieldProposal(SourcedProposal):
    business_context: FieldBusinessContext = Field(
        default_factory=FieldBusinessContext,
        description=(
            "字段的非执行业务属性；只保存 Excel 明确给出的标签和值。"
            "抽取动作必须放 instruction，不能复制到这里。"
        ),
    )
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
    instruction: str = Field(
        ...,
        description=(
            "简洁的字段抽取要求，只说明从哪个材料抽取什么内容以及实际的取值或标准化规则。"
            "不得重复字段类型、是否必需、抽取策略、优先级、生效日期或缺失说明。"
        ),
    )
    # 产物来源：该字段的取值由哪种处理产物提供（当前仅印章识别需要显式声明）。
    # 主体核对必须按这一声明确定「待核对主体取自哪份材料的哪种产物」，
    # 不能靠字段名里是否含某些汉字来猜。留空表示字段由常规字段抽取提供。
    artifact_source: str | None = None
    extraction_strategy: Literal["RAW_TEXT", "DETERMINISTIC", "STRUCTURED"] | None = None
    structure_schema: dict[str, Any] | None = None
    # 取数期间、报表口径与主体范围：由字段自己声明。计算参数不再重复声明这几项，
    # 因此这里是「这个数取的是哪一期、哪个口径、哪个主体」的唯一来源。
    # 只有规则实际涉及这些口径时才填写；省略表示不涉及，由编译层归一为 NOT_APPLICABLE / SELF。
    period_scope: FieldPeriodScope | None = Field(
        default=None,
        description="该字段取数的期间或时点，例如本期、上期、期初、期末、2025-06-30；原文未说明时省略，不猜默认期间。",
    )
    statement_scope: FieldStatementScope | None = Field(
        default=None,
        description="该字段取数的报表口径，例如合并报表、母公司报表；原文未说明时省略。",
    )
    entity_scope: str | None = Field(
        default=None,
        description="该字段取数的主体范围，例如母公司、子公司、合并主体；原文未说明时省略，编译层按 SELF 处理。",
    )
    config_status: str | None = None


class RuleInputProposal(ProposalModel):
    field_ref: str
    material_ref: str


class ApplicabilityProposal(ProposalModel):
    """规则的结构化适用条件；比较规则留空表示始终适用。"""

    kind: ApplicabilityKind




class RuleProposal(SourcedProposal):
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
    operator: str = OPERATOR_LLM_EXECUTE
    # 执行方式决定执行器给模型哪些上下文、允许做什么；它只描述动作，不描述业务。
    # 由解析模型按原行语义填写；无法判定时为 UNKNOWN，不能用 CHECK 顶替。
    execution_mode: ExecutionMode = Field(
        default="UNKNOWN",
        description=(
            "MATERIAL_CHECK=检查材料/文件本身；VALUE_COMPARE=检查同一范围内的字段值；"
            "CROSS_SCOPE_COMPARE=跨材料/期间/口径比较；AGGREGATE=只汇总其他规则结果；"
            "DERIVE_RISK=只根据其他规则结果推导风险；EXTRACT=抽取字段；"
            "CALCULATE=计算公式；REPORT=生成报告；UNKNOWN=无法判定。"
        ),
    )
    # 业务角色由 LLM 按原文明确声明，判断条件非空不能代表风险规则。
    rule_role: Literal["CHECK", "RISK"] = "CHECK"
    inputs: list[RuleInputProposal] = Field(default_factory=list)
    output_field_refs: list[str] = Field(default_factory=list)
    source_rule_ref: str | None = None
    material_refs: list[str] = Field(default_factory=list)
    parameters: dict[str, Any] = Field(default_factory=dict)
    # 执行要求与原始来源分开保存。
    requirement: str = ""
    applicability: ApplicabilityProposal | None = None
    # 以下字段是 Excel 场景配置事实，由转换候选保留：
    #   config_status：业务配置状态（已明确 / 待确认 / 停用）；
    #   check_operator：业务填写的「合格要求」原文取值，用于导出与预览；
    #   check_level / risk_level_value：检查等级与风险等级原文，两者独立保留、不相互覆盖；
    #   risk_names / source_rule_refs：前者保存关联风险编号；后者保存风险来源规则引用；
    #   risk_condition：风险成立条件完整原文；
    # 这些是业务配置事实而非执行参数，因此不进入 parameters 契约。
    config_status: str | None = None
    check_operator: str | None = None
    check_level: str | None = None
    risk_level_value: str | None = None
    risk_condition: str | None = None
    risk_names: list[str] = Field(default_factory=list)
    source_rule_refs: list[str] = Field(default_factory=list)


class ReportSectionProposal(SourcedProposal):
    candidate_ref: str
    section_name: str
    field_refs: list[str] = Field(
        default_factory=list,
        description="报告业务数据引用：字段或普通规则的 candidate_ref、计算的 result_code；不明确的原引用也保留。",
    )
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


class InputGap(ProposalModel):
    """计算定义内部的未绑定输入；所属计算由所在对象确定。"""

    parameter_code: str | None = None
    missing_input: str
    reason: str
    original_requirement: str = ""


class BindingGap(InputGap):
    """解析阶段已确认存在、但尚未建立输入绑定的取数需求。

    它不是规则状态，也不改变启用/停用：只说明公式或检查要求中的哪项输入没有
    对应字段、计算结果或常量，以及原文为什么需要它。执行时据此拒绝把缺项当成 0。
    """

    owner_group: Literal["rules", "calculations"]
    owner_ref: str = Field(description="所属对象的 candidate_ref（内部引用），不是业务编号。")
    owner_name: str | None = None


class CalculationCandidate(SourcedProposal):
    """计算业务定义；LLM 根据公式和完整要求执行，不使用固定计算/判定协议。"""

    candidate_ref: str
    source_id: str | None = Field(default=None, description="Excel 原始业务编号；没有编号时为 null，不用内部引用冒充。")
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
    # 第二轮逐项核对公式输入后的未绑定项；空列表表示所有输入已经建立绑定。
    input_gaps: list[InputGap] = Field(default_factory=list)
