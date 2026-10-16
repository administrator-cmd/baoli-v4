"""单条审核规则的执行循环；规则数量和模型轮次不占用任务图步数。"""

import logging
from typing import Any, Protocol

from ..domain.audit_vocab import LOOP_BLOCKED, LOOP_RUNNING


logger = logging.getLogger(__name__)


class RuleEvaluationRunner(Protocol):
    """真实业务步骤由服务层注入，执行器只组织调用顺序。"""

    def graph_load_rule_instance(
        self, db: Any, task_no: str, node_code: str, rule_cursor: int,
        execution_id: int | None, planned_rule: dict[str, Any],
    ) -> dict[str, Any]: ...

    def graph_prepare_agent_context(
        self, db: Any, task_no: str, node_code: str, planned_rule: dict[str, Any],
        rule_instance: dict[str, Any] | None, messages: list[dict[str, Any]] | None,
    ) -> dict[str, Any]: ...

    def graph_call_model(
        self, db: Any, task_no: str, node_code: str, planned_rule: dict[str, Any],
        messages: list[dict[str, Any]] | None, allowed_tools: list[dict[str, Any]] | None,
        loop_count: int, state_evaluation_id: str | None,
        executed_tool_results: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]: ...

    def graph_execute_tool_calls(
        self, db: Any, task_no: str, node_code: str,
        pending_tool_calls: list[dict[str, Any]] | None,
        allowed_tools: list[dict[str, Any]] | None, rule_instance: dict[str, Any] | None,
        existing_unresolved: list[dict[str, Any]] | None,
    ) -> dict[str, Any]: ...

    def graph_append_tool_results(
        self, messages: list[dict[str, Any]] | None,
        tool_results: list[dict[str, Any]] | None,
    ) -> dict[str, Any]: ...

    def graph_produce_candidate_verdict(
        self, db: Any, task_no: str, node_code: str,
        rule_instance: dict[str, Any] | None, last_model_response: dict[str, Any] | None,
        tool_results: list[dict[str, Any]] | None, allowed_tools: list[dict[str, Any]] | None,
        loop_status: str, unresolved_items: list[dict[str, Any]] | None,
        messages: list[dict[str, Any]] | None, verdict_errors: list[dict[str, Any]] | None,
    ) -> dict[str, Any]: ...

    def graph_save_evaluation_result(
        self, db: Any, task_no: str, node_code: str, execution_id: int | None,
        rule_instance: dict[str, Any] | None, candidate_verdict: dict[str, Any] | None,
        loop_status: str, unresolved_items: list[dict[str, Any]] | None,
        evaluation_id: str | None, loop_count: int,
        last_model_response: dict[str, Any] | None = None,
        verdict_errors: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]: ...


def run_rule_evaluation(
    db: Any, nodes: RuleEvaluationRunner, *, task_no: str, node_code: str,
    execution_id: int | None, rule_cursor: int, planned_rule: dict[str, Any],
) -> dict[str, Any]:
    """加载一条规则，完成模型/工具循环并提交结果，再释放本条消息与工具内容。

    每次调用独立创建状态，不携带上一条规则的消息、产物或轮次。
    模型轮次仍由 graph_call_model 按现有配置控制（0 表示不限）。
    单条业务失败由服务层明确记录；存储及程序异常原样向任务调用方传播。
    """
    state = nodes.graph_load_rule_instance(db, task_no, node_code, rule_cursor, execution_id, planned_rule)
    state.update(nodes.graph_prepare_agent_context(
        db, task_no, node_code, planned_rule, state["rule_instance"], state["messages"],
    ))
    rule_key = state["rule_instance"]["rule_key"]
    logger.info("开始执行审核规则 task_no=%s rule_key=%s cursor=%s", task_no, rule_key, rule_cursor)

    while True:
        turn = nodes.graph_call_model(
            db, task_no, node_code, planned_rule, state["messages"], state["allowed_tools"],
            state["loop_count"], state["evaluation_id"], state["tool_results"],
        )
        if "unresolved_items" in turn:
            turn["unresolved_items"] = [*state["unresolved_items"], *turn["unresolved_items"]]
        state.update(turn)

        if state["pending_tool_calls"]:
            result = nodes.graph_execute_tool_calls(
                db, task_no, node_code, state["pending_tool_calls"], state["allowed_tools"],
                state["rule_instance"], state["unresolved_items"],
            )
            result["tool_results"] = [*state["tool_results"], *result["tool_results"]]
            state.update(result)
            state.update(nodes.graph_append_tool_results(state["messages"], state["tool_results"]))
            continue

        if state["loop_status"] == LOOP_BLOCKED:
            break
        state.update(nodes.graph_produce_candidate_verdict(
            db, task_no, node_code, state["rule_instance"], state["last_model_response"],
            state["tool_results"], state["allowed_tools"], state["loop_status"],
            state["unresolved_items"], state["messages"], state["verdict_errors"],
        ))
        if state["loop_status"] != LOOP_RUNNING:
            break

    saved = nodes.graph_save_evaluation_result(
        db, task_no, node_code, execution_id, state["rule_instance"], state["candidate_verdict"],
        state["loop_status"], state["unresolved_items"], state["evaluation_id"],
        state["loop_count"], state["last_model_response"], state["verdict_errors"],
    )
    outcome = saved["rule_outcome"]
    logger.info("审核规则执行结束 task_no=%s rule_key=%s status=%s loop_count=%s",
                task_no, rule_key, outcome["status"], outcome["loop_count"])
    return outcome
