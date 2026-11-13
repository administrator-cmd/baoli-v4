"""审核执行的内部契约：固定调度结构与动态业务内容分开声明。"""

from typing import Any, NotRequired, TypedDict

from app.domain.shared.llm_failure import ChannelFailure


class EvaluationPlanEntry(TypedDict):
    cursor: int
    rule_key: str
    rule_code: str
    node_code: str
    # 冻结的业务规则随场景变化，不在运行时状态中重新定义业务配置。
    rule: dict[str, Any]


class FieldReviewState(TypedDict):
    """本轮调度的核对边界；与面向页面的核对展示结构分开。"""

    required: bool
    confirmed: bool
    extraction_closed: bool
    extraction_rule_count: int
    after_review_rule_count: int
    after_rule_keys: list[str]
    allowed_rule_keys: list[str]


class RuleOutcome(TypedDict):
    rule_key: str
    rule_code: str | None
    rule_name: str | None
    loop_status: str | None
    loop_count: int
    unresolved_items: list[dict[str, Any]]
    status: str
    channel_failure: NotRequired[ChannelFailure]


class EvaluationContext(TypedDict):
    agent_mode: bool
    evaluation_plan: list[EvaluationPlanEntry]
    field_review: FieldReviewState
    execution_id: int
    rule_outcomes: list[RuleOutcome]
    loop_count: int
    last_error: None


class AgentSummary(TypedDict):
    agent_rule_count: int
    missing_rule_keys: list[str]
    rule_outcomes: list[RuleOutcome]
    loop_status: str
    loop_count: int
    unresolved_items: list[dict[str, Any]]
    channel_failure: NotRequired[ChannelFailure]
    field_review_paused: NotRequired[bool]


class AuditGraphState(TypedDict, total=False):
    """图节点只返回更新的键；会话与执行器由图上下文单独传递。"""

    task_no: str
    current_node: str
    node_index: int
    phase: str
    executed_node: str | None
    last_execution: dict[str, Any] | None
    last_error: dict[str, Any] | None
    execution_id: int | None
    execution_mode: str | None
    node_result: dict[str, Any] | None
    execution_status: str
    execution_failure_code: str | None
    agent_mode: bool
    evaluation_plan: list[EvaluationPlanEntry]
    field_review: FieldReviewState | None
    rule_outcomes: list[RuleOutcome]
    loop_count: int
    channel_failure: ChannelFailure | None
    field_review_paused: bool
    covered_rule_keys: list[str]


class RuleExecutionState(TypedDict, total=False):
    """单条规则的初始状态及模型/工具步骤的局部更新，不用于持久化 checkpoint。"""

    agent_mode: bool
    evaluation_id: str | None
    rule_cursor: int
    rule_instance: dict[str, Any]
    allowed_tools: list[dict[str, Any]]
    messages: list[dict[str, Any]] | None
    pending_tool_calls: list[dict[str, Any]]
    tool_results: list[dict[str, Any]]
    candidate_verdict: dict[str, Any] | None
    unresolved_items: list[dict[str, Any]]
    loop_status: str
    loop_count: int
    last_model_response: dict[str, Any] | None
    verdict_errors: list[dict[str, Any]]
    last_error: dict[str, Any] | None
    channel_failure: ChannelFailure


class RuleSaveResult(TypedDict):
    rule_outcome: RuleOutcome
    unresolved_items: list[dict[str, Any]]
    pending_tool_calls: list[dict[str, Any]]
    tool_results: list[dict[str, Any]]
    last_error: None
