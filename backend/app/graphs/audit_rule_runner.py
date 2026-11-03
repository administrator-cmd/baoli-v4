"""单条审核规则的执行循环；规则数量和模型轮次不占用任务图步数。"""

import logging
from typing import Any, Protocol

from app.constants.status import LOOP_BLOCKED, LOOP_DONE, LOOP_RUNNING
from app.domain.execution_mode import MODE_AGGREGATE

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

    def graph_build_aggregate_verdict(
        self, db: Any, task_no: str, node_code: str, execution_id: int | None,
        rule_instance: dict[str, Any] | None, planned_rule: dict[str, Any],
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
        tool_results: list[dict[str, Any]] | None,
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


def _batch_key(rule: dict[str, Any]) -> tuple[str, str | None] | None:
    """可批量抽取的标识：无输入、单字段的抽取规则，键为（材料，产物来源）。

    同批规则必须在同一份材料、同一种产物上抽取，否则一次读取覆盖不了全部字段。
    """
    if rule.get("kind") != "EXTRACTION" or rule.get("inputs"):
        return None
    if any(rule.get(key) for key in (
        "dependency_error", "unavailable_dependencies", "source_rule_key",
        "source_rule_keys", "source_rule_codes", "input_gaps",
    )):
        return None
    definitions = rule.get("output_definitions") or []
    if len(definitions) != 1 or not definitions[0].get("material_key"):
        return None
    return definitions[0]["material_key"], definitions[0].get("artifact_source")


def extraction_group(plan: list[dict[str, Any]], start: int) -> list[dict[str, Any]]:
    """从 start 起连续同键的抽取项；第一个不是可批量项时只返回它自己。

    抽取项之间没有依赖，因此同键的抽取在计划里总是相邻的；不跨过其他规则合并，
    避免把有依赖的检查项夹带进同一个模型调用。
    """
    entry = plan[start]
    key = _batch_key(entry["rule"])
    group = [entry]
    if key is None:
        return group
    cursor = start + 1
    while cursor < len(plan) and _batch_key(plan[cursor]["rule"]) == key:
        group.append(plan[cursor])
        cursor += 1
    return group


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
    rule_key = state["rule_instance"]["rule_key"]
    logger.info("开始执行审核规则 task_no=%s rule_key=%s cursor=%s", task_no, rule_key, rule_cursor)

    if str(planned_rule.get("execution_mode") or "") == MODE_AGGREGATE:
        built = nodes.graph_build_aggregate_verdict(
            db, task_no, node_code, execution_id, state["rule_instance"], planned_rule,
        )
        saved = nodes.graph_save_evaluation_result(
            db, task_no, node_code, execution_id, state["rule_instance"],
            built["candidate_verdict"], LOOP_DONE, built.get("unresolved_items"),
            state["evaluation_id"], 0,
        )
        logger.info("组合汇总执行结束 task_no=%s rule_key=%s status=%s",
                    task_no, rule_key, saved["rule_outcome"]["status"])
        return saved["rule_outcome"]

    state.update(nodes.graph_prepare_agent_context(
        db, task_no, node_code, planned_rule, state["rule_instance"], state["messages"],
    ))

    channel_failure: dict[str, Any] | None = None
    while True:
        turn = nodes.graph_call_model(
            db, task_no, node_code, planned_rule, state["messages"], state["allowed_tools"],
            state["loop_count"], state["evaluation_id"], state["tool_results"],
        )
        # 渠道故障已经由模型步记录为本条未完成；这里原样带给调度方，用于停止后续请求。
        if turn.get("channel_failure"):
            channel_failure = turn["channel_failure"]
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
            if isinstance(result.get("channel_failure"), dict):
                # 工具内部的大模型请求（风险判断等）遇到渠道故障：本条按渠道故障结束，
                # 故障原样带给调度方停止后续请求；这里不抛异常，避免外层先落成通用失败。
                channel_failure = result["channel_failure"]
                state["loop_status"] = LOOP_BLOCKED
                break
            state.update(nodes.graph_append_tool_results(state["messages"], state["tool_results"]))
            continue

        if state["loop_status"] == LOOP_BLOCKED:
            break
        state.update(nodes.graph_produce_candidate_verdict(
            db, task_no, node_code, state["rule_instance"], state["last_model_response"],
            state["tool_results"], state["loop_status"],
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
    if channel_failure is not None:
        # 结果已经按真实原因保存；渠道故障单独带出，调用方据此暂停调度而不是继续请求。
        outcome = {**outcome, "channel_failure": channel_failure}
    return outcome
