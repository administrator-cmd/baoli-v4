"""审核能力目录：后端能力的单一事实来源。

这个模块是**叶子模块**：不导入 manifest、node_rulebook 或任何 services，
供它们共同引用。它只声明事实，不执行判定。

为什么必须有一份目录
--------------------
在此之前，「有哪些能力、能挂在哪个节点、输入是什么、参数怎么填、取证与判定怎么做、
是否已经实现」这六件事散落在 manifest、operators.REGISTRY、前端下拉与 quality_checks
至少七处，彼此互不校验，已经出现过真实漂移：后端允许 12 个算子而页面下拉只有 7 个；
route.supported 与 report.format_support 写了却全仓无人读；大小写金额检查
（DECIMAL_CN_EQUAL）未实现却不在能力缺口里，只在运行时返回不可判定。

本目录把上述事实收敛到一处。其他模块从这里派生，不再各写一份名单。
`services.audit.rules` 按 capability_id 分发执行，不再按算子码写分支。

边界
----
- 本模块**不**声明业务阈值、默认方向、默认单位或默认币种：那些只能来自业务配置。
- 本模块**不**为未实现的能力提供默认 handler 或兜底分支：未实现就是未实现，
  它会在导入/发布阶段作为能力缺口明确暴露，在运行时明确返回「不可判定」。
- `implemented=False` 表示当前**没有**真实判定实现，不表示「默认通过」。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from . import file_quality


# 能力版本：能力语义发生变化（参数、输出结构、判定口径）时递增。
# 已发布配置与历史任务按发布时刻冻结的版本执行，不随本模块的修改被重新解释。
CAPABILITY_VERSION = "1"

# 能力种类。算子级能力由规则行直接引用；文件检查子项由「检查项目」列引用。
KIND_OPERATOR = "OPERATOR"
KIND_QUALITY_ITEM = "QUALITY_ITEM"

# 输入形态。执行端据此决定需要准备哪些输入，不再按算子码猜。
INPUT_NONE = "NONE"                 # 无字段输入（风险、报告）
INPUT_FIELD_ONE = "FIELD_ONE"       # 恰好一个字段
INPUT_FIELD_PAIR = "FIELD_PAIR"     # 恰好两个字段
INPUT_FIELD_LIST = "FIELD_LIST"     # 左侧一个字段 + 至少一个加数
INPUT_OUTPUT_FIELDS = "OUTPUT_FIELDS"  # 产出字段（字段抽取）
INPUT_MATERIAL_SCOPE = "MATERIAL_SCOPE"  # 按材料位判定，无字段输入

# 执行方式。审核规则执行层按这个字段分发，不按算子码写 if/elif。
EXEC_OPERATOR = "OPERATOR"                # 直接调用 operators.REGISTRY
EXEC_LLM_VALIDATE = "LLM_VALIDATE"        # 检索证据 + 语义判断
EXEC_SUBJECT_RETRIEVAL = "SUBJECT_RETRIEVAL"  # 主体检索返回候选，结论交人工
EXEC_RISK = "RISK_TRIGGER"                # 组合来源检查结果
EXEC_MATERIAL_EXISTS = "MATERIAL_EXISTS"  # 材料位齐全性
EXEC_FILE_QUALITY = "FILE_QUALITY_CHECK"  # 文件质量逐份判定
EXEC_LLM_EXTRACT = "LLM_EXTRACT"          # 字段抽取
EXEC_REPORT = "REPORT_RENDER"             # 报告渲染

# 取证方式：按材料与字段直接读取结构化事实，或在材料文本中检索相关证据。
EVIDENCE_DIRECT = "DIRECT_FACTS"
EVIDENCE_RETRIEVAL = "TASK_RETRIEVAL"
# 主体检索：只对印章识别产物中的**主体名称**建向量，用待核对主体查相似印章主体。
# 它不是全文检索：不检索合同正文，也不检索条款。
EVIDENCE_SEAL_SUBJECT = "SEAL_SUBJECT_RETRIEVAL"

# 判定方式：确定性程序 / 大模型语义判断 / 需要人工裁决。
JUDGE_PROGRAM = "PROGRAM"
JUDGE_LLM = "LLM"
JUDGE_HUMAN = "HUMAN_REVIEW"

# 规则结果可能出现的状态。执行端与页面共用这一份集合：
# 不在集合里的状态码必须显式暴露，不能被页面当成「未开始」静默吞掉。
RULE_STATUSES: tuple[str, ...] = (
    "PASSED",
    "ABNORMAL",
    "EVIDENCE_INSUFFICIENT",
    "NOT_APPLICABLE",
    "NOT_EXECUTED",
    "PENDING_REVIEW",
    "REVIEWED",
    "SKIPPED",
    "RUNNING",
)

# 状态分类：页面分档与统计共用同一口径。
#   已判定：系统或人工已经给出可用的业务结论（含人工改判后的「已复核」）。
#   未判定：应当判定但还没有形成可用结论（证据不足、待复核、未执行、执行中、已跳过）。
#   不适用：适用条件明确不成立，本次就不该判定。
# 「未执行 / 已跳过 / 未配置」与「证据不足」都不能算合格，也不能互相冒充。
STATUS_CATEGORY_DECIDED = "DECIDED"
STATUS_CATEGORY_UNDECIDED = "UNDECIDED"
STATUS_CATEGORY_NOT_APPLICABLE = "NOT_APPLICABLE"
STATUS_CATEGORY_UNKNOWN = "UNKNOWN"

STATUS_CATEGORIES: dict[str, str] = {
    "PASSED": STATUS_CATEGORY_DECIDED,
    "ABNORMAL": STATUS_CATEGORY_DECIDED,
    "REVIEWED": STATUS_CATEGORY_DECIDED,
    "EVIDENCE_INSUFFICIENT": STATUS_CATEGORY_UNDECIDED,
    "PENDING_REVIEW": STATUS_CATEGORY_UNDECIDED,
    "NOT_EXECUTED": STATUS_CATEGORY_UNDECIDED,
    "RUNNING": STATUS_CATEGORY_UNDECIDED,
    "SKIPPED": STATUS_CATEGORY_UNDECIDED,
    "NOT_APPLICABLE": STATUS_CATEGORY_NOT_APPLICABLE,
}

# 状态的标准中文含义。它是**系统对状态码的解释**，与业务话术无关；
# 页面显示状态时用这一份，未知状态码明确标为未识别，不用兜底文案冒充正常。
RULE_STATUS_LABELS: dict[str, str] = {
    "PASSED": "合格",
    "ABNORMAL": "不合格",
    "EVIDENCE_INSUFFICIENT": "无法判断（证据或能力不足）",
    "NOT_APPLICABLE": "不适用",
    "NOT_EXECUTED": "未执行",
    "PENDING_REVIEW": "待人工复核",
    "REVIEWED": "已人工复核",
    "SKIPPED": "已跳过（本模板未配置此类检查）",
    "RUNNING": "执行中",
}

STATUS_CATEGORY_LABELS: dict[str, str] = {
    STATUS_CATEGORY_DECIDED: "已判定",
    STATUS_CATEGORY_UNDECIDED: "尚未判定",
    STATUS_CATEGORY_NOT_APPLICABLE: "不适用",
    STATUS_CATEGORY_UNKNOWN: "未识别状态",
}

# 每种「未判定」到底缺什么。统一写成「证据不足」会让人不知道该去补哪一步。
STATUS_NOTES: dict[str, str] = {
    "EVIDENCE_INSUFFICIENT": "尚未形成可用结论，具体缺什么见该条结果的原因；它既不是合格，也不是不合格。",
    "PENDING_REVIEW": "系统已给出待确认的判断，等待人工确认后才是最终结论。",
    "NOT_EXECUTED": "该检查本次没有执行，不能据此认为检查通过。",
    "SKIPPED": "本模板未配置此类检查，节点显式跳过；跳过不等于通过。",
    "RUNNING": "该检查正在执行，尚未形成结果。",
    "NOT_APPLICABLE": "适用条件明确不成立，本次不该执行这条检查；不适用不等于通过。",
}

# 结果里能力专用扩展数据的命名空间。能力自有的键（actual_bytes、per_material、
# duplicates 等）只能放在这里，不能混进所有规则共有的公共结构。
EXTENSION_NAMESPACE_QUALITY = "file_quality"
EXTENSION_NAMESPACE_EXTRACTION = "extraction"
EXTENSION_NAMESPACE_RISK = "risk"


@dataclass(frozen=True)
class ParameterSpec:
    """配置参数契约：必填/可选键与填写口径说明。

    它描述的是**格式与类型**，不是业务取值：阈值、方向、单位、币种一律来自业务配置，
    没有默认值。`required` 里的键缺失时导入与发布都必须明确失败。
    """

    required: frozenset[str] = field(default_factory=frozenset)
    optional: frozenset[str] = field(default_factory=frozenset)
    notes: tuple[str, ...] = ()

    def to_payload(self) -> dict[str, Any]:
        return {
            "required": sorted(self.required),
            "optional": sorted(self.optional),
            "notes": list(self.notes),
        }


@dataclass(frozen=True)
class Capability:
    """一项审核能力。

    capability_id 是稳定标识：已发布的配置与历史任务按它引用能力，
    中文标签可以改，标识不能改。
    """

    capability_id: str
    label: str
    kind: str
    nodes: frozenset[str]
    input_kind: str
    parameters: ParameterSpec
    # 本条能力的结果里可能出现的状态。它是**声明**，不是承诺：
    # implemented=False 时执行期只会给出 EVIDENCE_INSUFFICIENT。
    statuses: frozenset[str]
    supports_evidence: bool
    supports_comparison: bool
    implemented: bool
    execution: str
    evidence: str
    judge: str
    output_keys: tuple[str, ...] = ()
    # 执行结果**保证出现**的输出键。工具目录据此生成输出 Schema 的 `required`：
    # 只列真正每条返回路径都会给出的键，没有验证过的能力留空。
    # 把「可能出现」当成「保证出现」会让工具结果校验放行缺字段的结果。
    required_output_keys: tuple[str, ...] = ()
    # 扩展数据所在的命名空间；没有能力专用数据时为 None。
    extension_namespace: str | None = None
    version: str = CAPABILITY_VERSION
    note: str = ""
    # 未实现的真实原因。implemented=True 时为空。
    unimplemented_reason: str = ""
    # 文件检查子项的所属算子。
    parent: str | None = None

    def to_payload(self) -> dict[str, Any]:
        return {
            "capability_id": self.capability_id,
            "label": self.label,
            "kind": self.kind,
            "parent": self.parent,
            "nodes": sorted(self.nodes),
            "input_kind": self.input_kind,
            "parameters": self.parameters.to_payload(),
            "output_keys": list(self.output_keys),
            "required_output_keys": list(self.required_output_keys),
            "statuses": sorted(self.statuses),
            "supports_evidence": self.supports_evidence,
            "supports_comparison": self.supports_comparison,
            "implemented": self.implemented,
            "unimplemented_reason": self.unimplemented_reason or None,
            "execution": self.execution,
            "evidence": self.evidence,
            "judge": self.judge,
            "extension_namespace": self.extension_namespace,
            "version": self.version,
            "note": self.note or None,
        }


_COMPARE_NODES = frozenset({"INTERNAL_VALIDATE", "CROSS_VALIDATE"})
_DECIDED_STATUSES = frozenset({"PASSED", "ABNORMAL", "EVIDENCE_INSUFFICIENT"})

# 算子级能力：字段顺序与含义固定，其余模块从这里派生节点约束、中文标签、路由与分发方式。

_OPERATOR_CAPABILITIES: tuple[Capability, ...] = (
    Capability(
        capability_id="MATERIAL_EXISTS",
        label="材料是否齐全",
        kind=KIND_OPERATOR,
        nodes=frozenset({"MATERIAL_CHECK"}),
        input_kind=INPUT_MATERIAL_SCOPE,
        parameters=ParameterSpec(),
        statuses=frozenset({"PASSED", "ABNORMAL", "NOT_APPLICABLE", "EVIDENCE_INSUFFICIENT"}),
        supports_evidence=False,
        supports_comparison=True,
        implemented=True,
        execution=EXEC_MATERIAL_EXISTS,
        evidence=EVIDENCE_DIRECT,
        judge=JUDGE_PROGRAM,
        output_keys=("applicability", "standard", "comparison", "missing_material_keys", "missing_material_names"),
        # 这五项由 `quality_judgement.material_exists_result` 在**每条返回路径**上给出：
        # 节点实现与 Agent adapter 都调它，因此「保证出现」是可核对的。
        required_output_keys=("applicability", "standard", "comparison", "missing_material_keys", "missing_material_names"),
        note="按规则声明的材料位逐项核对是否已上传；比较对象是「已上传的材料位」与「本检查声明的材料位」。",
    ),
    Capability(
        capability_id="FILE_QUALITY_CHECK",
        label="文件质量检查",
        kind=KIND_OPERATOR,
        nodes=frozenset({"DOCUMENT_PREPARE"}),
        input_kind=INPUT_MATERIAL_SCOPE,
        parameters=ParameterSpec(
            required=frozenset({"artifact_type"}),
            optional=frozenset({"check_item", "requirement", "standard_value", "unit", "criterion"}),
            notes=(
                "artifact_type 取值属于处理产物类型目录（MATERIAL_CLASSIFY / DOCUMENT_PARSE / OCR / SEAL）。",
                "check_item 取值属于本目录里的文件检查子能力；未实现的子项在发布前作为能力缺口暴露。",
                "standard_value / unit 是业务填写的合格标准，没有系统默认值。",
            ),
        ),
        statuses=frozenset({"PASSED", "ABNORMAL", "NOT_APPLICABLE", "EVIDENCE_INSUFFICIENT"}),
        supports_evidence=True,
        supports_comparison=True,
        implemented=True,
        execution=EXEC_FILE_QUALITY,
        evidence=EVIDENCE_DIRECT,
        judge=JUDGE_PROGRAM,
        output_keys=("per_material", "standard", "comparison", "inputs"),
        extension_namespace=EXTENSION_NAMESPACE_QUALITY,
        note=(
            "算子本身已实现，但**判定能力取决于检查子项**：只有子项 implemented=true 时才真正判定；"
            "子项未实现时本条规则明确返回不可判定，不默认通过。"
        ),
    ),
    Capability(
        capability_id="LLM_EXTRACT",
        label="字段抽取",
        kind=KIND_OPERATOR,
        nodes=frozenset({"FIELD_EXTRACTION"}),
        input_kind=INPUT_OUTPUT_FIELDS,
        parameters=ParameterSpec(
            required=frozenset({"instruction"}),
            notes=("instruction 是该字段的抽取要求原文，只能来自业务配置。",),
        ),
        statuses=frozenset({"PASSED", "NOT_EXECUTED", "EVIDENCE_INSUFFICIENT"}),
        supports_evidence=True,
        supports_comparison=False,
        implemented=True,
        execution=EXEC_LLM_EXTRACT,
        evidence=EVIDENCE_DIRECT,
        judge=JUDGE_LLM,
        output_keys=("standard", "extracted_values"),
        extension_namespace=EXTENSION_NAMESPACE_EXTRACTION,
    ),
    Capability(
        capability_id="FIELD_EQUAL",
        label="字段是否一致",
        kind=KIND_OPERATOR,
        nodes=_COMPARE_NODES,
        input_kind=INPUT_FIELD_PAIR,
        parameters=ParameterSpec(optional=frozenset({"requires_manual_review"})),
        statuses=_DECIDED_STATUSES | frozenset({"PENDING_REVIEW"}),
        supports_evidence=True,
        supports_comparison=True,
        implemented=True,
        execution=EXEC_OPERATOR,
        evidence=EVIDENCE_DIRECT,
        judge=JUDGE_PROGRAM,
        output_keys=("comparison",),
    ),
    Capability(
        capability_id="FIELD_NOT_EQUAL",
        label="字段应当不同",
        kind=KIND_OPERATOR,
        nodes=_COMPARE_NODES,
        input_kind=INPUT_FIELD_PAIR,
        parameters=ParameterSpec(optional=frozenset({"requires_manual_review"})),
        statuses=_DECIDED_STATUSES | frozenset({"PENDING_REVIEW"}),
        supports_evidence=True,
        supports_comparison=True,
        implemented=True,
        execution=EXEC_OPERATOR,
        evidence=EVIDENCE_DIRECT,
        judge=JUDGE_PROGRAM,
        output_keys=("comparison",),
    ),
    Capability(
        capability_id="DATE_COMPARE",
        label="日期先后比较",
        kind=KIND_OPERATOR,
        nodes=_COMPARE_NODES,
        input_kind=INPUT_FIELD_PAIR,
        parameters=ParameterSpec(
            required=frozenset({"operator"}),
            optional=frozenset({"threshold", "threshold_unit", "requires_manual_review"}),
            notes=(
                "operator 取值：EQ / GTE / LTE / GT / LT。",
                "threshold 是非负整数天；填写时必须同时给出 threshold_unit=天。",
            ),
        ),
        statuses=_DECIDED_STATUSES,
        supports_evidence=True,
        supports_comparison=True,
        implemented=True,
        execution=EXEC_OPERATOR,
        evidence=EVIDENCE_DIRECT,
        judge=JUDGE_PROGRAM,
        output_keys=("comparison",),
        note="日期标准化当前只认 YYYY-MM-DD / YYYY/M/D / YYYY.M.D / YYYY年M月D日 这几种原文写法。",
    ),
    Capability(
        capability_id="DECIMAL_COMPARE",
        label="金额数值比较",
        kind=KIND_OPERATOR,
        nodes=_COMPARE_NODES,
        input_kind=INPUT_FIELD_PAIR,
        parameters=ParameterSpec(
            required=frozenset({"operator"}),
            optional=frozenset({"threshold", "threshold_unit", "requires_manual_review"}),
            notes=(
                "operator 取值：EQ / GTE / LTE / ABS_DIFF_LTE。",
                "threshold 是不小于 0 的数值；ABS_DIFF_LTE 必须填写。",
                "两侧字段必须声明一致的单位与币种，系统不做换算。",
            ),
        ),
        statuses=_DECIDED_STATUSES,
        supports_evidence=True,
        supports_comparison=True,
        implemented=True,
        execution=EXEC_OPERATOR,
        evidence=EVIDENCE_DIRECT,
        judge=JUDGE_PROGRAM,
        output_keys=("comparison",),
    ),
    Capability(
        capability_id="DECIMAL_SUM_COMPARE",
        label="金额加总比较",
        kind=KIND_OPERATOR,
        nodes=_COMPARE_NODES,
        input_kind=INPUT_FIELD_LIST,
        parameters=ParameterSpec(
            required=frozenset({"operator", "threshold"}),
            optional=frozenset({"threshold_unit", "requires_manual_review"}),
            notes=(
                "operator 取值：EQ / GTE / LTE；threshold 必须显式配置，缺省不等于 0。",
                "输入顺序是「左侧待核对总额 + 至少一个加数」，由编译期确定，执行期不重排。",
                "全部加数必须声明一致的归一化单位与币种。",
            ),
        ),
        statuses=_DECIDED_STATUSES,
        supports_evidence=True,
        supports_comparison=True,
        implemented=True,
        execution=EXEC_OPERATOR,
        evidence=EVIDENCE_DIRECT,
        judge=JUDGE_PROGRAM,
        output_keys=("comparison", "sum_value"),
    ),
    Capability(
        capability_id="DECIMAL_CN_EQUAL",
        label="大小写金额一致性",
        kind=KIND_OPERATOR,
        nodes=_COMPARE_NODES,
        input_kind=INPUT_FIELD_PAIR,
        parameters=ParameterSpec(
            optional=frozenset({"threshold", "requires_manual_review"}),
            notes=("threshold 只能是 0：本项不做数值差额。",),
        ),
        statuses=frozenset({"EVIDENCE_INSUFFICIENT", "PENDING_REVIEW"}),
        supports_evidence=True,
        supports_comparison=False,
        # 未实现：把大写金额换算成可核验数值需要业务先确认金额单位、币种与换算口径。
        # 代码不自行补值、不按字符串比较、不让大模型做算术。
        implemented=False,
        unimplemented_reason=(
            "把大写金额换算成有证据的数值需要业务先确认金额单位、币种与大写金额换算口径；"
            "系统不代填默认口径，本项当前不可判定"
        ),
        execution=EXEC_OPERATOR,
        evidence=EVIDENCE_DIRECT,
        judge=JUDGE_HUMAN,
        output_keys=("extracted_values",),
        note="换算口径确认后接入真实换算实现；在此之前规则可以配置，但不能作为可执行结论。",
    ),
    Capability(
        capability_id="FIELD_NOT_EMPTY",
        label="字段非空",
        kind=KIND_OPERATOR,
        nodes=_COMPARE_NODES,
        input_kind=INPUT_FIELD_ONE,
        parameters=ParameterSpec(optional=frozenset({"requires_manual_review"})),
        statuses=_DECIDED_STATUSES,
        supports_evidence=True,
        supports_comparison=True,
        implemented=True,
        execution=EXEC_OPERATOR,
        evidence=EVIDENCE_DIRECT,
        judge=JUDGE_PROGRAM,
        output_keys=("comparison",),
        note="只判断有效事实是否存在且非空；抽取失败与未执行都不等于「为空」。",
    ),
    Capability(
        capability_id="CLAUSE_ABSENT",
        label="条款不应存在",
        kind=KIND_OPERATOR,
        nodes=_COMPARE_NODES,
        input_kind=INPUT_FIELD_ONE,
        parameters=ParameterSpec(optional=frozenset({"requires_manual_review"})),
        statuses=_DECIDED_STATUSES,
        supports_evidence=True,
        supports_comparison=True,
        implemented=True,
        execution=EXEC_OPERATOR,
        evidence=EVIDENCE_DIRECT,
        judge=JUDGE_PROGRAM,
        output_keys=("comparison", "clause_exists"),
        note="判断条款本身是否存在，不按「抽取到的文字是否非空」判断；只能引用条款类型字段。",
    ),
    Capability(
        capability_id="LLM_VALIDATE",
        label="按审核要求智能判断",
        kind=KIND_OPERATOR,
        nodes=_COMPARE_NODES,
        input_kind=INPUT_FIELD_PAIR,
        parameters=ParameterSpec(
            required=frozenset({"instruction"}),
            optional=frozenset({"requires_manual_review"}),
            notes=("instruction 是审核要求原文，由后端按配置列确定性填写，不接受模型改写。",),
        ),
        statuses=_DECIDED_STATUSES | frozenset({"PENDING_REVIEW"}),
        supports_evidence=True,
        supports_comparison=True,
        implemented=True,
        execution=EXEC_LLM_VALIDATE,
        evidence=EVIDENCE_RETRIEVAL,
        judge=JUDGE_LLM,
        output_keys=("retrieval",),
        note="文件内部校验最多引用一个字段；交叉校验必须左右各一个字段。",
    ),
    Capability(
        capability_id="RISK_TRIGGER",
        label="命中来源规则即触发风险",
        kind=KIND_OPERATOR,
        nodes=frozenset({"RISK_RATING"}),
        input_kind=INPUT_NONE,
        parameters=ParameterSpec(
            required=frozenset({"risk_level", "action"}),
            notes=("成立条件（任一/全部）由来源检查的「关联风险」反向确定，不作为算子参数。",),
        ),
        statuses=frozenset({"PASSED", "ABNORMAL", "NOT_APPLICABLE", "EVIDENCE_INSUFFICIENT", "NOT_EXECUTED"}),
        supports_evidence=True,
        supports_comparison=False,
        implemented=True,
        execution=EXEC_RISK,
        evidence=EVIDENCE_DIRECT,
        judge=JUDGE_PROGRAM,
        output_keys=("source_rules", "source_instances", "risk_condition", "action"),
        extension_namespace=EXTENSION_NAMESPACE_RISK,
        note="缺一项来源结果不能默认成「任一」；来源状态无法识别时风险无法判断。",
    ),
    Capability(
        capability_id="REPORT_RENDER",
        label="生成报告章节",
        kind=KIND_OPERATOR,
        nodes=frozenset({"REPORT_OUTPUT"}),
        input_kind=INPUT_NONE,
        parameters=ParameterSpec(),
        statuses=frozenset({"PASSED"}),
        supports_evidence=False,
        supports_comparison=False,
        implemented=True,
        execution=EXEC_REPORT,
        evidence=EVIDENCE_DIRECT,
        judge=JUDGE_PROGRAM,
        # content 是报告正文：它落在 audit_report.content_md（报告页与管理端从这里读），
        # 不再随执行结果回传——正文有几百行，进执行结果会被页面当成结果摘要铺开。
        output_keys=("report_status", "content_type", "content"),
        note="系统派生规则，业务不需要填写；报告生成成功不代表审核通过。",
    ),
)

# 文件检查子能力（02_文件处理规则 的「检查项目」列）

_QUALITY_CAPABILITIES: tuple[Capability, ...] = tuple(
    Capability(
        capability_id=check_item,
        label=file_quality.label_of(check_item),
        kind=KIND_QUALITY_ITEM,
        parent="FILE_QUALITY_CHECK",
        nodes=frozenset({"DOCUMENT_PREPARE"}),
        input_kind=INPUT_MATERIAL_SCOPE,
        parameters=ParameterSpec(
            required=frozenset({"check_item", "requirement"}),
            optional=frozenset({"standard_value", "unit", "criterion"}),
            notes=(
                f"合格要求取值属于 {{{'、'.join(sorted(file_quality.QUALITY_ITEM_REQUIREMENTS.get(check_item, frozenset())))}}}。",
                f"依赖的处理产物：{file_quality.QUALITY_ITEM_ARTIFACT.get(check_item)}。",
            ),
        ),
        statuses=frozenset({"PASSED", "ABNORMAL", "NOT_APPLICABLE", "EVIDENCE_INSUFFICIENT"}),
        supports_evidence=False,
        supports_comparison=file_quality.is_implemented(check_item),
        implemented=file_quality.is_implemented(check_item),
        unimplemented_reason=(
            ""
            if file_quality.is_implemented(check_item)
            else (
                "印章识别只返回印章数量与位置，不能证明盖章正确"
                if file_quality.gap_code_for(check_item) == file_quality.GAP_SEAL_COUNT_IS_NOT_QUALITY
                else "缺少已确认的判定口径（判定算法、阈值或多页汇总方式）"
            )
        ),
        execution=EXEC_FILE_QUALITY,
        evidence=EVIDENCE_DIRECT,
        judge=JUDGE_PROGRAM,
        output_keys=("per_material", "comparison")
        + (("actual_bytes", "limit_bytes") if check_item == "FILE_SIZE" else ())
        + (("duplicates",) if check_item == "FILE_DUPLICATE" else ())
        + (("declared_content_type",) if check_item == "FILE_FORMAT" else ()),
        extension_namespace=EXTENSION_NAMESPACE_QUALITY,
    )
    for check_item in sorted(file_quality.KNOWN_QUALITY_ITEMS)
)

CAPABILITIES: dict[str, Capability] = {
    item.capability_id: item for item in (*_OPERATOR_CAPABILITIES, *_QUALITY_CAPABILITIES)
}

QUALITY_CAPABILITY_IDS: frozenset[str] = frozenset(item.capability_id for item in _QUALITY_CAPABILITIES)

# 从目录派生的查询视图，供规则编译与执行共用。
NODE_CONSTRAINTS: dict[str, frozenset[str]] = {
    item.capability_id: item.nodes for item in _OPERATOR_CAPABILITIES
}
OPERATOR_LABELS: dict[str, str] = {item.capability_id: item.label for item in _OPERATOR_CAPABILITIES}
ROUTE_BY_OPERATOR: dict[str, tuple[str, str]] = {
    item.capability_id: (item.evidence, item.judge) for item in _OPERATOR_CAPABILITIES
}


def capability_of(capability_id: str | None) -> Capability | None:
    """按标识取能力；未知标识返回 None（调用方必须显式处理，不得当作已支持）。"""
    if not isinstance(capability_id, str):
        return None
    return CAPABILITIES.get(capability_id)


def is_implemented(capability_id: str | None) -> bool:
    """该标识的能力当前是否已实现。未知标识返回 False，不当作已实现。"""
    capability = capability_of(capability_id)
    return bool(capability and capability.implemented)


def status_category_of(status: str | None) -> str:
    """规则结果状态的分档。未知状态码返回 UNKNOWN，**不**归入任何已知分档。"""
    if not isinstance(status, str):
        return STATUS_CATEGORY_UNKNOWN
    return STATUS_CATEGORIES.get(status, STATUS_CATEGORY_UNKNOWN)


def status_label_of(status: str | None) -> str | None:
    """状态的标准中文含义；未知状态码返回 None（调用方必须显式显示原始码）。"""
    if not isinstance(status, str):
        return None
    return RULE_STATUS_LABELS.get(status)


def status_category_label_of(status: str | None) -> str | None:
    """状态分档的中文名；未知状态码归入「未识别状态」。"""
    category = status_category_of(status)
    return STATUS_CATEGORY_LABELS.get(category)


def status_note(status: str | None) -> str | None:
    """某个「未判定」状态到底缺什么；未知状态码返回 None。"""
    if not isinstance(status, str):
        return None
    return STATUS_NOTES.get(status)


def status_view(status: str | None) -> dict[str, Any]:
    """一个状态码的标准解释，供接口与页面共用。

    未知状态码 `known=False` 且 `label=None`：页面必须显示原始码并标注「未识别」，
    不能用「未开始」这类兜底文案把它伪装成正常状态。
    """
    known = isinstance(status, str) and status in RULE_STATUSES
    return {
        "status": status,
        "known": known,
        "label": status_label_of(status),
        "category": status_category_of(status),
        "category_label": status_category_label_of(status),
        "note": status_note(status),
    }


def capability_id_of_rule(rule: dict[str, Any]) -> str | None:
    """一条规则实际引用的能力标识。

    文件检查由「检查项目」子项决定判定能力，其余规则就是算子本身。
    取不到时返回 None，调用方必须显式处理，不得当作已支持。
    """
    operator = rule.get("operator")
    if not isinstance(operator, str):
        return None
    if operator == "FILE_QUALITY_CHECK":
        check_item = (rule.get("parameters") or {}).get("check_item")
        return check_item if isinstance(check_item, str) and check_item else operator
    return operator


def coverage_of(rules: list[dict[str, Any]]) -> dict[str, Any]:
    """把每条启用规则引用的能力展开成可核对的事实。

    「配置可保存」不等于「规则可执行」：这份覆盖结果记录能力标识、版本、输入形态、
    可表达的状态、输出键、取证与判定方式，以及**当前是否已实现**。
    它随已发布版本一起冻结（进入 manifest 与 manifest_hash），
    因此后续能力实现状态变化不会重新解释历史版本的含义。

    未知算子在这里不报错——它由调用方按配置错误显式报出
    （manifest.capability_gaps 抛 MANIFEST_OPERATOR_UNKNOWN）。
    """
    rows: list[dict[str, Any]] = []
    implemented_rule_count = 0
    unimplemented_rule_count = 0
    for rule in rules:
        if not rule.get("enabled", True):
            continue
        capability_id = capability_id_of_rule(rule)
        capability = capability_of(capability_id)
        if capability is None:
            # 未知能力：如实记录为未实现，由编译期另行明确报错，不在这里伪装成缺口。
            unimplemented_rule_count += 1
            rows.append(
                {
                    "rule_key": rule.get("rule_key"),
                    "rule_name": rule.get("name"),
                    "node": rule.get("node"),
                    "operator": rule.get("operator"),
                    "capability_id": capability_id,
                    "capability_label": None,
                    "capability_version": None,
                    "known": False,
                    "implemented": False,
                    "unimplemented_reason": "该能力不在能力目录中",
                }
            )
            continue
        if capability.implemented:
            implemented_rule_count += 1
        else:
            unimplemented_rule_count += 1
        rows.append(
            {
                "rule_key": rule.get("rule_key"),
                "rule_name": rule.get("name"),
                "node": rule.get("node"),
                "operator": rule.get("operator"),
                "capability_id": capability.capability_id,
                "capability_label": capability.label,
                "capability_version": capability.version,
                "known": True,
                "input_kind": capability.input_kind,
                "statuses": sorted(capability.statuses),
                "output_keys": list(capability.output_keys),
                "supports_evidence": capability.supports_evidence,
                "supports_comparison": capability.supports_comparison,
                "evidence": capability.evidence,
                "judge": capability.judge,
                "extension_namespace": capability.extension_namespace,
                "implemented": capability.implemented,
                "unimplemented_reason": capability.unimplemented_reason or None,
                "parameters": sorted(
                    (rule.get("parameters") or {}).keys()
                    if isinstance(rule.get("parameters"), dict)
                    else ()
                ),
            }
        )
    return {
        "capability_version": CAPABILITY_VERSION,
        "declared_rule_count": len(rows),
        "implemented_rule_count": implemented_rule_count,
        "unimplemented_rule_count": unimplemented_rule_count,
        "rules": rows,
    }


def catalog_payload() -> list[dict[str, Any]]:
    """能力目录的对外结构：按「算子级在前、文件检查子项在后」的稳定顺序返回。"""
    return [item.to_payload() for item in (*_OPERATOR_CAPABILITIES, *_QUALITY_CAPABILITIES)]
