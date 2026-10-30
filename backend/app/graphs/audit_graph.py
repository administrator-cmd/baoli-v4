"""固定审核任务图；规则遍历和模型循环由独立执行器完成。"""

import logging
from typing import Any, Protocol, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime

from ..common.exceptions import BusinessError, exception_summary
from ..core.database import is_storage_unavailable
from .audit_rule_runner import RuleEvaluationRunner, extraction_group, run_rule_evaluation

logger = logging.getLogger(__name__)
STORAGE_UNAVAILABLE = "STORAGE_UNAVAILABLE"


class AuditGraphState(TypedDict, total=False):
    """本次任务的执行引用、固定规则计划与汇总，不保存单条模型消息。"""

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
    evaluation_plan: list[dict[str, Any]]
    field_review: dict[str, Any] | None
    rule_outcomes: list[dict[str, Any]]
    loop_count: int
    channel_failure: dict[str, Any] | None
    field_review_paused: bool
    covered_rule_keys: list[str]


class AuditNodeRunner(RuleEvaluationRunner, Protocol):
    """任务生命周期和单条规则入口由服务层注入，图不导入服务实现。"""

    def graph_load_task(self, db: Any, task_no: str) -> dict[str, Any]: ...

    def graph_execute_current_node(
        self, db: Any, task_no: str, current_node: str,
    ) -> dict[str, Any]: ...

    def graph_prepare_materials(
        self, db: Any, task_no: str, execution_id: int | None,
    ) -> dict[str, Any]: ...

    def graph_load_evaluation(
        self, db: Any, task_no: str, node_code: str, execution_id: int | None,
    ) -> dict[str, Any]: ...

    def graph_execute_extraction_batch(
        self, db: Any, task_no: str, node_code: str, execution_id: int | None,
        entries: list[dict[str, Any]],
    ) -> list[dict[str, Any]]: ...

    def graph_pause_scheduling(
        self, db: Any, task_no: str, execution_id: int | None,
        failure: dict[str, Any],
    ) -> dict[str, Any]: ...

    def graph_finalize_node_execution(
        self, db: Any, task_no: str, execution_id: int | None, execution_mode: str,
        node_result: dict[str, Any] | None, agent_summary: dict[str, Any] | None,
    ) -> dict[str, Any]: ...

    def graph_advance_workflow(
        self, db: Any, task_no: str, execution_id: int | None,
    ) -> dict[str, Any]: ...

    def graph_save_node_result(self, db: Any, task_no: str) -> dict[str, Any]: ...


class AuditGraphContext(TypedDict):
    """会话与服务实现只放调用上下文，不进入图状态。"""

    db: Any
    nodes: AuditNodeRunner


def _load_task(state: AuditGraphState, runtime: Runtime[AuditGraphContext]) -> dict[str, Any]:
    return runtime.context["nodes"].graph_load_task(runtime.context["db"], state["task_no"])


def _execute_current_node(state: AuditGraphState, runtime: Runtime[AuditGraphContext]) -> dict[str, Any]:
    return runtime.context["nodes"].graph_execute_current_node(
        runtime.context["db"], state["task_no"], state["current_node"],
    )


def _prepare_materials(state: AuditGraphState, runtime: Runtime[AuditGraphContext]) -> dict[str, Any]:
    return runtime.context["nodes"].graph_prepare_materials(
        runtime.context["db"], state["task_no"], state["execution_id"],
    )


def _load_evaluation(state: AuditGraphState, runtime: Runtime[AuditGraphContext]) -> dict[str, Any]:
    loaded = runtime.context["nodes"].graph_load_evaluation(
        runtime.context["db"], state["task_no"], state["current_node"], state["execution_id"],
    )
    return {
        "agent_mode": loaded["agent_mode"], "evaluation_plan": loaded["evaluation_plan"],
        "field_review": loaded["field_review"],
        "rule_outcomes": [], "loop_count": 0, "last_error": loaded["last_error"],
    }


def _execute_rules(state: AuditGraphState, runtime: Runtime[AuditGraphContext]) -> dict[str, Any]:
    """按依赖计划串行执行；同一材料上相邻的无输入抽取合并为一次调用。

    合并只发生在连续的、同材料同产物来源的单字段抽取之间：它们互不依赖，逐个执行会把同一份
    正文重复送进模型。每条字段的结果仍各自保存、各自成败，不因同批其他字段失败而连带阻断。

    字段核对边界由执行计划给出：当前字段版本尚未确认时只执行抽取段，执行完即停在核对等待点；
    已确认时执行核对后的其余规则。两条路径用同一份 allowed_rule_keys，不从入口各自判断。

    模型渠道故障是唯一的提前结束：渠道不可用时后续每条规则都会失败，继续请求只会重复排队和
    计费。已经形成的结果按单一规则或按字段逐条提交，未完成的规则留在同一批次里等待继续执行。
    """
    review = state.get("field_review")
    if review is None:
        # 没有核对信息（非当前执行入口）时按完整计划执行；有列表但为空表示本轮不执行任何规则。
        plan = list(state["evaluation_plan"])
    else:
        allowed = set(review.get("allowed_rule_keys") or [])
        plan = [entry for entry in state["evaluation_plan"] if entry["rule_key"] in allowed]
    outcomes: list[dict[str, Any]] = []
    index = 0
    channel_failure: dict[str, Any] | None = None
    while index < len(plan):
        group = extraction_group(plan, index)
        index += len(group)
        if len(group) > 1:
            produced = runtime.context["nodes"].graph_execute_extraction_batch(
                runtime.context["db"], state["task_no"], state["current_node"],
                state["execution_id"], group,
            )
        else:
            entry = group[0]
            produced = [run_rule_evaluation(
                runtime.context["db"], runtime.context["nodes"], task_no=state["task_no"],
                node_code=state["current_node"], execution_id=state["execution_id"],
                rule_cursor=entry["cursor"], planned_rule=entry["rule"],
            )]
        outcomes.extend(produced)
        failure = next(
            (item["channel_failure"] for item in produced if item.get("channel_failure")), None,
        )
        if failure is not None:
            channel_failure = failure
            # 进度先落库再结束：即使收尾阶段再出错，页面也能看到停在哪一条、为什么停。
            runtime.context["nodes"].graph_pause_scheduling(
                runtime.context["db"], state["task_no"], state["execution_id"], failure=failure,
            )
            break
    return {
        "rule_outcomes": outcomes,
        "loop_count": sum(item["loop_count"] for item in outcomes),
        "channel_failure": channel_failure,
        # 抽取段跑完就先停在核对等待点；渠道故障优先按失败处理，不写成等待核对。
        "field_review_paused": bool(
            review
            and review.get("required")
            and not review.get("confirmed")
            and channel_failure is None
        ),
        "covered_rule_keys": [entry["rule_key"] for entry in plan],
    }


def collect_agent_summary(
    plan_keys: set[str], outcomes: list[dict[str, Any]], loop_count: int,
) -> dict[str, Any]:
    """按真实逐条结果汇总批次：遍历完不等于业务通过，未执行项与阻断原因照实记录。"""
    missing_keys = plan_keys - {item["rule_key"] for item in outcomes}
    statuses = {item["loop_status"] for item in outcomes}
    overall = "DONE"
    for status in ("BLOCKED", "NEEDS_HUMAN"):
        if status in statuses:
            overall = status
            break
    if missing_keys:
        overall = "BLOCKED"
    summary = {
        "agent_rule_count": len(outcomes),
        "missing_rule_keys": sorted(missing_keys), "rule_outcomes": outcomes,
        "loop_status": overall, "loop_count": loop_count,
        "unresolved_items": [item for outcome in outcomes for item in outcome["unresolved_items"]],
    }
    failure = next((item["channel_failure"] for item in outcomes if item.get("channel_failure")), None)
    if failure is not None:
        summary["channel_failure"] = failure
    return summary


def _collect_agent_summary(state: AuditGraphState) -> dict[str, Any]:
    """遍历完不等于业务通过，未执行项与阻断原因按真实结果汇总。

    本轮只覆盖计划的一部分时（停在字段核对等待点），缺项判定也只按这一部分算：
    核对后才执行的规则不是「未完成」。
    """
    covered = state.get("covered_rule_keys")
    plan_keys = (
        {item["rule_key"] for item in state["evaluation_plan"]} if covered is None else set(covered)
    )
    summary = collect_agent_summary(plan_keys, state["rule_outcomes"], state["loop_count"])
    if state.get("field_review_paused"):
        summary["field_review_paused"] = True
    return summary


def _finalize_node_execution(state: AuditGraphState, runtime: Runtime[AuditGraphContext]) -> dict[str, Any]:
    return runtime.context["nodes"].graph_finalize_node_execution(
        runtime.context["db"], state["task_no"], state["execution_id"], state["execution_mode"],
        state["node_result"], _collect_agent_summary(state),
    )


def _advance_workflow(state: AuditGraphState, runtime: Runtime[AuditGraphContext]) -> dict[str, Any]:
    return runtime.context["nodes"].graph_advance_workflow(
        runtime.context["db"], state["task_no"], state["execution_id"],
    )


def _save_node_result(state: AuditGraphState, runtime: Runtime[AuditGraphContext]) -> dict[str, Any]:
    return runtime.context["nodes"].graph_save_node_result(runtime.context["db"], state["task_no"])


def _after_execute(state: AuditGraphState) -> str:
    return "save_node_result" if state.get("last_error") is not None else "prepare_materials"


_builder = StateGraph(AuditGraphState, context_schema=AuditGraphContext)
_builder.add_node("load_task", _load_task)
_builder.add_node("execute_current_node", _execute_current_node)
_builder.add_node("prepare_materials", _prepare_materials)
_builder.add_node("load_evaluation", _load_evaluation)
_builder.add_node("execute_rules", _execute_rules)
_builder.add_node("finalize_node_execution", _finalize_node_execution)
_builder.add_node("advance_workflow", _advance_workflow)
_builder.add_node("save_node_result", _save_node_result)
_builder.add_edge(START, "load_task")
_builder.add_edge("load_task", "execute_current_node")
_builder.add_conditional_edges(
    "execute_current_node", _after_execute,
    {"prepare_materials": "prepare_materials", "save_node_result": "save_node_result"},
)
_builder.add_edge("prepare_materials", "load_evaluation")
_builder.add_edge("load_evaluation", "execute_rules")
_builder.add_edge("execute_rules", "finalize_node_execution")
_builder.add_edge("finalize_node_execution", "advance_workflow")
_builder.add_edge("advance_workflow", "save_node_result")
_builder.add_edge("save_node_result", END)


def _compiled() -> Any:
    """任务图固定为八步且无回边，不因规则数量或模型轮次扩大。"""
    return _builder.compile()


def _graph_failure(exc: Exception) -> BusinessError:
    """把图里抛出的非业务异常分成两类，避免把代码缺陷说成「可稍后重试」。

    只有驱动或 SQLAlchemy 的连接、数据库运行故障才按「存储暂时不可达」上报；其余异常
    是执行本身的缺陷（唯一键冲突、JSON 序列化失败、工具参数错误、代码 bug），必须保留
    真实原因，否则用户会以为过一会儿重试就好。

    业务错误不经过本函数：调用方先 `except BusinessError: raise`，输入缺失、
    工具参数非法等都保留各自原本的错误码。
    """
    if is_storage_unavailable(exc):
        logger.exception("审核流程数据库操作失败", exc_info=exc)
        return BusinessError(
            STORAGE_UNAVAILABLE,
            "数据库暂时不可达，本次操作未完成，可稍后重试",
            status_code=500,
            details={"reason": exception_summary(exc)},
        )
    logger.exception("审核流程图执行发生未处理异常", exc_info=exc)
    return BusinessError(
        "AUDIT_GRAPH_EXECUTION_FAILED",
        "审核流程图执行失败，本次操作未完成；失败原因见执行记录与服务端日志",
        status_code=500,
        details={"reason": exception_summary(exc)},
    )


def run_audit_node(task_no: str, db: Any, nodes: AuditNodeRunner) -> dict[str, Any]:
    """执行任务当前节点：一次调用跑完它的全部规则。"""
    try:
        return _compiled().invoke(
            {"task_no": task_no},
            context={"db": db, "nodes": nodes},
        )
    except BusinessError:
        raise
    except Exception as exc:
        raise _graph_failure(exc) from exc
