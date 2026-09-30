from typing import Any, Literal

from pydantic import BaseModel, Field

from ...common.api import ApiResponse


class ReportSnapshotSignal(BaseModel):
    """报告生成时刻的真实范围快照，用于把报告与当时的输入修订对账。"""

    task_input_revision: int = 0
    upload_ids: list[int] = Field(default_factory=list)
    field_value_ids: list[int] = Field(default_factory=list)
    review_ids: list[int] = Field(default_factory=list)
    rule_result_ids: list[int] = Field(default_factory=list)

class ReportFormatSupport(BaseModel):
    """一种声明输出格式的真实支持状态。

    声明（业务对报告形态的期望）与实现（系统能否真的产出该格式文件）必须分开表达：
    节点版模板在 07 声明 PDF / Word / Excel，当前只生成 Markdown 正文。
    只记录 formats、不记录支持状态，会让页面把未实现的格式显示成「可执行的输出格式」。
    """

    format: str
    supported: bool
    message: str = ""
    report_code: str | None = None
    report_name: str | None = None

class ReportResponse(ApiResponse):
    task_no: str
    status: Literal["COMPLETE", "INCOMPLETE", "NOT_GENERATED"]
    content_type: Literal["text/markdown"]
    content: str | None
    reasons: list[str]
    # 三个信号必须分开表达：报告是否生成、已配置审核范围是否执行完整、风险是否可判定。
    # 不能用单独一个 status 表示“全部通过”，也不能因为未配置风险评级就判定执行故障。
    report_generated: bool = False
    scope_execution_complete: bool = False
    risk_conclusion_available: bool = False
    # 报告结果范围：只对应当前有效事实与执行结果，历史结果不再进入当前结论。
    results_scope: Literal["CURRENT_RUN"] = "CURRENT_RUN"
    # 当前入口返回的报告是否仍对应当前输入修订；为 false 时页面必须标明“已失效”。
    current: bool = True
    input_revision: int = 0
    snapshot: ReportSnapshotSignal | None = None
    # 本节报告声明的输出格式与各自的真实支持状态；缺少该字段会让页面无从判断
    # 「模板要求 PDF、系统只产 Markdown」这件事。
    format_support: list[ReportFormatSupport] = Field(default_factory=list)
    # 本次报告正文里，系统固定事实章节之外的业务附加章节数量。零业务章节不等于零报告，
    # 也不等于审核范围完整，因此单独表达。
    business_section_count: int = 0
    # 报告结论的结构化形式（报告是否生成 / 范围是否完整 / 风险是否可判定 / 未实现的格式 / 能力缺口）。
    # 页面优先读它；报告正文里的中文行仍是给人读的原文，页面不再靠匹配行首文案提取结论。
    conclusion: dict[str, Any] | None = None

__all__ = [
    "ReportSnapshotSignal",
    "ReportFormatSupport",
    "ReportResponse",
]

