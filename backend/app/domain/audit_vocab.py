"""审核链路的共享词汇：产物来源声明与材料类型状态。

取值同时被编译期与运行期使用，只能有一份定义。本模块不依赖 services，
供 domain、services 与 operators 共同引用。
"""

from typing import Any, Literal

from ..common.exceptions import BusinessError


# 字段「产物来源」列唯一受支持的取值：印章识别产物。
SEAL_ARTIFACT_SOURCE = "SEAL"

# 字段「期间口径」「报表口径」的不适用取值，由当前业务是否涉及相应口径决定。
# 必须是显式取值而不是 NULL——统一事实表的「当前版本」唯一键由这几个维度用 CONCAT 拼成，
# MySQL 的 CONCAT 遇 NULL 会整体返回 NULL，唯一键随之失效，同一字段可有多条「当前」事实。
# 它只表示不适用，不等于任何具体口径；计算参数可使用它定位不涉及该口径的事实。
SCOPE_NOT_APPLICABLE = "NOT_APPLICABLE"


def declared_artifact_source(item: dict[str, Any]) -> str | None:
    """字段声明的产物来源；未声明返回 None。不认识的来源明确报错，不当成未声明。"""
    declared = item.get("artifact_source")
    if declared in (None, ""):
        return None
    source = str(declared)
    if source != SEAL_ARTIFACT_SOURCE:
        raise BusinessError(
            "FIELD_ARTIFACT_SOURCE_UNSUPPORTED",
            f"字段“{item.get('name') or item.get('field_key')}”声明的产物来源“{source}”当前不受支持，"
            "系统无法确定该字段应取自哪种处理产物",
            details={"field_key": item.get("field_key"), "field_name": item.get("name")},
        )
    return source


# 材料类型待复核与已确认不符分别记录。
MATERIAL_TYPE_REVIEW = "TYPE_REVIEW"
MATERIAL_TYPE_REJECTED = "TYPE_REJECTED"
MATERIAL_TYPE_UNAVAILABLE_STATUSES = (MATERIAL_TYPE_REVIEW, MATERIAL_TYPE_REJECTED)


def is_type_unavailable(status: str | None) -> bool:
    """材料位的类型是否不可用（待复核或已确认不符）。"""
    return status in MATERIAL_TYPE_UNAVAILABLE_STATUSES




# 规则结果可能出现的状态。执行端与页面共用这一份集合：
# 不在集合里的状态码必须显式暴露，不能被页面当成「未开始」静默吞掉。
RULE_STATUSES: tuple[str, ...] = (
    "COMPUTED",
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

# 执行器、能力目录和结果视图使用的状态集合。状态码本身只在这里声明，
# 调用方按业务语境选择集合，避免每个模块重新拼接字符串集合。
ResultStatus = Literal[
    "COMPUTED",
    "PASSED",
    "ABNORMAL",
    "EVIDENCE_INSUFFICIENT",
    "NOT_APPLICABLE",
    "NOT_EXECUTED",
    "PENDING_REVIEW",
    "REVIEWED",
    "SKIPPED",
    "RUNNING",
]
RiskStatus = Literal[
    "PASSED", "ABNORMAL", "EVIDENCE_INSUFFICIENT", "PENDING_REVIEW", "NOT_APPLICABLE",
]

# 单条规则执行循环状态，与最终业务结论状态分开。
LOOP_RUNNING = "RUNNING"
LOOP_NEEDS_HUMAN = "NEEDS_HUMAN"
LOOP_DONE = "DONE"
LOOP_BLOCKED = "BLOCKED"

MODEL_VERDICT_STATUSES = frozenset({
    "PASSED", "ABNORMAL", "COMPUTED", "NOT_APPLICABLE", "EVIDENCE_INSUFFICIENT",
})
PRODUCED_RESULT_STATUSES = frozenset({"PASSED", "ABNORMAL", "COMPUTED"})
COMPLETED_RESULT_STATUSES = frozenset({"PASSED", "ABNORMAL", "COMPUTED", "NOT_APPLICABLE"})
CONCLUSIVE_RESULT_STATUSES = frozenset({"PASSED", "ABNORMAL", "NOT_APPLICABLE"})
OPERATOR_RESULT_STATUSES = frozenset({"PASSED", "ABNORMAL", "EVIDENCE_INSUFFICIENT"})
UNRESOLVED_RESULT_STATUSES = frozenset({
    "EVIDENCE_INSUFFICIENT", "PENDING_REVIEW", "NOT_EXECUTED", "RUNNING", "SKIPPED",
})

# 状态分类：页面分档与统计共用同一口径。
#   已判定：系统或人工已经给出可用的业务结论（含人工改判后的「已复核」）。
#   未判定：应当判定但还没有形成可用结论（证据不足、待复核、未执行、执行中、已跳过）。
#   不适用：适用条件明确不成立，本次就不该判定。
#   仅计算完成：已有中间值，本条没有配置合格/异常判定。
# 「未执行 / 已跳过 / 未配置」与「证据不足」都不能算合格，也不能互相冒充。
STATUS_CATEGORY_DECIDED = "DECIDED"
STATUS_CATEGORY_UNDECIDED = "UNDECIDED"
STATUS_CATEGORY_NOT_APPLICABLE = "NOT_APPLICABLE"
STATUS_CATEGORY_COMPUTED = "COMPUTED"
STATUS_CATEGORY_UNKNOWN = "UNKNOWN"

STATUS_CATEGORIES: dict[str, str] = {
    "COMPUTED": STATUS_CATEGORY_COMPUTED,
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

# 状态的标准中文含义。它是系统对状态码的解释，与业务话术无关；
# 页面显示状态时用这一份，未知状态码明确标为未识别，不用兜底文案冒充正常。
RULE_STATUS_LABELS: dict[str, str] = {
    "COMPUTED": "已完成产出（不作合格/异常判定）",
    "PASSED": "合格",
    "ABNORMAL": "不合格",
    "EVIDENCE_INSUFFICIENT": "无法判断（证据或能力不足）",
    "NOT_APPLICABLE": "不适用",
    "NOT_EXECUTED": "未执行",
    "PENDING_REVIEW": "未确认",
    "REVIEWED": "已人工复核",
    "SKIPPED": "已跳过",
    "RUNNING": "执行中",
}

STATUS_CATEGORY_LABELS: dict[str, str] = {
    STATUS_CATEGORY_COMPUTED: "已完成产出",
    STATUS_CATEGORY_DECIDED: "已判定",
    STATUS_CATEGORY_UNDECIDED: "尚未判定",
    STATUS_CATEGORY_NOT_APPLICABLE: "不适用",
    STATUS_CATEGORY_UNKNOWN: "未识别状态",
}

# 每种「未判定」到底缺什么。统一写成「证据不足」会让人不知道该去补哪一步。
STATUS_NOTES: dict[str, str] = {
    "COMPUTED": "已产生可供下游使用的中间值，本条没有合格或异常结论。",
    "EVIDENCE_INSUFFICIENT": "尚未形成可用结论，具体缺什么见该条结果的原因；它既不是合格，也不是不合格。",
    "PENDING_REVIEW": "业务尚未确认；人工审核暂不启用，保留原因并继续执行其他规则，不代表通过。",
    "NOT_EXECUTED": "该检查本次没有执行，不能据此认为检查通过。",
    "SKIPPED": "该规则本次已跳过，具体原因见执行结果；跳过不等于通过。",
    "RUNNING": "该检查正在执行，尚未形成结果。",
    "NOT_APPLICABLE": "适用条件明确不成立，本次不该执行这条检查；不适用不等于通过。",
}


def status_category_of(status: str | None) -> str:
    """规则结果状态的分档。未知状态码返回 UNKNOWN，不归入任何已知分档。"""
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
