"""生效规则的模型/工具循环与真实结果保存；人工审核暂不启用。

RULE_EXECUTION 只关联执行记录，不划分业务阶段；单条无法执行会保存原因并继续。
"""

import json
import logging
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any, Iterator

from langchain_core.utils.function_calling import convert_to_openai_tool
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from ...common.exceptions import BusinessError, exception_summary
from ...domain.audit_vocab import LOOP_BLOCKED, LOOP_DONE, LOOP_NEEDS_HUMAN, LOOP_RUNNING
from ...domain.rule_library import active, is_risk_rule
from ...entity import AuditTask, NodeExecution, RuleResult
from ..task_lookup import task_or_404
from .reviews import _invalidate_retry_results
from .rule_results import save_rule_result
from .shared import _material_names, _scenario, _task_manifest
from .state import _scenario_nodes, _set_task_checkpoint, _task_checkpoint


logger = logging.getLogger(__name__)

MODE_AGENT = "AGENT"

# 被服务重启打断、由 `commands.recover_interrupted_executions` 写成 FAILED 的执行记录。
# 这类终态是「进程被杀」而不是「节点真的失败」，Agent 路径在人工复核之后仍会收尾，
# 因此允许被真实结果覆盖，并在结果里保留原失败信息。
_INTERRUPTED_FAILURE_CODE = "AUDIT_EXECUTION_INTERRUPTED"


def graph_load_task(db: Session, task_no: str) -> dict[str, Any]:
    """加载任务并校验它确实停在可执行的节点上。"""
    task = task_or_404(db, task_no)
    if task.status in {"COMPLETED", "FAILED"}:
        raise BusinessError("AUDIT_TASK_FINISHED", "审核任务已结束，不能继续执行", status_code=409)
    _, _, current_index = _scenario_nodes(db, task)
    progress = _task_checkpoint(task)
    if progress.get("current_node") != task.current_node or progress.get("node_index") != current_index:
        raise BusinessError("AUDIT_PROGRESS_INVALID", "任务进度与当前节点不一致，不能继续执行", status_code=409)
    phase = progress.get("phase")
    if phase != "AWAITING_CONFIRMATION":
        raise BusinessError("AUDIT_PROGRESS_INVALID", "任务当前不处于可执行状态", status_code=409)
    return {
        "current_node": task.current_node,
        "node_index": current_index,
        "phase": phase,
        "last_execution": None,
        "last_error": None,
    }

def _graph_error_state(db: Session, task_no: str, node_code: str, exc: BusinessError) -> dict[str, Any]:
    task = task_or_404(db, task_no)
    progress = _task_checkpoint(task)
    _, _, current_index = _scenario_nodes(db, task)
    return {
        "executed_node": node_code,
        "current_node": task.current_node,
        "node_index": current_index,
        "phase": progress.get("phase"),
        "last_error": {
            "code": exc.code,
            "message": exc.message,
            "status_code": exc.status_code,
            "details": exc.details,
        },
    }

def _running_execution(db: Session, task: AuditTask) -> NodeExecution | None:
    """本任务当前执行中的节点执行记录（最多一条，由 begin 与并发校验保证）。"""
    return db.scalar(
        select(NodeExecution)
        .where(NodeExecution.audit_task_id == task.id, NodeExecution.status == "RUNNING")
        .order_by(NodeExecution.id.desc())
    )


def _execution_mode(task: AuditTask, manifest: dict[str, Any]) -> str:
    """所有生效执行项统一走模型与工具循环，不按业务阶段分发。"""
    if manifest.get("execution_contract_version") != "rule_library_v1":
        raise BusinessError("RULE_LIBRARY_EXECUTION_REQUIRED", "任务采用已移除的九阶段执行配置，不能按新执行器执行；原任务快照未被改写", status_code=409)
    return MODE_AGENT


def _enabled_rules(task: AuditTask, manifest: dict[str, Any], node_code: str) -> list[dict[str, Any]]:
    enabled_keys = set(task.task_schema_json["enabled_rule_keys"])
    return [
        rule
        for rule in manifest["rules"]
        if active(rule) and rule.get("node") == node_code and rule.get("rule_key") in enabled_keys
    ]


def graph_execute_current_node(
    db: Session,
    task_no: str,
    current_node: str,
) -> dict[str, Any]:
    """建立本次规则执行记录；实际规则统一由模型与工具循环完成。"""
    task = task_or_404(db, task_no, lock=True)
    if task.status in {"COMPLETED", "FAILED"}:
        raise BusinessError("AUDIT_TASK_FINISHED", "审核任务已结束，不能继续执行", status_code=409)
    if current_node != task.current_node:
        # 人工复核或材料裁决会把任务退回更早的节点（例如回到内部校验）：此时图 state 里记录的
        # 仍是退回前的节点。业务任务表才是当前节点的权威来源，这里按业务节点执行并留下日志，
        # 否则退回后第一次执行必然被误判为“提交节点不一致”。
        logger.info(
            "审核流程按业务当前节点执行 task_no=%s graph_node=%s business_node=%s",
            task_no,
            current_node,
            task.current_node,
        )
        current_node = task.current_node
    try:
        manifest = _task_manifest(task)
        nodes = sorted(manifest["nodes"], key=lambda item: item["sequence_no"])
        _, _, current_index = _scenario_nodes(db, task)
        mode = _execution_mode(task, manifest)
        execution = _begin_node_execution(db, task, manifest, nodes, current_index)
    except BusinessError as exc:
        db.rollback()
        return _graph_error_state(db, task_no, current_node, exc)
    db.commit()
    return {
        "executed_node": current_node,
        "current_node": task.current_node,
        "node_index": current_index,
        "phase": "RUNNING",
        "execution_id": execution.id,
        "execution_mode": mode,
        "node_result": None,
        "last_execution": None,
        "last_error": None,
    }


def _begin_node_execution(
    db: Session,
    task: AuditTask,
    manifest: dict[str, Any],
    nodes: list[dict[str, Any]],
    current_index: int,
) -> NodeExecution:
    """建立本次节点执行记录并把任务置为执行中。

    执行记录与任务状态在节点真正开始之前就已提交，
    这样进程在节点执行中崩溃时，留下的 RUNNING 执行记录是「上次执行中断」的真实表达。
    """
    if task.status == "RUNNING":
        raise BusinessError("AUDIT_TASK_RUNNING", "任务正在执行当前节点，不能并发执行或重跑", status_code=409)
    running_execution = _running_execution(db, task)
    if running_execution:
        raise BusinessError("AUDIT_NODE_RUNNING", "当前任务已有执行中的节点，不能并发执行或重跑", status_code=409)
    if task.status == "BLOCKED":
        _invalidate_retry_results(db, task, nodes, current_index)
    else:
        db.execute(
            update(NodeExecution)
            .where(
                NodeExecution.audit_task_id == task.id,
                NodeExecution.node_code == task.current_node,
                NodeExecution.status.in_(["BLOCKED", "FAILED"]),
            )
            .values(status="INVALIDATED")
        )
    sequence_no = (db.scalar(select(func.max(NodeExecution.sequence_no)).where(NodeExecution.audit_task_id == task.id)) or 0) + 1
    execution = NodeExecution(
        audit_task_id=task.id,
        scenario_version_id=task.scenario_version_id,
        node_code=task.current_node,
        sequence_no=sequence_no,
        status="RUNNING",
        started_at=datetime.now(UTC),
    )
    db.add(execution)
    db.flush()
    task.status = "RUNNING"
    return execution


def _execution_in_progress(db: Session, task: AuditTask, execution_id: int | None) -> NodeExecution:
    """本次执行记录。按 state 里的 id 取，取不到即显式失败（不模糊匹配「最新一条」）。"""
    if not isinstance(execution_id, int):
        raise BusinessError(
            "AUDIT_EXECUTION_MISSING",
            "审核流程缺少当前节点执行记录标识",
            status_code=500,
        )
    execution = db.get(NodeExecution, execution_id)
    if execution is None or execution.audit_task_id != task.id or execution.node_code != task.current_node:
        raise BusinessError(
            "AUDIT_EXECUTION_MISSING",
            "找不到当前节点的执行记录，本次执行未完成",
            status_code=500,
            details={"execution_id": execution_id, "node": task.current_node},
        )
    return execution


def graph_finalize_node_execution(
    db: Session,
    task_no: str,
    execution_id: int | None,
    execution_mode: str,
    node_result: dict[str, Any] | None,
    agent_summary: dict[str, Any] | None,
) -> dict[str, Any]:
    """收尾规则执行批次，保存真实逐条结果的汇总。"""
    task = task_or_404(db, task_no, lock=True)
    execution = _execution_in_progress(db, task, execution_id)
    interrupted = execution.status == "FAILED" and execution.failure_code == _INTERRUPTED_FAILURE_CODE
    if execution.status in {"SUCCEEDED", "SKIPPED", "BLOCKED", "FAILED", "INVALIDATED"} and not interrupted:
        return {
            "execution_status": execution.status,
            "execution_failure_code": execution.failure_code,
            "node_result": execution.result_json,
            "last_error": None,
        }
    # 服务重启把执行记录标成 FAILED 时，节点其实已经重新跑完（Agent 复核期间进程被杀）：
    # 这里写真实结果，但把原失败信息一并保留，不让一次中断在记录上消失。
    recovered_from = execution.failure_code if interrupted else None
    recovered_message = execution.failure_message if interrupted else None
    execution.finished_at = datetime.now(UTC)
    execution.result_json = {**(execution.result_json or {}), **(agent_summary or {})}
    # 遍历结束不等于执行成功。格式/产出失败在所有规则尝试完后汇总，允许重跑。
    failed = [item for item in execution.result_json.get("rule_outcomes", [])
              if item.get("loop_status") == LOOP_BLOCKED or item.get("status") == "NOT_EXECUTED"]
    if execution.result_json.get("loop_status") == LOOP_BLOCKED or failed:
        execution.status = "BLOCKED"
        execution.failure_code = "AUDIT_RULE_EXECUTION_INCOMPLETE"
        incomplete_count = len(failed) + len(execution.result_json.get("missing_rule_keys", []))
        execution.failure_message = f"本批次有 {incomplete_count} 项未完成执行；具体原因见规则结果，可处理后重新执行"
        task.status = "BLOCKED"
        _set_task_checkpoint(task, node_index=_scenario_nodes(db, task)[2], phase="AWAITING_CONFIRMATION")
        db.flush()
    else:
        execution.status = "SUCCEEDED"
    if recovered_from is not None:
        execution.result_json = {
            **(execution.result_json or {}),
            "recovered_from_interrupted_execution": {
                "failure_code": recovered_from,
                "failure_message": recovered_message,
            },
        }
        # 覆盖被中断的 FAILED 只在本次执行确实成功时做：本次又阻断/失败时，
        # 当前失败码是真实结论，不能被「上次是中断」擦掉。
        if execution.status not in {"BLOCKED", "FAILED"}:
            execution.failure_code = None
            execution.failure_message = None
    db.commit()
    return {
        "execution_status": execution.status,
        "execution_failure_code": execution.failure_code,
        "node_result": execution.result_json,
        "last_error": None,
    }


def graph_advance_workflow(
    db: Session,
    task_no: str,
    execution_id: int | None,
) -> dict[str, Any]:
    """规则批次收尾；失败保持阻断，成功结束任务。"""
    task = task_or_404(db, task_no, lock=True)
    progress = _task_checkpoint(task)
    manifest = _task_manifest(task)
    nodes = sorted(manifest["nodes"], key=lambda item: item["sequence_no"])
    _, _, current_index = _scenario_nodes(db, task)
    execution = _execution_in_progress(db, task, execution_id)
    if execution.status == "BLOCKED":
        # 必传材料缺失：任务显式阻断在当前节点，检查点不动，用户补传后可重新执行。
        return {
            "current_node": task.current_node,
            "node_index": current_index,
            "phase": progress.get("phase"),
            "last_execution": {
                "task_no": task.task_no,
                "node_name": nodes[current_index]["name"],
                "execution_status": execution.status,
                "task_status": task.status,
                "next_node_name": None,
                "result_summary": execution.result_json,
                "failure_code": execution.failure_code,
                "failure_message": execution.failure_message,
            },
            "last_error": None,
        }
    try:
        next_node = _next_node_after(db, task, manifest, nodes)
    except BusinessError as exc:
        execution.status = "BLOCKED"
        execution.failure_code = exc.code
        execution.failure_message = exc.message
        task.status = "BLOCKED"
        db.flush()
        db.commit()
        return {
            "current_node": task.current_node,
            "node_index": current_index,
            "phase": progress.get("phase"),
            "last_execution": {
                "task_no": task.task_no,
                "node_name": nodes[current_index]["name"],
                "execution_status": execution.status,
                "task_status": task.status,
                "next_node_name": None,
                "result_summary": execution.result_json,
                "failure_code": exc.code,
                "failure_message": exc.message,
            },
            "last_error": None,
        }
    if next_node:
        task.current_node = next_node["node_code"]
        task.status = "READY"
        _set_task_checkpoint(task, node_index=_index_of(nodes, next_node["node_code"]), phase="AWAITING_CONFIRMATION")
    else:
        task.status = "COMPLETED"
        _set_task_checkpoint(task, node_index=current_index, phase="COMPLETED")
    db.flush()
    db.commit()
    return {
        "current_node": task.current_node,
        "node_index": _index_of(nodes, task.current_node) if next_node else current_index,
        "phase": "AWAITING_CONFIRMATION" if next_node else "COMPLETED",
        "last_execution": {
            "task_no": task.task_no,
            "node_name": nodes[current_index]["name"],
            "execution_status": execution.status,
            "task_status": task.status,
            "next_node_name": next_node["name"] if next_node else None,
            "result_summary": execution.result_json,
        },
        "last_error": None,
    }


def _index_of(nodes: list[dict[str, Any]], node_code: str) -> int:
    for index, item in enumerate(nodes):
        if item["node_code"] == node_code:
            return index
    raise BusinessError("AUDIT_NODE_NOT_IN_SCENARIO", "节点不属于任务场景", status_code=500)


def _next_node_after(db: Session, task: AuditTask, manifest: dict[str, Any], nodes: list[dict[str, Any]]) -> None:
    """规则游标遍历完成后收尾任务，不再调度业务阶段。"""
    return None


def graph_save_node_result(db: Session, task_no: str) -> dict[str, Any]:
    """落库并提交本次节点结果与流程进度。"""
    task = task_or_404(db, task_no, lock=True)
    progress = _task_checkpoint(task)
    db.flush()
    db.commit()
    _, _, current_index = _scenario_nodes(db, task)
    return {
        "current_node": task.current_node,
        "node_index": current_index,
        "phase": progress.get("phase"),
    }

def _raise_graph_error(state: dict[str, Any]) -> None:
    error = state.get("last_error")
    if not isinstance(error, dict):
        return
    code = error.get("code")
    message = error.get("message")
    status_code = error.get("status_code")
    details = error.get("details")
    if not isinstance(code, str) or not isinstance(message, str) or not isinstance(status_code, int) or not isinstance(details, dict):
        raise BusinessError("AUDIT_GRAPH_RESULT_INVALID", "审核流程图返回了无效错误状态", status_code=500)
    raise BusinessError(code, message, status_code=status_code, details=details)


# 规则评估步骤：任务图负责固定生命周期，audit_rule_runner 组织单条模型与工具循环。
# 一次评估只处理一条规则；消息和工具结果只在本条调用的内存状态中保留。

def _agent_module() -> Any:
    """延迟导入 Agent 实现。

    `flow` 与 `agent` 同属 audit 包，但 agent 依赖 integrations.llm 与
    services.tool_service；模块级导入会让「只加载 flow」也拉起模型网关。
    延迟导入保持 import 图干净（同手法见 operation_gateway）。
    """
    from . import agent

    return agent


def _plan_entry(node_code: str, cursor: int, rule: dict[str, Any]) -> dict[str, Any]:
    """统一执行计划中的一条生效规则实例。"""
    return {
        "cursor": cursor,
        "rule_key": rule.get("rule_key"),
        "rule_code": rule.get("rule_code") or rule.get("rule_key"),
        "node_code": node_code,
        "rule": rule,
    }


def _evaluation_id(task: AuditTask, execution_id: int, rule_instance_id: str) -> str:
    """执行记录与规则实例构成评估标识。"""
    return f"evaluation:{execution_id}:{rule_instance_id}"


def _rule_instance_id(node_code: str, rule_identity: str, execution_id: int) -> str:
    """规则实例标识：同一条规则在不同节点执行批次里是不同实例。"""
    return f"{node_code}:{rule_identity}:NE{execution_id}"


def _openai_tools(allowed_tools: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """根据本次循环的实际工具列表构造模型参数。"""
    definitions = _agent_module().tool_definitions(allowed_tools)
    return [
        convert_to_openai_tool(definition)
        for definition in sorted(definitions, key=lambda item: str(item.get("name")))
    ]


@contextmanager
def _agent_step(db: Session, task_no: str) -> Iterator[None]:
    """Agent 路径单步的失败处理：先落成显式的节点失败，再原样抛出。

    模型调用与工具执行会失败（配置缺失、供应商超时、快照过期、凭据失效），也可能抛出未预期
    异常（唯一键冲突、JSON 序列化失败、工具参数错误）。两类都必须写进执行记录并把任务置
    BLOCKED，异常摘要原样保留——不记录的话任务与执行记录都停在 RUNNING，用户既不能重跑也
    看不到原因。

    错误码的分工是刻意的：`BusinessError` 的码原样上抛（快照冲突、输入缺失、工具参数非法……）；
    其余异常写 `AUDIT_NODE_UNEXPECTED_ERROR` 并把异常类型、消息与出错位置落进
    `failure_message`，同时以同一错误码抛出。这样图包装层不会把它再包成「检查点不可达」——
    那会把快照唯一键冲突、序列化失败这类真实缺陷伪装成存储故障。

    只对「确实有一次执行正在进行」的情况落失败：任务号、执行记录与图命名空间三者都对得上才写。
    对不上说明这次调用根本不属于当前流程（例如等待点已被材料替换取代），此时不碰任何记录，
    只把原错误抛出。
    """
    try:
        yield
    except BusinessError as exc:
        _fail_running_execution(db, task_no, code=exc.code, message=exc.message)
        raise
    except Exception as exc:  # noqa: BLE001 - 未预期异常必须落库后再暴露
        logger.exception(
            "规则评估发生未处理异常 task_no=%s", task_no, exc_info=exc
        )
        summary = exception_summary(exc)
        _fail_running_execution(
            db,
            task_no,
            code="AUDIT_NODE_UNEXPECTED_ERROR",
            message=summary,
        )
        raise BusinessError(
            "AUDIT_NODE_UNEXPECTED_ERROR",
            "规则评估发生未处理异常，本次未完成，失败原因见执行记录",
            status_code=500,
            details={"reason": summary},
        ) from exc


def _fail_running_execution(
    db: Session,
    task_no: str,
    *,
    code: str,
    message: str,
) -> None:
    """把当前执行记录写成 FAILED、任务置 BLOCKED，并提交。

    Agent 单步和图执行期间的存储故障共用此处。只终止匹配当前批次的 RUNNING
    执行；尚未开始或已进入确认/复核等待点的任务保持原状态。
    """
    db.rollback()
    task = db.scalar(select(AuditTask).where(AuditTask.task_no == task_no).with_for_update())
    execution = _running_execution(db, task) if task is not None else None
    if (
        task is None
        or task.status != "RUNNING"
        or execution is None
        or execution.node_code != task.current_node
    ):
        return
    execution.status = "FAILED"
    execution.finished_at = datetime.now(UTC)
    execution.failure_code = code
    execution.failure_message = message
    task.status = "BLOCKED"
    db.flush()
    db.commit()


def _mark_graph_runtime_failed(
    db: Session,
    task_no: str,
    *,
    failure_code: str = "AUDIT_GRAPH_RUNTIME_FAILED",
    failure_message: str = "审核流程运行失败，详情见后台操作失败信息与服务端日志",
) -> None:
    """图基础设施在业务分支之外失败时落一个终态，避免任务永远停在「执行中」。

    与 `_fail_running_execution` 的区别是终态：这里是 FAILED（需要显式恢复），
    那边是 BLOCKED（用户补传材料或重跑即可继续）。
    """
    db.rollback()
    task = db.scalar(select(AuditTask).where(AuditTask.task_no == task_no).with_for_update())
    if task is None:
        return
    task.status = "FAILED"
    db.execute(
        update(NodeExecution)
        .where(NodeExecution.audit_task_id == task.id, NodeExecution.status == "RUNNING")
        .values(
            status="FAILED",
            finished_at=datetime.now(UTC),
            failure_code=failure_code,
            failure_message=failure_message,
        )
    )
    db.commit()


def graph_prepare_materials(db: Session, task_no: str, execution_id: int | None) -> dict[str, Any]:
    """程序完成全部材料前置处理，再进入规则与模型循环。"""
    with _agent_step(db, task_no):
        from .preprocessing import prepare_task_materials

        task = task_or_404(db, task_no, lock=True)
        execution = _execution_in_progress(db, task, execution_id)
        prepare_task_materials(db, task, execution)
        return {"last_error": None}


def graph_load_evaluation(
    db: Session,
    task_no: str,
    node_code: str,
    execution_id: int | None,
) -> dict[str, Any]:
    """从任务冻结配置生成一次依赖计划；单条执行不重新排序。"""
    with _agent_step(db, task_no):
        task = task_or_404(db, task_no, lock=True)
        manifest = _task_manifest(task)
        execution = _execution_in_progress(db, task, execution_id)
        from ...domain.rule_library import execution_order
        agent_rules = execution_order(_enabled_rules(task, manifest, node_code), all_rules=manifest["rules"])
        return {
            "agent_mode": True,
            "evaluation_plan": [_plan_entry(node_code, index, rule) for index, rule in enumerate(agent_rules)],
            "execution_id": execution.id,
            "rule_outcomes": [],
            "loop_count": 0,
            "last_error": None,
        }


def graph_load_rule_instance(
    db: Session,
    task_no: str,
    node_code: str,
    rule_cursor: int,
    execution_id: int | None,
    planned_rule: dict[str, Any],
) -> dict[str, Any]:
    """按本次计划读取单条规则的真实输入，创建独立执行状态。"""
    with _agent_step(db, task_no):
        task = task_or_404(db, task_no, lock=True)
        execution = _execution_in_progress(db, task, execution_id)
        rule = planned_rule
        rule_code = str(rule.get("rule_code") or rule.get("rule_key"))
        agent = _agent_module()
        allowed = agent.available_tools()
        identity = rule["rule_key"]
        rule_instance_id = _rule_instance_id(node_code, identity, execution.id)
        evaluation_id = _evaluation_id(task, execution.id, rule_instance_id)
        data = agent.rule_data(db, task, {**rule, "inputs": []} if rule.get("dependency_error") else rule)
        inputs = data["inputs"]
        return {
            "agent_mode": True,
            "evaluation_id": evaluation_id,
            "rule_cursor": rule_cursor,
            "rule_instance": {
                "rule_key": rule.get("rule_key"),
                "rule_code": rule_code,
                "rule_name": rule.get("name"),
                "node_code": node_code,
                "rule_instance_id": rule_instance_id,
                "evaluation_id": evaluation_id,
                "input_revision": int(task.input_revision or 0),
                "inputs": inputs,
                # 材料范围 = 这条规则声明的材料位。模型请求超出它的输入会被拒绝。
                "material_refs": list(rule.get("materials") or []),
                "materials": data["materials"],
                "facts": data["facts"],
            },
            "allowed_tools": allowed,
            "messages": None,
            "pending_tool_calls": [],
            "tool_results": [],
            "candidate_verdict": None,
            "unresolved_items": [],
            "loop_status": LOOP_RUNNING,
            "loop_count": 0,
            "last_model_response": None,
            "verdict_errors": [],
            "last_error": None,
        }


def graph_prepare_agent_context(
    db: Session,
    task_no: str,
    node_code: str,
    planned_rule: dict[str, Any],
    rule_instance: dict[str, Any] | None,
    messages: list[dict[str, Any]] | None,
) -> dict[str, Any]:
    """准备模型上下文：系统提示 + 这条规则的要求与可用字段事实。

    消息历史只在首次进入这条规则实例时生成，已有历史就原样保留——重新生成等于把模型已经看到
    的内容换掉，恢复后的判断会与中断前不一致。

    提示里只用本规则实际使用的事实与标识：给全量事实会让模型在无关字段上做判断，也会让它有机会
    引用不属于这条规则的输入。
    """
    with _agent_step(db, task_no):
        task = task_or_404(db, task_no, lock=True)
        manifest = _task_manifest(task)
        instance = rule_instance or {}
        rule = _rule_of_instance(manifest, node_code, instance)
        agent = _agent_module()
        inputs = list(instance.get("inputs") or [])
        # 输入缺项交给模型结合实际条件判断，不能在调用前拒绝整条规则。
        if messages:
            return {"messages": list(messages), "last_error": None}
        context_rule = dict(rule)
        if planned_rule.get("unavailable_dependencies"):
            context_rule["unavailable_dependencies"] = planned_rule["unavailable_dependencies"]
            context_rule["dependency_details"] = planned_rule.get("dependency_details") or []
        from .rule_execution import evaluation_context
        execution_context = evaluation_context(db, task, rule)
        context_rule.update(execution_context)
        context_rule["available_materials"] = instance.get("materials") or []
        # 模型看到的上游结果与消息一同保存在本条内存状态；最终引用校验使用同一份，
        # 不在保存时重查后来的结果来替换本次依据。
        from ...domain.fact_projection import fact_identity
        visible_facts = {fact_identity(fact): fact
                         for fact in [*execution_context["upstream_facts"], *list(instance.get("facts") or [])]}
        instance = {**instance, "facts": list(visible_facts.values()),
                    "upstream_results": execution_context["upstream_results"]}
        return {
            "rule_instance": instance,
            "messages": agent.initial_messages(
                context_rule,
                inputs,
                material_names=_material_names(manifest),
                manifest=manifest,
            ),
            "last_error": None,
        }


def _loop_limit_reached(loop_count: int) -> int | None:
    """本条规则是否已经到达模型轮次上限；到达时返回上限值。

    轮次的定义全链路只有一种：一次模型调用算一轮（与 `loop_count` 的计数口径一致）。
    批次不是轮次：一次模型调用可以请求多个工具，它们仍属于同一轮。

    `settings.llm_tool_max_rounds` 为 0 表示不设上限（由调用方自行控制终止），
    这是配置事实，不在这里改成某个默认值。
    """
    from ...config import get_settings

    max_rounds = get_settings().llm_tool_max_rounds
    if max_rounds and loop_count >= max_rounds:
        return max_rounds
    return None


def _loop_limit_state(max_rounds: int, loop_count: int, executed_tool_calls: list[str]) -> dict[str, Any]:
    """到达上限时的显式阻断状态：原因、已用轮次与已经执行过的工具调用。"""
    return {
        "loop_status": LOOP_BLOCKED,
        "unresolved_items": [
            {
                "kind": "LLM_TOOL_LOOP_LIMIT",
                "message": (
                    f"模型与工具循环达到已配置上限（{max_rounds} 轮）仍未形成可保存的结论，本次未完成；"
                    "系统不会把「跑到上限」当作结论，也不会继续执行下一批工具"
                ),
                "max_rounds": max_rounds,
                "loop_count": loop_count,
                "executed_tool_calls": list(executed_tool_calls),
            }
        ],
    }


def graph_call_model(
    db: Session,
    task_no: str,
    node_code: str,
    planned_rule: dict[str, Any],
    messages: list[dict[str, Any]] | None,
    allowed_tools: list[dict[str, Any]] | None,
    loop_count: int,
    state_evaluation_id: str | None,
    executed_tool_results: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """调用模型一次，返回本条规则这一轮的状态变化。

    这里不执行工具：模型请求了工具就由 `graph_execute_tool_calls` 执行、由
    `graph_append_tool_results` 生成 tool 消息，再由单条执行器决定继续调用或保存结果。
    本条循环不创建图回边，不占用任务图的步数。

    轮次上限（`settings.llm_tool_max_rounds`，按模型调用次数计）在这里兜底：已用完额度时不再
    调用模型，直接进 `LOOP_BLOCKED`；这一轮是最后一轮而模型仍请求工具时，不执行这一批工具，
    同样进 `LOOP_BLOCKED`。

    两种情况都写入 `LLM_TOOL_LOOP_LIMIT` 与未解决事项，并保留本条内存中的工具结果
    （`executed_tool_results`）与已得到的模型响应，交后续按阻断处理——既不默认通过，
    也不静默结束。
    """
    with _agent_step(db, task_no):
        from ...integrations.llm.gateway import TURN_CONTENT, TURN_REFUSAL, TURN_TOOL_CALLS

        task = task_or_404(db, task_no, lock=True)
        agent = _agent_module()
        executed = [
            str(item.get("tool_call_id"))
            for item in (executed_tool_results or [])
            if isinstance(item, dict) and item.get("tool_call_id")
        ]
        exhausted = _loop_limit_reached(loop_count)
        if exhausted is not None:
            # 额度已用完：不再调用模型，也不再执行任何工具。
            return {
                "loop_count": loop_count,
                "candidate_verdict": None,
                "pending_tool_calls": [],
                "messages": list(messages or []),
                **_loop_limit_state(exhausted, loop_count, executed),
                "last_error": None,
            }
        rule = planned_rule
        if rule.get("dependency_error"):
            return {"loop_status": LOOP_BLOCKED, "candidate_verdict": None, "pending_tool_calls": [], "last_error": None,
                    "unresolved_items": [{"kind": "RULE_DEPENDENCY_UNAVAILABLE", "message": "无法执行：显式依赖未生效、引用不存在或存在循环", "references": rule["dependency_error"]}]}
        tools = _openai_tools(allowed_tools)
        next_loop_count = loop_count
        try:
            # 前置处理已经完成。无输入事实的抽取项首轮必须读取真实正文或印章产物。
            tool_choice = "auto"
            if rule.get("output_definitions") and not rule.get("inputs") and loop_count == 0:
                tools = [tool for tool in tools if tool["function"]["name"] == "TASK_DATA_READ"]
                if not tools:
                    raise BusinessError("EXTRACTION_MATERIAL_TOOLS_MISSING", "当前工具目录没有材料读取工具，无法取得真实输入")
                tool_choice = "required"
            next_loop_count += 1
            turn = agent.call_model_once(list(messages or []), tools=tools, tool_choice=tool_choice)
        except BusinessError as exc:
            logger.warning("规则模型请求失败，继续其他项 task_no=%s rule_key=%s code=%s message=%s", task_no, rule["rule_key"], exc.code, exc.message)
            return {"loop_status": LOOP_BLOCKED, "loop_count": next_loop_count,
                    "candidate_verdict": None, "pending_tool_calls": [], "last_error": None,
                    "unresolved_items": [{"kind": exc.code, "message": exc.message, "details": exc.details}]}
        summary = {
            "kind": turn.kind,
            "finish_reason": turn.finish_reason,
            "model": turn.model,
            "response_id": turn.response_id,
            "requested_at": turn.requested_at,
            "responded_at": turn.responded_at,
            "tool_call_count": len(turn.tool_calls),
            "content": turn.content,
            "refusal": turn.refusal,
            "raw_message": turn.raw_message,
        }
        base = {
            "loop_count": next_loop_count,
            "last_model_response": summary,
            "candidate_verdict": None,
            "pending_tool_calls": [],
            "last_error": None,
        }
        if turn.kind == TURN_TOOL_CALLS:
            exhausted = _loop_limit_reached(next_loop_count)
            if exhausted is not None:
                # 这一轮是最后一轮：不把这一批工具放进待执行集合（它不会被执行），
                # assistant 消息也不并入历史——留着一条没有结果的 tool_calls
                # 会让恢复后的消息历史是一段不完整的协议。
                return {
                    **base,
                    "messages": list(messages or []),
                    **_loop_limit_state(exhausted, next_loop_count, executed),
                }
            return {
                **base,
                "messages": [*list(messages or []), agent.assistant_message_of(turn)],
                "pending_tool_calls": agent.pending_calls_of(turn, state_evaluation_id or ""),
                "loop_status": LOOP_RUNNING,
            }
        if turn.kind == TURN_CONTENT:
            return {**base, "messages": list(messages or []), "loop_status": LOOP_RUNNING}
        if turn.kind == TURN_REFUSAL:
            return {
                **base,
                "messages": list(messages or []),
                "loop_status": LOOP_BLOCKED,
                "unresolved_items": [
                    {
                        "kind": "MODEL_REFUSAL",
                        "message": turn.refusal or "模型拒绝了本次判断",
                    }
                ],
            }
        return {
            **base,
            "messages": list(messages or []),
            "loop_status": LOOP_BLOCKED,
            "unresolved_items": [
                {
                    "kind": "MODEL_RESPONSE_UNUSABLE",
                    "message": f"模型响应不可用（finish_reason={turn.finish_reason}）",
                }
            ],
        }


def graph_execute_tool_calls(
    db: Session,
    task_no: str,
    node_code: str,
    pending_tool_calls: list[dict[str, Any]] | None,
    allowed_tools: list[dict[str, Any]] | None,
    rule_instance: dict[str, Any] | None,
    existing_unresolved: list[dict[str, Any]] | None,
) -> dict[str, Any]:
    """执行模型明确请求的工具并返回真实结果与错误。"""
    with _agent_step(db, task_no):
        task = task_or_404(db, task_no, lock=True)
        calls = [item for item in (pending_tool_calls or []) if isinstance(item, dict)]
        if not calls:
            return {"tool_results": [], "pending_tool_calls": [], "last_error": None}
        agent = _agent_module()
        scenario = _scenario(db, task)
        manifest = _task_manifest(task)
        instance = rule_instance or {}
        rule = _rule_of_instance(manifest, node_code, instance)
        inputs = list(instance.get("inputs") or [])
        authorized = {
            str(item["tool_id"])
            for item in (allowed_tools or [])
            if isinstance(item, dict) and item.get("tool_id")
        }
        context = agent.execution_context(
            db,
            task=task,
            scene_code=scenario.scenario_code,
            node_code=node_code,
            rule_instance_id=str(instance.get("rule_instance_id") or ""),
            material_refs=tuple(instance.get("material_refs") or ()),
        )
        runtime, result_context = None, {}
        results: list[dict[str, Any]] = []
        unresolved: list[dict[str, Any]] = []
        for call in calls:
            tool_name = str(call.get("tool_name") or "")
            if tool_name not in authorized:
                # 模型请求了本次未授权的工具：明确拒绝并告知，不静默忽略。
                result = {
                    "status": "NOT_EXECUTED",
                    "message": f"工具“{tool_name}”不在本次授权范围内，未执行",
                }
                unresolved.append({"kind": "TOOL_NOT_AUTHORIZED", "tool_name": tool_name, "message": result["message"]})
                results.append(agent.tool_result_entry(call, result))
                continue
            from .rule_execution import tool_runtime
            runtime, result_context = tool_runtime(db, task, rule, tool_name)
            result = agent.invoke_tool(
                db, call=call, inputs=inputs, context=context, runtime=runtime,
                result_context=result_context, rule=rule, manifest=manifest,
            )
            if result.get("status") in {"NEEDS_RECONCILE", "FAILED"}:
                unresolved.append(
                    {
                        "kind": "TOOL_UNDECIDED",
                        "tool_name": tool_name,
                        "tool_call_id": call.get("tool_call_id"),
                        "message": result.get("message"),
                    }
                )
            results.append(agent.tool_result_entry(call, result))
        return {
            "tool_results": results,
            "pending_tool_calls": [],
            # 累积未解决事项：前几轮的失败与待核查不会被本轮覆盖掉。
            "unresolved_items": [*existing_unresolved, *unresolved],
            "last_error": None,
        }


def graph_append_tool_results(
    messages: list[dict[str, Any]] | None,
    tool_results: list[dict[str, Any]] | None,
) -> dict[str, Any]:
    """把本批工具结果生成 `tool` 角色消息并追加到消息历史。

    单独成一个节点而不是塞进 `execute_tool_calls`：消息历史与工具执行是两件事，合在一起会让
    「工具执行成功但消息没写进 State」这种半成品状态无法分辨。

    同一个供应商调用标识在消息历史里只能有一条 `tool` 消息：`tool_results` 会跨轮累积（供最终
    证据校验使用），若把整份累积结果逐项追加，第 N 轮会把前 N-1 轮的消息再追加一遍——同一
    `tool_call_id` 出现多条，上下文平方级膨胀，还可能被供应商判为协议错误。因此这里只追加历史
    中尚未出现的标识，避免跨轮重复回填同一次调用。
    """
    agent = _agent_module()
    appended = list(messages or [])
    appended_ids = {
        str(item.get("tool_call_id"))
        for item in appended
        if isinstance(item, dict) and item.get("role") == "tool" and item.get("tool_call_id")
    }
    for item in tool_results or []:
        if not isinstance(item, dict) or not item.get("tool_call_id"):
            continue
        # 回填消息用供应商的调用标识：模型认的是它自己给出的那个；
        provider_call_id = item.get("provider_tool_call_id") or item["tool_call_id"]
        if str(provider_call_id) in appended_ids:
            continue
        appended_ids.add(str(provider_call_id))
        result = item.get("result") if isinstance(item.get("result"), dict) else item
        appended.append(
            agent.tool_message(
                {
                    "tool_call_id": item["tool_call_id"],
                    "provider_tool_call_id": item.get("provider_tool_call_id"),
                    "tool_name": item.get("tool_name"),
                },
                result,
            )
        )
    return {"messages": appended, "last_error": None}


def graph_produce_candidate_verdict(
    db: Session,
    task_no: str,
    node_code: str,
    rule_instance: dict[str, Any] | None,
    last_model_response: dict[str, Any] | None,
    tool_results: list[dict[str, Any]] | None,
    allowed_tools: list[dict[str, Any]] | None,
    loop_status: str,
    unresolved_items: list[dict[str, Any]] | None,
    messages: list[dict[str, Any]] | None,
    verdict_errors: list[dict[str, Any]] | None,
) -> dict[str, Any]:
    """校验模型给出的候选结论，形成可采信的结论或明确的未解决事项。

    服务端只核对 `services.audit.agent.validate_verdict` 所定义的结果结构与真实引用。
    模型没有给出可保存的结论时保留原响应与接口错误，交回同一规则继续修正。
    """
    with _agent_step(db, task_no):
        task = task_or_404(db, task_no, lock=True)
        agent = _agent_module()
        manifest = _task_manifest(task)
        instance = rule_instance or {}
        rule = _rule_of_instance(manifest, node_code, instance)
        if loop_status in {LOOP_BLOCKED, LOOP_NEEDS_HUMAN}:
            # 模型响应不可用或拒绝判断：没有候选结论，如实保留原因交后续处理。
            return {
                "candidate_verdict": None,
                "loop_status": loop_status,
                "unresolved_items": list(unresolved_items or []),
                "last_error": None,
            }
        response = last_model_response if isinstance(last_model_response, dict) else {}
        raw = agent.parse_verdict(response.get("content"))
        facts = list(instance.get("facts") or [])
        upstream_results = instance.get("upstream_results") or []
        try:
            if raw is None:
                raise BusinessError("VERDICT_UNPARSEABLE", "模型没有返回可解析的最终结论 JSON 对象")
            from .rule_execution import validate_outputs
            verdict, status, extra = agent.validate_verdict(
                raw,
                rule=rule,
                tool_results=list(tool_results or []),
                facts=facts,
                upstream_results=upstream_results,
                tools=_openai_tools(allowed_tools),
            )
            validate_outputs(rule, raw)
        except BusinessError as exc:
            # 原返回与接口错误交回同一条规则的模型上下文，不猜测状态或丢弃产出。
            # 下一次调用仍走既有模型/工具循环及其配置，不另设重试上限。
            logger.warning("规则结论格式未满足接口，交回模型修正 task_no=%s rule_key=%s code=%s details=%s",
                           task_no, rule["rule_key"], exc.code, exc.details)
            return {
                "candidate_verdict": None,
                "loop_status": LOOP_RUNNING,
                "messages": [
                    *list(messages or []),
                    {"role": "assistant", "content": response["content"]},
                    {"role": "user", "content": "上一轮返回未能保存，接口错误：" + json.dumps(
                        {"code": exc.code, "message": exc.message, "details": exc.details}, ensure_ascii=False
                    ) + "。请结合已有真实材料、工具结果和本规则声明的输出继续执行并修正 JSON。"
                    "需要工具时使用原生 tool_calls；最终返回 status、message、basis、missing_items、next_action、evidence、outputs，不返回进度文字。"
                    "不能完成时如实使用 EVIDENCE_INSUFFICIENT 并说明原因，不编造产出。"},
                ],
                "unresolved_items": list(unresolved_items or []),
                "verdict_errors": [*(verdict_errors or []), {
                    "code": exc.code, "message": exc.message, "details": exc.details,
                    "model_response": response,
                }],
                "last_error": None,
            }
        if is_risk_rule(rule):
            verdict["risk_context"] = {
                "source_rules": [
                    {key: item.get(key) for key in ("rule_result_id", "rule_key", "rule_code", "rule_name", "status", "message")}
                    for item in upstream_results
                ],
                "risk_condition": rule.get("risk_condition"),
            }
        # 调用过程中出现的错误保留为历史诊断；最终业务缺口来自已校验的候选结论。
        # 不把恢复前的失败继续冒充当前业务缺项，也不删除调用历史。
        verdict["execution_issues"] = list(unresolved_items or [])
        return {
            "candidate_verdict": verdict,
            "loop_status": status,
            "unresolved_items": extra,
            "last_error": None,
        }


def graph_save_evaluation_result(
    db: Session,
    task_no: str,
    node_code: str,
    execution_id: int | None,
    rule_instance: dict[str, Any] | None,
    candidate_verdict: dict[str, Any] | None,
    loop_status: str,
    unresolved_items: list[dict[str, Any]] | None,
    evaluation_id: str | None,
    loop_count: int,
    last_model_response: dict[str, Any] | None = None,
    verdict_errors: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """保存这一条规则实例的评估结果。

    保存是确定性生命周期的一部分：Agent 不能跳过或改写。按
    (节点执行记录, 规则编号) 唯一写一条结果——人工复核已经改写过它时是更新而非新增，
    因此「人工结论」不会被随后的保存覆盖成模型候选结论。

    BLOCKED 时不写「通过」类结果：没有结论就如实记录未解决原因。
    """
    with _agent_step(db, task_no):
        task = task_or_404(db, task_no, lock=True)
        manifest = _task_manifest(task)
        execution = _execution_in_progress(db, task, execution_id)
        instance = rule_instance or {}
        rules = [item for item in manifest["rules"] if item.get("rule_key") == instance.get("rule_key")]
        if not rules:
            raise BusinessError(
                "AGENT_RULE_MISSING",
                "待保存的规则不在任务的已发布配置中，本次未保存结果",
                status_code=500,
                details={"rule_key": instance.get("rule_key")},
            )
        outcome = {
            "rule_key": instance.get("rule_key"),
            "rule_code": instance.get("rule_code"),
            "rule_name": instance.get("rule_name"),
            "loop_status": loop_status,
            "loop_count": loop_count,
            "unresolved_items": list(unresolved_items or []),
        }
        saved = db.scalar(select(RuleResult).where(
            RuleResult.node_execution_id == execution.id,
            RuleResult.rule_key == rules[0]["rule_key"],
        ))
        saved_value = saved
        reviewed = saved_value is not None and bool((saved_value.result_json or {}).get("manual_review_no"))
        if reviewed:
            # 人工裁决已写入真实结果，继续执行不能再次保存旧模型候选及其产出。
            outcome.update(status=saved_value.status, loop_status=LOOP_DONE, unresolved_items=[])
        else:
            try:
                with db.begin_nested():
                    from .rule_execution import save_outputs
                    save_outputs(db, task, execution, rules[0], candidate_verdict)
            except BusinessError as exc:
                logger.warning("规则产出未保存 task_no=%s rule_key=%s code=%s message=%s", task_no, rules[0]["rule_key"], exc.code, exc.message)
                candidate_verdict = None
                loop_status = LOOP_BLOCKED
                unresolved_items = [*(unresolved_items or []), {"kind": exc.code, "message": exc.message, "details": exc.details}]
                outcome.update(loop_status=loop_status, unresolved_items=unresolved_items)
            with db.begin_nested():
                saved_result = save_rule_result(db, task=task, execution=execution, rule=rules[0], verdict=candidate_verdict,
                                         loop_status=loop_status, unresolved_items=unresolved_items,
                                         evaluation_id=evaluation_id, inputs=list(instance.get("inputs") or []))
            saved_result.result_json = {**saved_result.result_json,
                                        "loop_count": loop_count,
                                        "model_response": last_model_response,
                                        "verdict_errors": list(verdict_errors or [])}
            outcome["status"] = saved_result.status
        db.commit()
        return {
            "rule_outcome": outcome,
            "unresolved_items": [],
            "pending_tool_calls": [],
            "tool_results": [],
            "last_error": None,
        }


def _rule_of_instance(manifest: dict[str, Any], node_code: str, instance: dict[str, Any]) -> dict[str, Any]:
    """按规则实例取出本节点的规则定义（已发布配置，冻结不变）。"""
    rule_key = instance.get("rule_key")
    for rule in manifest["rules"]:
        if rule.get("node") == node_code and rule.get("rule_key") == rule_key:
            return rule
    raise BusinessError(
        "AGENT_RULE_MISSING",
        "规则评估关联的规则不在任务的已发布配置中，本次未继续",
        status_code=500,
        details={"node_code": node_code, "rule_key": rule_key},
    )
