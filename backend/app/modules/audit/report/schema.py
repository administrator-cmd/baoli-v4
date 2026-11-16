"""审核响应模型。"""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.core.base_schema import PagedResponse


class ReportRowResponse(BaseModel):
    report_no: str
    report_name: str
    task_no: str
    task_name: str
    scenario_name: str
    scenario_version_no: int
    plan_name: str | None
    generated_at: datetime | None
    report_generated: bool
    scope_execution_complete: bool
    risk_conclusion_available: bool
    status: str
    archived_at: datetime | None
    current: bool
    stale: bool
    stale_reason: str | None
    input_revision: int
    task_input_revision: int
    remark: str | None


class RiskRowResponse(BaseModel):
    task_no: str
    task_name: str
    scenario_name: str
    scenario_version_no: int
    plan_name: str | None
    rule_key: str
    rule_code: str | None
    name: str
    risk_level: str | None
    status: str
    message: str | None
    suggestion: Any
    source_rules: list[dict[str, Any]] = Field(default_factory=list)
    result_id: int
    created_at: datetime


class RiskResultPageResponse(PagedResponse[RiskRowResponse]):
    """风险结果分页：命中数仅统计 ABNORMAL。"""

    hit_count: int = 0
    risk_levels: list[str] = Field(default_factory=list)


class RiskListResponse(BaseModel):
    task_no: str
    risk_configured: bool = False
    risk_executed: bool = False
    # 风险是否形成可用结论；与“命中 0 条”不是一回事。
    risk_conclusion_available: bool = False
    # 只统计 ABNORMAL，不能用结果总数冒充命中数。
    hit_count: int = 0
    status_counts: dict[str, int] = Field(default_factory=dict)
    items: list[dict[str, Any]] = Field(default_factory=list)

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
    报告格式来自规则库要求，实际输出格式由真实产出记录。
    只记录 formats、不记录支持状态，会让页面把未实现的格式显示成「可执行的输出格式」。
    """

    format: str
    supported: bool
    message: str = ""
    report_code: str | None = None
    report_name: str | None = None


class ReportGenerationInfo(BaseModel):
    """报告生成时记录的实际方式；历史报告不推断或补造。"""

    mode: Literal["RULES", "TEMPLATE"]
    requested_mode: Literal["RULES", "TEMPLATE"] | None = None
    template_no: str | None = None
    template_name: str | None = None
    fallback_code: str | None = None
    fallback_reason: str | None = None


class ReportResponse(BaseModel):
    task_no: str
    report_no: str | None = None
    report_name: str | None = None
    reports: list[dict[str, Any]] = Field(default_factory=list)
    status: Literal["COMPLETE", "INCOMPLETE", "NOT_GENERATED"]
    content_type: Literal["text/markdown", "text/plain", "text/html"]
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
    # 失效的真实原因；current 为 true 时为空。页面直接展示它，不再自行推断失效原因。
    stale_reason: str | None = None
    input_revision: int = 0
    snapshot: ReportSnapshotSignal | None = None
    report_generation: ReportGenerationInfo | None = None
    # 本节报告声明的输出格式与各自的真实支持状态；缺少该字段会让页面无从判断
    # 「模板要求 PDF、系统只产 Markdown」这件事。
    format_support: list[ReportFormatSupport] = Field(default_factory=list)
    # 本次报告正文里，系统固定事实章节之外的业务附加章节数量。零业务章节不等于零报告，
    # 也不等于审核范围完整，因此单独表达。
    business_section_count: int = 0
    # 报告结论的结构化形式（报告是否生成 / 范围是否完整 / 风险是否可判定 / 未实现的格式 / 能力缺口）。
    # 页面优先读它；报告正文里的中文行仍是给人读的原文，页面不再靠匹配行首文案提取结论。
    conclusion: dict[str, Any] | None = None


class ReportDetailResponse(BaseModel):
    """报告详情：正文、三信号、范围快照与结构化结论。"""

    report_no: str
    report_name: str
    task_no: str | None = None
    task_name: str | None = None
    scenario_name: str | None = None
    scenario_version_no: int | None = None
    plan_name: str | None = None
    generated_at: datetime | None = None
    content_type: str
    content: str | None = None
    report_generated: bool
    scope_execution_complete: bool
    risk_conclusion_available: bool
    reasons: dict[str, Any] = Field(default_factory=dict)
    snapshot: dict[str, Any] | None = None
    report_generation: ReportGenerationInfo | None = None
    status: str
    archived_at: datetime | None = None
    remark: str | None = None
    stale: bool
    stale_reason: str | None = None
    input_revision: int
    task_input_revision: int | None = None
