import logging
from typing import Any, Protocol, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime
from langgraph.types import Command, interrupt

from ..errors import BusinessError
from ..runtime.checkpoint import mysql_saver


logger = logging.getLogger(__name__)

# 检查点存储暂时不可达（连接/读取异常）与「检查点确实不在等待」是两件事：
# 调用方据此区分“本次操作可重试”与“业务等待点已丢失”，不能一律按缺失处理。
CHECKPOINT_STORAGE_UNAVAILABLE = "CHECKPOINT_STORAGE_UNAVAILABLE"


class AuditGraphState(TypedDict, total=False):
    """图 state 只放可持久化、可 JSON 序列化的流程引用，不放业务对象或数据库会话。"""

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


class AuditNodeRunner(Protocol):
    """图与节点实现之间的契约。

    图只负责状态推进、interrupt、resume 与节点调度；节点内部的业务规则在
    `services.audit.flow` 里，由调用方在**调用时**注入，图模块因此不导入服务层，
    `graphs` 与 `services` 之间没有模块级依赖，也没有循环。

    这里把五个节点入口写成显式协议，而不是 `Any`：函数改名、缺一个入口或签名变化
    会在类型检查（mypy / pyright）与实现自检时暴露，不会拖到真实图执行才失败。
    """

    def graph_load_task(self, db: Any, task_no: str, graph_namespace: str) -> dict[str, Any]: ...

    def graph_execute_current_node(
        self,
        db: Any,
        task_no: str,
        graph_namespace: str,
        current_node: str,
        confirmed_node: str | None,
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


class AuditGraphContext(TypedDict):
    """单次调用的依赖。它们绝不写入图 state。

    `nodes` 是本次调用要用的节点实现（`services.audit.flow` 模块），必须满足
    `AuditNodeRunner`。它在调用时注入，因此图模块不依赖服务层。
    """

    db: Any
    nodes: AuditNodeRunner


def _flow(runtime: Runtime[AuditGraphContext]) -> AuditNodeRunner:
    return runtime.context["nodes"]


def _load_task(state: AuditGraphState, runtime: Runtime[AuditGraphContext]) -> dict[str, Any]:
    flow = _flow(runtime)
    return flow.graph_load_task(
        runtime.context["db"],
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
    flow = _flow(runtime)
    return flow.graph_execute_current_node(
        runtime.context["db"],
        state["task_no"],
        state["graph_namespace"],
        state["current_node"],
        state.get("confirmed_node"),
    )


def _prepare_manual_review(state: AuditGraphState, runtime: Runtime[AuditGraphContext]) -> dict[str, Any]:
    flow = _flow(runtime)
    return flow.graph_prepare_manual_review(
        runtime.context["db"],
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
        }
    )
    if not isinstance(decisions, list) or not all(isinstance(item, dict) for item in decisions):
        raise BusinessError("MANUAL_REVIEW_PAYLOAD_INVALID", "人工复核提交内容无效", status_code=409)
    return {"review_decisions": decisions, "last_error": None}


def _apply_manual_reviews(state: AuditGraphState, runtime: Runtime[AuditGraphContext]) -> dict[str, Any]:
    flow = _flow(runtime)
    return flow.graph_apply_manual_reviews(
        runtime.context["db"],
        state["task_no"],
        state["graph_namespace"],
        state.get("review_hash"),
        state.get("review_count"),
        state.get("review_decisions"),
    )


def _save_node_result(state: AuditGraphState, runtime: Runtime[AuditGraphContext]) -> dict[str, Any]:
    flow = _flow(runtime)
    return flow.graph_save_node_result(
        runtime.context["db"],
        state["task_no"],
        state["graph_namespace"],
    )


def _after_load(state: AuditGraphState) -> str:
    return "await_manual_review" if state.get("phase") == "WAITING_REVIEW" else "await_node_confirmation"


def _after_execute(state: AuditGraphState) -> str:
    # 执行到人工复核节点时先进入 prepare_manual_review：那里可能把任务留在
    # WAITING_REVIEW（复核分批）或显式跳过并推进，两种情况由它的条件边处理。
    return "prepare_manual_review" if state.get("executed_node") == "MANUAL_REVIEW" else "save_node_result"


def _after_prepare_manual_review(state: AuditGraphState) -> str:
    return "await_manual_review" if state.get("phase") == "WAITING_REVIEW" else "save_node_result"


def _after_apply_manual_reviews(state: AuditGraphState) -> str:
    # 本批提交后仍有有效待办时，任务留在同一等待点并重建集合哈希，
    # 必须回到 await_manual_review 等下一批；只有没有剩余待办时才推进节点。
    return "await_manual_review" if state.get("phase") == "WAITING_REVIEW" else "save_node_result"


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
_builder.add_node("prepare_manual_review", _prepare_manual_review)
_builder.add_node("await_manual_review", _await_manual_review)
_builder.add_node("apply_manual_reviews", _apply_manual_reviews)
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
    {"prepare_manual_review": "prepare_manual_review", "save_node_result": "save_node_result"},
)
_builder.add_conditional_edges(
    "prepare_manual_review",
    _after_prepare_manual_review,
    {"await_manual_review": "await_manual_review", "save_node_result": "save_node_result"},
)
_builder.add_edge("await_manual_review", "apply_manual_reviews")
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
    except Exception as exc:  # noqa: BLE001 - 这里是分类函数，必须把读取异常收敛为可识别结果
        logger.exception("审核流程检查点读取失败 thread_id=%s", thread_id, exc_info=exc)
        return "STORAGE_UNAVAILABLE"
    if snapshot is not None and expected_node in snapshot.next and snapshot.interrupts:
        return "PENDING"
    return "NOT_WAITING"


def audit_interrupt_is_pending(thread_id: str, graph_namespace: str, expected_node: str) -> bool:
    try:
        with mysql_saver() as saver:
            snapshot = _compiled(saver).get_state(_config(thread_id, graph_namespace))
    except BusinessError:
        raise
    except Exception as exc:
        raise _storage_unavailable(exc) from exc
    return bool(snapshot and expected_node in snapshot.next and snapshot.interrupts)


def open_audit_interrupt(
    task_no: str,
    thread_id: str,
    graph_namespace: str,
    db: Any,
    nodes: AuditNodeRunner,
) -> dict[str, Any]:
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
        raise _storage_unavailable(exc) from exc
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
        raise _storage_unavailable(exc) from exc


def resume_audit_manual_review(
    thread_id: str,
    graph_namespace: str,
    decisions: list[dict[str, Any]],
    db: Any,
    nodes: AuditNodeRunner,
) -> dict[str, Any]:
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
        raise _storage_unavailable(exc) from exc
