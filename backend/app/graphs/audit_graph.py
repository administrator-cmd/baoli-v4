import logging
from typing import Any, Protocol, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime
from langgraph.types import Command, interrupt

from ..common.exceptions import BusinessError, exception_summary
from ..runtime.checkpoint import is_storage_unavailable, mysql_saver


logger = logging.getLogger(__name__)

# 检查点存储暂时不可达（连接/读取异常）与「检查点确实不在等待」是两件事：
# 调用方据此区分“本次操作可重试”与“业务等待点已丢失”，不能一律按缺失处理。
CHECKPOINT_STORAGE_UNAVAILABLE = "CHECKPOINT_STORAGE_UNAVAILABLE"


class AuditGraphState(TypedDict, total=False):
    """图 state 只放可持久化、可 JSON 序列化的流程引用，不放业务对象或数据库会话。

    字段分三组：流程控制（任务加载、节点确认、人工复核等待点、结果保存）；节点执行
    （「开始 → 执行 → 收尾 → 推进」四步，执行模式在执行之前确定，因此任务当前节点在
    Agent 评估期间不会被提前推进）；Agent 运行契约（规则评估子图的「模型—工具—结果」
    循环状态）。

    三条硬约束：可序列化——数据库会话、HTTP 客户端、密钥、ORM 对象一律不进 State
    （它们在 `AuditGraphContext` 与 `ToolExecutionContext.database_session` 里）；
    可恢复——恢复时读同一份引用，不通过查询「最新对象」改变历史执行；单一归属——
    消息与工具结果属于当前这一条规则实例，切到下一条时被重置。
    """

    task_no: str
    graph_namespace: str
    current_node: str
    node_index: int
    phase: str
    confirmed_node: str | None
    executed_node: str | None
    last_execution: dict[str, Any] | None
    manual_review_result: dict[str, Any] | None
    review_hash: str | None
    review_count: int | None
    review_decisions: list[dict[str, Any]] | None
    last_error: dict[str, Any] | None

    # 节点执行
    # 本次节点执行记录。开始执行时建立并提交，收尾时按它写终态。
    execution_id: int | None
    # DETERMINISTIC / AGENT / MANUAL_REVIEW_PREPARE。执行之前由冻结配置确定。
    execution_mode: str | None
    # 确定性路径的执行结果（供收尾写执行记录）。
    node_result: dict[str, Any] | None
    # 节点里没有授权工具、仍由确定性实现执行的规则结果。
    deterministic_result: dict[str, Any] | None

    # Agent 运行契约
    # 本节点是否走规则评估 Agent 路径。
    agent_mode: bool | None
    # 节点的评估计划（交给 Agent 的规则实例清单，顺序确定）。
    evaluation_plan: list[dict[str, Any]] | None
    # 当前处理到计划中的第几条规则。一次评估只处理一条。
    rule_cursor: int | None
    # 规则实例：同一条规则定义作用于不同材料组合时是不同实例，结果不能互相覆盖。
    # 含本次从冻结快照解析出的输入事实与材料范围。
    rule_instance: dict[str, Any] | None
    # 规则实例评估批次。一次评估内的事实集合是冻结的，全部工具调用共享同一批次身份。
    evaluation_id: str | None
    # 本次评估冻结的输入事实快照身份（ref 可读、hash 供恢复比对）。
    fact_snapshot_ref: str | None
    fact_snapshot_hash: str | None
    # 模型当时看到的工具定义版本与内容指纹。指纹由目录内容算出，
    # 因此能发现「版本号没改但 Schema 变了」。
    tool_catalog_version: str | None
    tool_catalog_hash: str | None
    # 本次允许调用的工具范围（来自已发布配置的 allowed_tools，按节点与规则收窄），
    # 不是整个目录。
    tool_policy_version: str | None
    allowed_tools: list[dict[str, Any]] | None
    # 消息历史：只放进入模型上下文的部分，大对象用版本化引用。
    messages: list[dict[str, Any]] | None
    # 已请求但尚未取回结果的工具调用（按 tool_call_id 关联）。每轮只装一批。
    pending_tool_calls: list[dict[str, Any]] | None
    # 已完成并已回填消息的工具调用与结果。它与 `agent_tool_call` 账本对账：
    # state 用于流程推进，账本用于审计与幂等恢复。
    tool_results: list[dict[str, Any]] | None
    # 模型形成的候选结论（经服务端校验后采纳的那一份，尚未保存为规则结果）。
    candidate_verdict: dict[str, Any] | None
    # 仍未解决的事项：缺输入、证据不足、工具失败、口径未确认。
    # 循环无法继续时必须由它和 loop_status 暴露真实原因，不静默结束。
    unresolved_items: list[dict[str, Any]] | None
    # 循环状态：RUNNING / NEEDS_HUMAN / DONE / BLOCKED。
    # BLOCKED 必须带 unresolved_items，不允许用「跑完轮次」冒充 DONE。
    loop_status: str | None
    loop_count: int | None
    # 模型最近一次响应的摘要（含响应种类、结束原因与最终文本），供失败诊断与结论解析。
    last_model_response: dict[str, Any] | None
    # 已处理完的规则结论摘要。全部规则处理完后由它汇总成节点执行结果。
    rule_outcomes: list[dict[str, Any]] | None
    # 本节点是否还有下一条规则实例。
    agent_done: bool | None
    # 本次人工复核等待是否由 Agent 评估创建（决定提交后走哪条应用路径）。
    agent_review_pending: bool | None
    # Agent 复核前正在评估的节点与检查点，提交后据此恢复执行位置。
    agent_review_restore: dict[str, Any] | None
    # 字段更正使原 Agent 评估快照失效时，退出旧评估并重跑受影响节点。
    agent_review_recalculate: bool | None


class AuditNodeRunner(Protocol):
    """图与节点实现之间的契约。

    图只负责状态推进、interrupt、resume 与节点调度；节点内部的业务规则在
    `services.audit.flow` 里，由调用方在调用时注入，图模块因此不导入服务层，
    `graphs` 与 `services` 之间没有模块级依赖，也没有循环。

    这里把全部节点入口写成显式协议，而不是 `Any`：函数改名、缺一个入口或签名变化
    会在类型检查（mypy / pyright）与实现自检时暴露，不会拖到真实图执行才失败。
    """

    def graph_load_task(self, db: Any, task_no: str, graph_namespace: str) -> dict[str, Any]: ...

    # 节点执行的四步
    def graph_execute_current_node(
        self,
        db: Any,
        task_no: str,
        graph_namespace: str,
        current_node: str,
        confirmed_node: str | None,
    ) -> dict[str, Any]: ...

    def graph_run_deterministic_node(
        self,
        db: Any,
        task_no: str,
        graph_namespace: str,
        execution_id: int | None,
    ) -> dict[str, Any]: ...

    def graph_finalize_node_execution(
        self,
        db: Any,
        task_no: str,
        graph_namespace: str,
        execution_id: int | None,
        execution_mode: str,
        node_result: dict[str, Any] | None,
        agent_summary: dict[str, Any] | None,
    ) -> dict[str, Any]: ...

    def graph_advance_workflow(
        self,
        db: Any,
        task_no: str,
        graph_namespace: str,
        execution_id: int | None,
    ) -> dict[str, Any]: ...

    def graph_prepare_manual_review(self, db: Any, task_no: str, graph_namespace: str) -> dict[str, Any]: ...

    def graph_apply_manual_reviews(
        self,
        db: Any,
        task_no: str,
        graph_namespace: str,
        review_hash: str | None,
        review_count: int | None,
        decisions: list[dict[str, Any]] | None,
    ) -> dict[str, Any]: ...

    def graph_save_node_result(self, db: Any, task_no: str, graph_namespace: str) -> dict[str, Any]: ...

    # 规则评估 Agent 的生命周期入口
    # 与上面同一手法：图只调度，实现由调用方注入。
    # 每个入口只走一步：一次加载、一次模型调用、一批工具执行、一次结论校验。
    # 没有函数内部循环——循环由图的条件边表达，因此每一步都可 checkpoint。

    def graph_load_evaluation(
        self,
        db: Any,
        task_no: str,
        graph_namespace: str,
        node_code: str,
        execution_id: int | None,
    ) -> dict[str, Any]: ...

    def graph_load_rule_instance(
        self,
        db: Any,
        task_no: str,
        graph_namespace: str,
        node_code: str,
        rule_cursor: int,
        execution_id: int | None,
    ) -> dict[str, Any]: ...

    def graph_prepare_agent_context(
        self,
        db: Any,
        task_no: str,
        graph_namespace: str,
        node_code: str,
        rule_cursor: int,
        rule_instance: dict[str, Any] | None,
        messages: list[dict[str, Any]] | None,
    ) -> dict[str, Any]: ...

    def graph_call_model(
        self,
        db: Any,
        task_no: str,
        graph_namespace: str,
        node_code: str,
        rule_cursor: int,
        messages: list[dict[str, Any]] | None,
        allowed_tools: list[dict[str, Any]] | None,
        loop_count: int,
        state_evaluation_id: str | None,
        executed_tool_results: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]: ...

    def graph_execute_tool_calls(
        self,
        db: Any,
        task_no: str,
        graph_namespace: str,
        node_code: str,
        pending_tool_calls: list[dict[str, Any]] | None,
        allowed_tools: list[dict[str, Any]] | None,
        fact_snapshot_ref: str | None,
        rule_instance: dict[str, Any] | None,
        existing_unresolved: list[dict[str, Any]] | None,
    ) -> dict[str, Any]: ...

    def graph_append_tool_results(
        self,
        messages: list[dict[str, Any]] | None,
        tool_results: list[dict[str, Any]] | None,
    ) -> dict[str, Any]: ...

    def graph_produce_candidate_verdict(
        self,
        db: Any,
        task_no: str,
        graph_namespace: str,
        node_code: str,
        rule_instance: dict[str, Any] | None,
        last_model_response: dict[str, Any] | None,
        tool_results: list[dict[str, Any]] | None,
        allowed_tools: list[dict[str, Any]] | None,
        fact_snapshot_ref: str | None,
        loop_status: str,
        unresolved_items: list[dict[str, Any]] | None,
    ) -> dict[str, Any]: ...

    def graph_prepare_agent_review(
        self,
        db: Any,
        task_no: str,
        graph_namespace: str,
        node_code: str,
        rule_cursor: int,
        execution_id: int | None,
        candidate_verdict: dict[str, Any] | None,
        unresolved_items: list[dict[str, Any]] | None,
        tool_results: list[dict[str, Any]] | None,
        evaluation_id: str | None,
        rule_instance: dict[str, Any] | None,
        fact_snapshot_ref: str | None = None,
    ) -> dict[str, Any]: ...

    def graph_apply_agent_reviews(
        self,
        db: Any,
        task_no: str,
        graph_namespace: str,
        review_hash: str | None,
        review_count: int | None,
        decisions: list[dict[str, Any]] | None,
        agent_review_restore: dict[str, Any] | None,
    ) -> dict[str, Any]: ...

    def graph_save_evaluation_result(
        self,
        db: Any,
        task_no: str,
        graph_namespace: str,
        node_code: str,
        execution_id: int | None,
        rule_instance: dict[str, Any] | None,
        candidate_verdict: dict[str, Any] | None,
        loop_status: str,
        unresolved_items: list[dict[str, Any]] | None,
        tool_results: list[dict[str, Any]] | None,
        evaluation_id: str | None,
        fact_snapshot_ref: str | None = None,
    ) -> dict[str, Any]: ...

    def graph_advance_rule_cursor(
        self,
        rule_cursor: int,
        evaluation_plan: list[dict[str, Any]] | None,
    ) -> dict[str, Any]: ...


class AuditGraphContext(TypedDict):
    """单次调用的依赖。它们绝不写入图 state。

    `nodes` 是本次调用要用的节点实现（`services.audit.flow` 模块），必须满足
    `AuditNodeRunner`。它在调用时注入，因此图模块不依赖服务层。
    """

    db: Any
    nodes: AuditNodeRunner


# 循环状态取值。与 `services.audit.agent` 的常量一致（那里是实现，这里是状态机）。
LOOP_RUNNING = "RUNNING"
LOOP_NEEDS_HUMAN = "NEEDS_HUMAN"
LOOP_DONE = "DONE"
LOOP_BLOCKED = "BLOCKED"

# 执行模式取值。与 `services.audit.flow` 的常量一致。
MODE_DETERMINISTIC = "DETERMINISTIC"
MODE_AGENT = "AGENT"
MODE_MANUAL_REVIEW = "MANUAL_REVIEW_PREPARE"


def _flow(runtime: Runtime[AuditGraphContext]) -> AuditNodeRunner:
    return runtime.context["nodes"]


def _db(runtime: Runtime[AuditGraphContext]) -> Any:
    return runtime.context["db"]


# 流程控制节点


def _load_task(state: AuditGraphState, runtime: Runtime[AuditGraphContext]) -> dict[str, Any]:
    flow = _flow(runtime)
    return flow.graph_load_task(
        _db(runtime),
        state["task_no"],
        state["graph_namespace"],
    )


def _await_node_confirmation(state: AuditGraphState) -> dict[str, Any]:
    confirmation = interrupt(
        {
            "kind": "NODE_CONFIRMATION",
            "task_no": state["task_no"],
            "node_code": state["current_node"],
        }
    )
    if not isinstance(confirmation, dict) or not isinstance(confirmation.get("node_code"), str):
        raise BusinessError("AUDIT_CONFIRMATION_INVALID", "审核节点确认内容无效", status_code=409)
    return {"confirmed_node": confirmation["node_code"], "last_error": None}


def _execute_current_node(state: AuditGraphState, runtime: Runtime[AuditGraphContext]) -> dict[str, Any]:
    """开始执行当前节点：建执行记录、确定执行模式。

    不执行节点本身：执行由 `run_deterministic_node` 或规则评估 Agent 路径完成。
    """
    flow = _flow(runtime)
    started = flow.graph_execute_current_node(
        _db(runtime),
        state["task_no"],
        state["graph_namespace"],
        state["current_node"],
        state.get("confirmed_node"),
    )
    # StateGraph 会保留未被本次返回值覆盖的字段。每个业务节点必须从自己的
    # 执行状态开始，否则前一 Agent 节点会改变后续确定性节点的收尾与复核路由。
    # 人工复核恢复直接进入 apply_*_reviews，不经过这里，继续使用原评估状态。
    return {
        "execution_id": None,
        "execution_mode": None,
        "node_result": None,
        "last_execution": None,
        "manual_review_result": None,
        "review_hash": None,
        "review_count": None,
        "review_decisions": None,
        "deterministic_result": None,
        "agent_mode": False,
        "evaluation_plan": None,
        "rule_cursor": None,
        "rule_instance": None,
        "evaluation_id": None,
        "fact_snapshot_ref": None,
        "fact_snapshot_hash": None,
        "tool_catalog_version": None,
        "tool_catalog_hash": None,
        "tool_policy_version": None,
        "allowed_tools": None,
        "messages": None,
        "pending_tool_calls": [],
        "tool_results": [],
        "candidate_verdict": None,
        "unresolved_items": [],
        "loop_status": None,
        "loop_count": 0,
        "last_model_response": None,
        "rule_outcomes": [],
        "agent_done": False,
        "agent_review_pending": False,
        "agent_review_restore": None,
        "agent_review_recalculate": False,
        **started,
    }


def _run_deterministic_node(state: AuditGraphState, runtime: Runtime[AuditGraphContext]) -> dict[str, Any]:
    flow = _flow(runtime)
    return flow.graph_run_deterministic_node(
        _db(runtime),
        state["task_no"],
        state["graph_namespace"],
        state.get("execution_id"),
    )


def _finalize_node_execution(state: AuditGraphState, runtime: Runtime[AuditGraphContext]) -> dict[str, Any]:
    """收尾当前节点执行：两条路径的终态都写在这一处。

    Agent 路径的节点结果由逐规则结论汇总而来，因此在这里生成，不由某个中途节点预写。
    判定用 `agent_mode`（评估路径实际跑了）而不是 `execution_mode`（执行之前的
    预判）：收尾要写的是真实发生的事。
    """
    flow = _flow(runtime)
    mode = state.get("execution_mode") or MODE_DETERMINISTIC
    agent_mode = bool(state.get("agent_mode"))
    summary = _collect_agent_summary(state) if agent_mode else None
    return flow.graph_finalize_node_execution(
        _db(runtime),
        state["task_no"],
        state["graph_namespace"],
        state.get("execution_id"),
        MODE_AGENT if agent_mode else mode,
        state.get("node_result"),
        summary,
    )


def _advance_workflow(state: AuditGraphState, runtime: Runtime[AuditGraphContext]) -> dict[str, Any]:
    flow = _flow(runtime)
    return flow.graph_advance_workflow(
        _db(runtime),
        state["task_no"],
        state["graph_namespace"],
        state.get("execution_id"),
    )


def _prepare_manual_review(state: AuditGraphState, runtime: Runtime[AuditGraphContext]) -> dict[str, Any]:
    flow = _flow(runtime)
    return flow.graph_prepare_manual_review(
        _db(runtime),
        state["task_no"],
        state["graph_namespace"],
    )


def _await_manual_review(state: AuditGraphState) -> dict[str, Any]:
    review_hash = state.get("review_hash")
    review_count = state.get("review_count")
    if not isinstance(review_hash, str) or not isinstance(review_count, int):
        raise BusinessError("REVIEW_CHECKPOINT_MISSING", "人工复核流程检查点缺少待复核集合", status_code=409)
    decisions = interrupt(
        {
            "kind": "MANUAL_REVIEW",
            "task_no": state["task_no"],
            "review_hash": review_hash,
            "review_count": review_count,
            "agent_review": bool(state.get("agent_review_pending")),
        }
    )
    if not isinstance(decisions, list) or not all(isinstance(item, dict) for item in decisions):
        raise BusinessError("MANUAL_REVIEW_PAYLOAD_INVALID", "人工复核提交内容无效", status_code=409)
    return {"review_decisions": decisions, "last_error": None}


def _apply_manual_reviews(state: AuditGraphState, runtime: Runtime[AuditGraphContext]) -> dict[str, Any]:
    flow = _flow(runtime)
    return flow.graph_apply_manual_reviews(
        _db(runtime),
        state["task_no"],
        state["graph_namespace"],
        state.get("review_hash"),
        state.get("review_count"),
        state.get("review_decisions"),
    )


def _apply_agent_reviews(state: AuditGraphState, runtime: Runtime[AuditGraphContext]) -> dict[str, Any]:
    flow = _flow(runtime)
    return flow.graph_apply_agent_reviews(
        _db(runtime),
        state["task_no"],
        state["graph_namespace"],
        state.get("review_hash"),
        state.get("review_count"),
        state.get("review_decisions"),
        state.get("agent_review_restore"),
    )


def _save_node_result(state: AuditGraphState, runtime: Runtime[AuditGraphContext]) -> dict[str, Any]:
    flow = _flow(runtime)
    return flow.graph_save_node_result(
        _db(runtime),
        state["task_no"],
        state["graph_namespace"],
    )


# 规则评估 Agent 循环：图负责确定性生命周期（加载评估 → 冻结规则实例 → 准备上下文 →
# 调用模型 → 路由响应 → 执行工具 → 回填结果 → 形成候选结论 → 必要时人工复核 →
# 保存结果 → 推进游标 → 收尾节点），Agent 只负责在授权范围内选择工具。
#
# 每一步都是一个图节点：call_model 每次只调一次模型，execute_tool_calls 每次只执行 State
# 里那一批待执行调用，append_tool_results 只生成 tool 消息。因此每个节点结束后 State 都可
# checkpoint，interrupt / resume 能停在循环中间，恢复后不会重复执行已登记的工具调用。


def _load_evaluation(state: AuditGraphState, runtime: Runtime[AuditGraphContext]) -> dict[str, Any]:
    """加载节点评估：评估计划、节点级授权工具与工具策略版本。"""
    flow = _flow(runtime)
    result = flow.graph_load_evaluation(
        _db(runtime),
        state["task_no"],
        state["graph_namespace"],
        state["current_node"],
        state.get("execution_id"),
    )
    return {
        **result,
        "loop_status": LOOP_RUNNING,
        "loop_count": 0,
        "unresolved_items": [],
        "rule_outcomes": [],
    }


def _load_rule_instance(state: AuditGraphState, runtime: Runtime[AuditGraphContext]) -> dict[str, Any]:
    """冻结当前游标上那一条规则实例：评估身份、事实快照、授权工具与输入事实。"""
    flow = _flow(runtime)
    return flow.graph_load_rule_instance(
        _db(runtime),
        state["task_no"],
        state["graph_namespace"],
        state["current_node"],
        state.get("rule_cursor") or 0,
        state.get("execution_id"),
    )


def _prepare_agent_context(state: AuditGraphState, runtime: Runtime[AuditGraphContext]) -> dict[str, Any]:
    """准备模型上下文：消息历史、授权工具与快照身份。

    消息只在首次进入这条规则实例时生成；已有历史原样保留，恢复后模型看到的内容
    与中断前一致。
    """
    flow = _flow(runtime)
    return flow.graph_prepare_agent_context(
        _db(runtime),
        state["task_no"],
        state["graph_namespace"],
        state["current_node"],
        state.get("rule_cursor") or 0,
        state.get("rule_instance"),
        state.get("messages"),
    )


def _route_prepared_context(state: AuditGraphState) -> str:
    """准备上下文阶段发现输入不可用时，直接进入人工复核。"""
    return "prepare_agent_review" if state.get("loop_status") == LOOP_NEEDS_HUMAN else "call_model"


def _call_model(state: AuditGraphState, runtime: Runtime[AuditGraphContext]) -> dict[str, Any]:
    """调用模型一次，把这一轮的响应写回 State。

    模型请求了工具就由 `execute_tool_calls` 执行；只输出结论就由
    `produce_candidate_verdict` 校验。本节点不执行工具、不解析结论。

    `tool_results` 一并传进去：到达轮次上限而阻断时，已执行的工具账本要随
    未解决事项一起保留，不能因为「不再执行下一批」就把已做过的事丢掉。
    """
    flow = _flow(runtime)
    result = flow.graph_call_model(
        _db(runtime),
        state["task_no"],
        state["graph_namespace"],
        state["current_node"],
        state.get("rule_cursor") or 0,
        state.get("messages"),
        state.get("allowed_tools"),
        state.get("loop_count") or 0,
        state.get("evaluation_id"),
        state.get("tool_results"),
    )
    if isinstance(result.get("unresolved_items"), list):
        # 本轮的未解决事项追加到既有清单：前几轮的失败与待核查不会被覆盖掉。
        return {
            **result,
            "unresolved_items": [*(state.get("unresolved_items") or []), *result["unresolved_items"]],
        }
    return result


def _execute_tool_calls(state: AuditGraphState, runtime: Runtime[AuditGraphContext]) -> dict[str, Any]:
    """执行 State 中的这一批待执行工具调用。

    本轮结果追加进 `tool_results`：模型引用证据时可以指向前几轮的任何一次调用，
    只保留最后一轮会让「结论是哪几次工具调用得到的」丢掉一半。
    """
    flow = _flow(runtime)
    result = flow.graph_execute_tool_calls(
        _db(runtime),
        state["task_no"],
        state["graph_namespace"],
        state["current_node"],
        state.get("pending_tool_calls"),
        state.get("allowed_tools"),
        state.get("fact_snapshot_ref"),
        state.get("rule_instance"),
        state.get("unresolved_items"),
    )
    return {
        **result,
        "tool_results": [*(state.get("tool_results") or []), *(result.get("tool_results") or [])],
    }


def _append_tool_results(state: AuditGraphState, runtime: Runtime[AuditGraphContext]) -> dict[str, Any]:
    """把本批工具结果生成 `tool` 角色消息并追加到消息历史。"""
    flow = _flow(runtime)
    return flow.graph_append_tool_results(
        state.get("messages"),
        state.get("tool_results"),
    )


def _produce_candidate_verdict(state: AuditGraphState, runtime: Runtime[AuditGraphContext]) -> dict[str, Any]:
    """校验模型给出的候选结论。

    服务端在这里决定「这份结论能不能采信」：状态枚举、证据引用、与确定性工具结果的
    冲突、以及不得越过 EVIDENCE_INSUFFICIENT / FAILED / NEEDS_RECONCILE。
    不能只靠 system prompt 约束模型。
    """
    flow = _flow(runtime)
    return flow.graph_produce_candidate_verdict(
        _db(runtime),
        state["task_no"],
        state["graph_namespace"],
        state["current_node"],
        state.get("rule_instance"),
        state.get("last_model_response"),
        state.get("tool_results"),
        state.get("allowed_tools"),
        state.get("fact_snapshot_ref"),
        state.get("loop_status") or LOOP_RUNNING,
        state.get("unresolved_items"),
    )


def _prepare_agent_review(state: AuditGraphState, runtime: Runtime[AuditGraphContext]) -> dict[str, Any]:
    """把「需要人工复核」变成真实待办与合法的等待状态。

    先写规则结果与待办、再切 `WAITING_REVIEW`；顺序颠倒会让待办没有可关联的
    业务对象，也让「待复核集合」与任务状态不一致。
    """
    flow = _flow(runtime)
    result = flow.graph_prepare_agent_review(
        _db(runtime),
        state["task_no"],
        state["graph_namespace"],
        state["current_node"],
        state.get("rule_cursor") or 0,
        state.get("execution_id"),
        state.get("candidate_verdict"),
        state.get("unresolved_items"),
        state.get("tool_results"),
        state.get("evaluation_id"),
        state.get("rule_instance"),
        state.get("fact_snapshot_ref"),
    )
    return {**result, "agent_review_pending": True}


def _save_evaluation_result(state: AuditGraphState, runtime: Runtime[AuditGraphContext]) -> dict[str, Any]:
    """保存这一条规则实例的评估结果。保存是确定性生命周期的一部分，Agent 不能跳过。"""
    flow = _flow(runtime)
    result = flow.graph_save_evaluation_result(
        _db(runtime),
        state["task_no"],
        state["graph_namespace"],
        state["current_node"],
        state.get("execution_id"),
        state.get("rule_instance"),
        state.get("candidate_verdict"),
        state.get("loop_status") or LOOP_DONE,
        state.get("unresolved_items"),
        state.get("tool_results"),
        state.get("evaluation_id"),
        state.get("fact_snapshot_ref"),
    )
    outcomes = list(state.get("rule_outcomes") or [])
    if isinstance(result.get("rule_outcome"), dict):
        outcomes.append(result["rule_outcome"])
    return {**result, "rule_outcomes": outcomes}


def _advance_rule_cursor(state: AuditGraphState, runtime: Runtime[AuditGraphContext]) -> dict[str, Any]:
    """推进规则游标：还有规则就冻结下一条，没有就收尾节点执行。"""
    flow = _flow(runtime)
    return flow.graph_advance_rule_cursor(
        state.get("rule_cursor") or 0,
        state.get("evaluation_plan"),
    )


def _collect_agent_summary(state: AuditGraphState) -> dict[str, Any]:
    """节点执行结果的 Agent 汇总（收尾时写执行记录）。

    汇总逐规则的结论与未解决事项，并给出节点整体循环状态：任一规则 BLOCKED
    就必须在整体上暴露，不能因为别的规则完成了就报 DONE。
    """
    outcomes = [item for item in (state.get("rule_outcomes") or []) if isinstance(item, dict)]
    statuses = {item.get("loop_status") for item in outcomes}
    overall = LOOP_DONE
    for wanted in (LOOP_BLOCKED, LOOP_NEEDS_HUMAN):
        if wanted in statuses:
            overall = wanted
            break
    return {
        "origin": "AGENT",
        "evaluation_mode": MODE_AGENT,
        "agent_rule_count": len(outcomes),
        "rule_outcomes": outcomes,
        "loop_status": overall,
        "loop_count": state.get("loop_count") or 0,
        "deterministic_result": state.get("deterministic_result"),
        "unresolved_items": [
            item
            for outcome in outcomes
            for item in (outcome.get("unresolved_items") or [])
            if isinstance(item, dict)
        ],
    }


# 条件边


def _after_load(state: AuditGraphState) -> str:
    return "await_manual_review" if state.get("phase") == "WAITING_REVIEW" else "await_node_confirmation"


def _after_execute(state: AuditGraphState) -> str:
    """执行模式决定走哪条路径。

    模式在 `execute_current_node` 里、执行之前就已确定，因此这里只是分发：
    人工复核节点先去准备待办；Agent 模式进入规则评估循环；其余进入确定性节点实现。
    """
    if state.get("last_error") is not None:
        # 校验未通过时没有执行记录可收尾，交由结果节点抛出真实错误。
        return "save_node_result"
    mode = state.get("execution_mode")
    if mode == MODE_MANUAL_REVIEW:
        return "prepare_manual_review"
    if mode == MODE_AGENT:
        return "load_evaluation"
    return "run_deterministic_node"


def _after_prepare_manual_review(state: AuditGraphState) -> str:
    return "await_manual_review" if state.get("phase") == "WAITING_REVIEW" else "save_node_result"


def _after_await_manual_review(state: AuditGraphState) -> str:
    """本次等待是 Agent 复核批还是人工复核节点批。

    Agent 复核发生在某个校验节点执行期间：提交后要回到该节点继续评估，
    不能像人工复核节点那样推进到下一个节点。两条路径共用同一个等待节点与 interrupt，
    因此 `probe_interrupt` 与复核提交入口对两者完全一致。
    """
    return "apply_agent_reviews" if state.get("agent_review_pending") else "apply_manual_reviews"


def _after_apply_manual_reviews(state: AuditGraphState) -> str:
    # 本批提交后仍有有效待办时，任务留在同一等待点并重建集合哈希，
    # 必须回到 await_manual_review 等下一批；只有没有剩余待办时才推进节点。
    return "await_manual_review" if state.get("phase") == "WAITING_REVIEW" else "save_node_result"


def _after_apply_agent_reviews(state: AuditGraphState) -> str:
    # 字段事实变化后，原评估快照已失效；等待重新执行 DAG 选出的节点。
    return "save_node_result" if state.get("agent_review_recalculate") else "save_evaluation_result"


def _route_model_response(state: AuditGraphState) -> str:
    """按本轮响应决定下一步。

    - 模型请求了工具 → 先执行这一批，再回到 call_model 问下一轮；
    - 模型只给了文本 → 校验它给出的候选结论；
    - 循环无法继续（响应不可用）→ 保存并如实记录未解决事项；
    - 模型拒绝判断 → 没有候选结论，直接进入人工复核准备。
    """
    if state.get("pending_tool_calls"):
        return "execute_tool_calls"
    if state.get("loop_status") == LOOP_BLOCKED:
        return "save_evaluation_result"
    if state.get("loop_status") == LOOP_NEEDS_HUMAN:
        return "prepare_agent_review"
    return "produce_candidate_verdict"


def _route_verdict(state: AuditGraphState) -> str:
    """候选结论是否需要人工复核。

    结论与确定性工具冲突、证据不足、或工具未给出确定结果时，必须交人工，
    不能静默保存为「通过」或「不合格」。
    """
    return "prepare_agent_review" if state.get("loop_status") == LOOP_NEEDS_HUMAN else "save_evaluation_result"


def _after_advance_rule_cursor(state: AuditGraphState) -> str:
    return "finalize_node_execution" if state.get("agent_done") else "load_rule_instance"


def _after_save(state: AuditGraphState) -> str:
    if state.get("phase") == "COMPLETED":
        return "end"
    if state.get("phase") == "WAITING_REVIEW":
        return "await_manual_review"
    return "await_node_confirmation"


_builder = StateGraph(AuditGraphState, context_schema=AuditGraphContext)
_builder.add_node("load_task", _load_task)
_builder.add_node("await_node_confirmation", _await_node_confirmation)
_builder.add_node("execute_current_node", _execute_current_node)
_builder.add_node("run_deterministic_node", _run_deterministic_node)
_builder.add_node("finalize_node_execution", _finalize_node_execution)
_builder.add_node("advance_workflow", _advance_workflow)
# 规则评估 Agent 循环：确定性生命周期，模型只在授权范围内选工具。
_builder.add_node("load_evaluation", _load_evaluation)
_builder.add_node("load_rule_instance", _load_rule_instance)
_builder.add_node("prepare_agent_context", _prepare_agent_context)
_builder.add_node("call_model", _call_model)
_builder.add_node("execute_tool_calls", _execute_tool_calls)
_builder.add_node("append_tool_results", _append_tool_results)
_builder.add_node("produce_candidate_verdict", _produce_candidate_verdict)
_builder.add_node("prepare_agent_review", _prepare_agent_review)
_builder.add_node("save_evaluation_result", _save_evaluation_result)
_builder.add_node("advance_rule_cursor", _advance_rule_cursor)
_builder.add_node("prepare_manual_review", _prepare_manual_review)
_builder.add_node("await_manual_review", _await_manual_review)
_builder.add_node("apply_manual_reviews", _apply_manual_reviews)
_builder.add_node("apply_agent_reviews", _apply_agent_reviews)
_builder.add_node("save_node_result", _save_node_result)
_builder.add_edge(START, "load_task")
_builder.add_conditional_edges(
    "load_task",
    _after_load,
    {"await_node_confirmation": "await_node_confirmation", "await_manual_review": "await_manual_review"},
)
_builder.add_edge("await_node_confirmation", "execute_current_node")
_builder.add_conditional_edges(
    "execute_current_node",
    _after_execute,
    {
        "run_deterministic_node": "run_deterministic_node",
        "load_evaluation": "load_evaluation",
        "prepare_manual_review": "prepare_manual_review",
        "save_node_result": "save_node_result",
    },
)
# 节点执行的收尾与推进：两条路径在这里汇合。
_builder.add_edge("run_deterministic_node", "finalize_node_execution")
_builder.add_edge("finalize_node_execution", "advance_workflow")
_builder.add_edge("advance_workflow", "save_node_result")
# Agent 循环的边：冻结规则实例 → 准备上下文 → 调模型 →（按响应路由）。
_builder.add_edge("load_evaluation", "load_rule_instance")
_builder.add_edge("load_rule_instance", "prepare_agent_context")
_builder.add_conditional_edges(
    "prepare_agent_context",
    _route_prepared_context,
    {"call_model": "call_model", "prepare_agent_review": "prepare_agent_review"},
)
_builder.add_conditional_edges(
    "call_model",
    _route_model_response,
    {
        "execute_tool_calls": "execute_tool_calls",
        "produce_candidate_verdict": "produce_candidate_verdict",
        "prepare_agent_review": "prepare_agent_review",
        "save_evaluation_result": "save_evaluation_result",
    },
)
# 工具执行与结果回填是两个节点：执行成功但消息没写进 State 这种半成品状态必须分得清。
_builder.add_edge("execute_tool_calls", "append_tool_results")
_builder.add_edge("append_tool_results", "call_model")
_builder.add_conditional_edges(
    "produce_candidate_verdict",
    _route_verdict,
    {"prepare_agent_review": "prepare_agent_review", "save_evaluation_result": "save_evaluation_result"},
)
# 人工复核等待点：Agent 复核批与人工复核节点批共用同一个 interrupt 节点。
_builder.add_edge("prepare_agent_review", "await_manual_review")
_builder.add_conditional_edges(
    "apply_agent_reviews",
    _after_apply_agent_reviews,
    {"save_node_result": "save_node_result", "save_evaluation_result": "save_evaluation_result"},
)
# 保存规则结果后推进游标：还有规则就冻结下一条，全部处理完才收尾节点。
_builder.add_edge("save_evaluation_result", "advance_rule_cursor")
_builder.add_conditional_edges(
    "advance_rule_cursor",
    _after_advance_rule_cursor,
    {"load_rule_instance": "load_rule_instance", "finalize_node_execution": "finalize_node_execution"},
)
_builder.add_conditional_edges(
    "prepare_manual_review",
    _after_prepare_manual_review,
    {"await_manual_review": "await_manual_review", "save_node_result": "save_node_result"},
)
_builder.add_conditional_edges(
    "await_manual_review",
    _after_await_manual_review,
    {"apply_agent_reviews": "apply_agent_reviews", "apply_manual_reviews": "apply_manual_reviews"},
)
_builder.add_conditional_edges(
    "apply_manual_reviews",
    _after_apply_manual_reviews,
    {"await_manual_review": "await_manual_review", "save_node_result": "save_node_result"},
)
_builder.add_conditional_edges(
    "save_node_result",
    _after_save,
    {"await_node_confirmation": "await_node_confirmation", "await_manual_review": "await_manual_review", "end": END},
)


def _config(thread_id: str, graph_namespace: str) -> dict[str, Any]:
    """恢复身份只由 thread_id 决定。

    checkpoint_ns 使用 LangGraph 的默认根命名空间；graph_namespace 属于业务批次信息，
    保存在图 state 与 WorkflowCheckpoint 中，不能当作任意业务字段混用。
    """
    if not thread_id or not graph_namespace:
        raise BusinessError("AUDIT_THREAD_MISSING", "审核流程缺少线程标识", status_code=500)
    return {"configurable": {"thread_id": thread_id}}


def _compiled(saver: Any) -> Any:
    # 每次调用用当次的有效 saver 重新构图；saver 关闭后下次调用可重新读取同一 thread。
    return _builder.compile(checkpointer=saver)


def _storage_unavailable(exc: Exception) -> BusinessError:
    """把检查点读取异常转成可识别的错误码，供调用方与「检查点缺失」分开处理。"""
    return BusinessError(
        CHECKPOINT_STORAGE_UNAVAILABLE,
        "审核流程检查点存储暂时不可达，本次操作未完成，可稍后重试",
        status_code=500,
        details={"reason": str(exc) or exc.__class__.__name__},
    )


def _graph_failure(exc: Exception) -> BusinessError:
    """把从图里抛出的非业务异常分成两类，不再一律包装成「检查点不可达」。

    只有驱动或 SQLAlchemy 的连接、数据库运行故障才是
    「检查点存储暂时不可达」，语义是「可稍后重试」。其余异常是执行本身的缺陷（快照唯一键
    冲突、JSON 序列化失败、工具参数错误、代码 bug），包装成 `CHECKPOINT_STORAGE_UNAVAILABLE`
    会指错方向——用户以为过一会儿重试就好，真实原因被藏起来。因此保留独立错误码，并把异常
    类型与出错位置（`exception_summary`）一起带回。

    业务错误不经过本函数：调用方先 `except BusinessError: raise`，快照冲突、输入缺失、
    工具参数非法等都保留各自原本的错误码。
    """
    if is_storage_unavailable(exc):
        logger.exception("审核流程检查点/数据库操作失败", exc_info=exc)
        return _storage_unavailable(exc)
    logger.exception("审核流程图执行发生未处理异常", exc_info=exc)
    return BusinessError(
        "AUDIT_GRAPH_EXECUTION_FAILED",
        "审核流程图执行失败，本次操作未完成；失败原因见执行记录与服务端日志",
        status_code=500,
        details={"reason": exception_summary(exc)},
    )


def probe_interrupt(thread_id: str, expected_node: str) -> str:
    """把检查点读取结果分成三类，调用方不再只能靠异常区分“不可达”与“确实不在等待”。

    PENDING=确实停在 expected_node 的 interrupt；NOT_WAITING=能读到但没有该等待点；
    STORAGE_UNAVAILABLE=连接或读取异常（临时故障，不代表等待点丢失）。
    """
    if not thread_id:
        raise BusinessError("AUDIT_THREAD_MISSING", "审核流程缺少线程标识", status_code=500)
    try:
        with mysql_saver() as saver:
            snapshot = _compiled(saver).get_state({"configurable": {"thread_id": thread_id}})
    except BusinessError:
        raise
    except Exception as exc:  # noqa: BLE001 - 这里是分类函数，存储故障收敛为可识别结果
        if not is_storage_unavailable(exc):
            # 非存储层异常不能报成「检查点存储不可达」：那是可重试的临时故障，
            # 而这类异常是执行本身的缺陷，必须按真实原因抛出。
            raise _graph_failure(exc) from exc
        logger.exception("审核流程检查点读取失败 thread_id=%s", thread_id, exc_info=exc)
        return "STORAGE_UNAVAILABLE"
    if snapshot is not None and expected_node in snapshot.next and snapshot.interrupts:
        return "PENDING"
    return "NOT_WAITING"


def audit_interrupt_is_pending(thread_id: str, graph_namespace: str, expected_node: str) -> bool:
    """该等待点是否确实停在预期节点；检查点存储不可达时明确报错，不当作「没在等待」。"""
    try:
        with mysql_saver() as saver:
            snapshot = _compiled(saver).get_state(_config(thread_id, graph_namespace))
    except BusinessError:
        raise
    except Exception as exc:
        # 只有存储层故障才映射成 CHECKPOINT_STORAGE_UNAVAILABLE（见 _graph_failure）：
        # 快照唯一键冲突、JSON 序列化失败、工具参数错误等必须保留真实原因。
        raise _graph_failure(exc) from exc
    return bool(snapshot and expected_node in snapshot.next and snapshot.interrupts)


def open_audit_interrupt(
    task_no: str,
    thread_id: str,
    graph_namespace: str,
    db: Any,
    nodes: AuditNodeRunner,
) -> dict[str, Any]:
    """开始执行当前节点：建立图实例并停到下一个等待点。"""
    try:
        with mysql_saver() as saver:
            result = _compiled(saver).invoke(
                {"task_no": task_no, "graph_namespace": graph_namespace},
                _config(thread_id, graph_namespace),
                context={"db": db, "nodes": nodes},
            )
    except BusinessError:
        raise
    except Exception as exc:
        # 只有存储层故障才映射成 CHECKPOINT_STORAGE_UNAVAILABLE（见 _graph_failure）：
        # 快照唯一键冲突、JSON 序列化失败、工具参数错误等必须保留真实原因。
        raise _graph_failure(exc) from exc
    if not result.get("__interrupt__"):
        raise BusinessError("AUDIT_INTERRUPT_MISSING", "审核流程未产生预期等待点", status_code=500)
    return result


def resume_audit_confirmation(
    thread_id: str,
    graph_namespace: str,
    node_code: str,
    db: Any,
    nodes: AuditNodeRunner,
) -> dict[str, Any]:
    """从节点确认等待点继续执行。"""
    try:
        with mysql_saver() as saver:
            return _compiled(saver).invoke(
                Command(resume={"node_code": node_code}),
                _config(thread_id, graph_namespace),
                context={"db": db, "nodes": nodes},
            )
    except BusinessError:
        raise
    except Exception as exc:
        # 只有存储层故障才映射成 CHECKPOINT_STORAGE_UNAVAILABLE（见 _graph_failure）：
        # 快照唯一键冲突、JSON 序列化失败、工具参数错误等必须保留真实原因。
        raise _graph_failure(exc) from exc


def resume_audit_manual_review(
    thread_id: str,
    graph_namespace: str,
    decisions: list[dict[str, Any]],
    db: Any,
    nodes: AuditNodeRunner,
) -> dict[str, Any]:
    """从人工复核等待点继续执行，并交回本批裁决。"""
    try:
        with mysql_saver() as saver:
            return _compiled(saver).invoke(
                Command(resume=decisions),
                _config(thread_id, graph_namespace),
                context={"db": db, "nodes": nodes},
            )
    except BusinessError:
        raise
    except Exception as exc:
        # 只有存储层故障才映射成 CHECKPOINT_STORAGE_UNAVAILABLE（见 _graph_failure）：
        # 快照唯一键冲突、JSON 序列化失败、工具参数错误等必须保留真实原因。
        raise _graph_failure(exc) from exc
