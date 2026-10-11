"""生效规则的模型/工具循环、真实结果保存与按需人工确认。

RULE_EXECUTION 只关联执行记录，不划分业务阶段；单条无法执行会保存原因并继续。
"""

import hashlib
import logging
from contextlib import contextmanager
from ...domain import tool_catalog
from ...domain.identity import public_no
from ...domain.manifest import comparison_label
from ...common.exceptions import BusinessError, exception_summary
from ...entity import AuditTask, Evidence, ManualReview, NodeExecution, RuleResult
from ...schema.dto import ManualReviewRequest
from ..task_lookup import task_or_404
from .evidence import _copy_evidence_to_rule, _rule_evidence_snapshots
from .reviews import (
    _apply_review_decisions,
    _current_review_batch,
    _decision_command_no,
    _invalidate_execution_records,
    _invalidate_retry_results,
    _invalidate_validation_results,
    _next_node_for_recalculation,
    _review_hash,
    _validate_review_payloads,
)
from .shared import _material_names, _scenario, _task_manifest
from .state import _flow_checkpoint_state, _save_flow_checkpoint, _scenario_nodes, _set_task_checkpoint, _validate_graph_namespace
from datetime import UTC, datetime
from langchain_core.utils.function_calling import convert_to_openai_tool
from pydantic import ValidationError
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session
from typing import Any, Iterator


logger = logging.getLogger(__name__)

MODE_AGENT = "AGENT"

# Agent 循环状态。与 `services.audit.agent` 的常量一致。
LOOP_RUNNING = "RUNNING"
LOOP_NEEDS_HUMAN = "NEEDS_HUMAN"
LOOP_DONE = "DONE"
LOOP_BLOCKED = "BLOCKED"

# 被服务重启打断、由 `commands.recover_interrupted_executions` 写成 FAILED 的执行记录。
# 这类终态是「进程被杀」而不是「节点真的失败」，Agent 路径在人工复核之后仍会收尾，
# 因此允许被真实结果覆盖，并在结果里保留原失败信息。
_INTERRUPTED_FAILURE_CODE = "AUDIT_EXECUTION_INTERRUPTED"


def graph_load_task(db: Session, task_no: str, graph_namespace: str) -> dict[str, Any]:
    """加载并校验可恢复的任务状态：检查点必须与任务当前节点、阶段一致。"""
    task = task_or_404(db, task_no)
    checkpoint = _validate_graph_namespace(task, graph_namespace)
    if task.status in {"COMPLETED", "FAILED"}:
        raise BusinessError("AUDIT_TASK_FINISHED", "审核任务已结束，不能继续执行", status_code=409)
    _, _, current_index = _scenario_nodes(db, task)
    if checkpoint.get("current_node") != task.current_node or checkpoint.get("node_index") != current_index:
        raise BusinessError("AUDIT_CHECKPOINT_INVALID", "审核流程检查点与当前任务节点不一致", status_code=409)
    phase = checkpoint.get("phase")
    if phase not in {"AWAITING_CONFIRMATION", "WAITING_REVIEW"}:
        raise BusinessError("AUDIT_CHECKPOINT_INVALID", "审核流程当前不处于可恢复等待状态", status_code=409)
    if phase == "WAITING_REVIEW":
        if task.status != "WAITING_REVIEW":
            raise BusinessError("REVIEW_CHECKPOINT_MISSING", "人工复核流程检查点与任务状态不一致", status_code=409)
        _flow_checkpoint_state(task)
    return {
        "current_node": task.current_node,
        "node_index": current_index,
        "phase": phase,
        "review_hash": checkpoint.get("review_hash"),
        "review_count": checkpoint.get("review_count"),
        "last_execution": None,
        "manual_review_result": None,
        "last_error": None,
    }

def _graph_error_state(db: Session, task_no: str, graph_namespace: str, node_code: str, exc: BusinessError) -> dict[str, Any]:
    task = task_or_404(db, task_no)
    checkpoint = _validate_graph_namespace(task, graph_namespace)
    _, _, current_index = _scenario_nodes(db, task)
    return {
        "executed_node": node_code,
        "current_node": task.current_node,
        "node_index": current_index,
        "phase": checkpoint["phase"],
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
        if rule.get("enabled", True) and rule.get("node") == node_code and rule.get("rule_key") in enabled_keys
    ]


def graph_execute_current_node(
    db: Session,
    task_no: str,
    graph_namespace: str,
    current_node: str,
    confirmed_node: str | None,
) -> dict[str, Any]:
    """建立本次规则执行记录；实际规则统一由模型与工具循环完成。"""
    task = task_or_404(db, task_no, lock=True)
    checkpoint = _validate_graph_namespace(task, graph_namespace)
    if checkpoint.get("phase") != "AWAITING_CONFIRMATION":
        raise BusinessError("AUDIT_CONFIRMATION_NOT_WAITING", "任务当前不处于节点确认等待状态", status_code=409)
    if task.status in {"COMPLETED", "FAILED"}:
        raise BusinessError("AUDIT_TASK_FINISHED", "审核任务已结束，不能继续执行", status_code=409)
    if confirmed_node != task.current_node:
        raise BusinessError("AUDIT_NODE_MISMATCH", "提交节点与任务当前节点不一致", status_code=409)
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
        return _graph_error_state(db, task_no, graph_namespace, current_node, exc)
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
    graph_namespace: str,
    execution_id: int | None,
    execution_mode: str,
    node_result: dict[str, Any] | None,
    agent_summary: dict[str, Any] | None,
) -> dict[str, Any]:
    """收尾规则执行批次，保存真实逐条结果的汇总。"""
    task = task_or_404(db, task_no, lock=True)
    _validate_graph_namespace(task, graph_namespace)
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
    execution.result_json = dict(agent_summary or {})
    # 批次已遍历；单条失败仍以真实规则状态和原因保存。
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
    graph_namespace: str,
    execution_id: int | None,
) -> dict[str, Any]:
    """规则执行结束后更新任务与检查点，实际人工待办存在时等待处理。"""
    task = task_or_404(db, task_no, lock=True)
    checkpoint = _validate_graph_namespace(task, graph_namespace)
    manifest = _task_manifest(task)
    nodes = sorted(manifest["nodes"], key=lambda item: item["sequence_no"])
    _, _, current_index = _scenario_nodes(db, task)
    execution = _execution_in_progress(db, task, execution_id)
    if execution.status == "BLOCKED":
        # 必传材料缺失：任务显式阻断在当前节点，检查点不动，用户补传后可重新执行。
        return {
            "current_node": task.current_node,
            "node_index": current_index,
            "phase": checkpoint["phase"],
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
        _save_flow_checkpoint(db, task)
        db.commit()
        return {
            "current_node": task.current_node,
            "node_index": current_index,
            "phase": checkpoint["phase"],
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
    _save_flow_checkpoint(db, task)
    db.commit()
    return {
        "current_node": task.current_node,
        "node_index": _index_of(nodes, task.current_node) if next_node else current_index,
        "phase": "AWAITING_CONFIRMATION" if next_node else "COMPLETED",
        "review_hash": None,
        "review_count": None,
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
    pending = list(db.scalars(select(ManualReview.review_no).where(ManualReview.audit_task_id == task.id, ManualReview.status == "PENDING")))
    if pending:
        raise BusinessError("RULE_REVIEW_UNRESOLVED", "执行结束时仍有未提交的人工确认项", details={"review_nos": pending})
    return None


def graph_save_node_result(db: Session, task_no: str, graph_namespace: str) -> dict[str, Any]:
    """落库并提交本次节点结果与流程检查点。"""
    task = task_or_404(db, task_no, lock=True)
    checkpoint = _validate_graph_namespace(task, graph_namespace)
    _save_flow_checkpoint(db, task)
    db.commit()
    _, _, current_index = _scenario_nodes(db, task)
    return {
        "current_node": task.current_node,
        "node_index": current_index,
        "phase": checkpoint["phase"],
        "review_hash": checkpoint.get("review_hash"),
        "review_count": checkpoint.get("review_count"),
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


# 规则评估 Agent 的生命周期实现（图只调度，实现在这里）
#
# 这些入口每一步只做一件事，全部是确定性的：取数、冻结快照、调一次模型、
# 执行一批工具、校验候选结论、落库结果、推进游标。没有函数内部循环——
# 循环由图的节点与条件边表达，因此每一步都可 checkpoint、可 interrupt、可恢复。
#
# 一次评估只处理一条规则实例：State 里保存当前规则实例与游标，
# 消息、工具调用与结果都属于这条规则，不与其他规则的混在一起。

def _agent_module() -> Any:
    """延迟导入 Agent 实现。

    `flow` 与 `agent` 同属 audit 包，但 agent 依赖 integrations.llm 与
    services.tool_service；模块级导入会让「只加载 flow」也拉起模型网关。
    延迟导入保持 import 图干净（同手法见 operation_gateway）。
    """
    from . import agent

    return agent


def _evaluation_scope(
    manifest: dict[str, Any],
    node_code: str,
) -> dict[str, Any]:
    """本节点的评估范围：节点级授权工具与工具策略版本。

    两者都在评估开始时冻结，本次评估的全部工具调用共用同一份身份——
    这是「工具结果绑定到当时的事实快照与工具策略版本」的落点。
    """
    agent = _agent_module()
    return {
        "node_tools": agent.allowed_tools_of(manifest, node_code=node_code),
        "policy": agent.policy_of(manifest),
    }


def _plan_entry(node_code: str, cursor: int, rule: dict[str, Any]) -> dict[str, Any]:
    """统一执行计划中的一条生效规则实例。"""
    return {
        "cursor": cursor,
        "rule_key": rule.get("rule_key"),
        "rule_code": rule.get("rule_code") or rule.get("rule_key"),
        "node_code": node_code,
    }


def _plan_rule(
    task: AuditTask,
    manifest: dict[str, Any],
    node_code: str,
    rule_cursor: int,
) -> dict[str, Any]:
    """按相同显式依赖顺序取得当前生效规则，与加载的执行计划一致。"""
    from ...domain.rule_library import execution_order
    agent_rules = execution_order(_enabled_rules(task, manifest, node_code))
    if rule_cursor < 0 or rule_cursor >= len(agent_rules):
        raise BusinessError(
            "AGENT_RULE_CURSOR_INVALID",
            "规则评估游标越界，本次评估未继续",
            status_code=500,
            details={"node_code": node_code, "rule_cursor": rule_cursor, "rule_count": len(agent_rules)},
        )
    return agent_rules[rule_cursor]


def _evaluation_id(task: AuditTask, graph_namespace: str, execution_id: int, rule_instance_id: str) -> str:
    """一次规则实例评估的批次标识。由身份推导，不随机生成。

    推导而非随机是刻意的：恢复与重跑必须得到同一个标识，否则
    「同一次评估」会分裂成两次，账本里的工具调用也无法复用。
    参与推导的是不可变身份——任务、图命名空间、节点执行记录、规则实例、输入修订。
    """
    canonical = "|".join(
        [
            task.task_no,
            graph_namespace,
            str(execution_id),
            rule_instance_id,
            str(int(task.input_revision or 0)),
        ]
    )
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return f"EVAL-{digest[:24].upper()}"


def _rule_instance_id(node_code: str, rule_identity: str, execution_id: int) -> str:
    """规则实例标识：同一条规则在不同节点执行批次里是不同实例。"""
    return f"{node_code}:{rule_identity}:NE{execution_id}"


def _openai_tools(allowed_tools: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """按冻结的工具定义构造模型的 `tools` 参数。

    模型看到的必须是发布期冻结的那一份定义，不是当前工具目录：目录升级后
    旧任务仍按冻结定义执行，用当前目录重算会让历史结论无法复现，也会让
    「模型当时看到的是什么」失去答案。清单里缺少冻结定义时由
    `agent.frozen_tool_definitions` 显式报错，不静默改用当前目录。
    """
    definitions = _agent_module().frozen_tool_definitions(allowed_tools)
    return [
        convert_to_openai_tool(definition)
        for definition in sorted(definitions, key=lambda item: str(item.get("name")))
    ]


@contextmanager
def _agent_step(db: Session, task_no: str, graph_namespace: str) -> Iterator[None]:
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
        _fail_running_execution(db, task_no, graph_namespace, code=exc.code, message=exc.message)
        raise
    except Exception as exc:  # noqa: BLE001 - 未预期异常必须落库后再暴露
        logger.exception(
            "规则评估发生未处理异常 task_no=%s graph_namespace=%s", task_no, graph_namespace, exc_info=exc
        )
        summary = exception_summary(exc)
        _fail_running_execution(
            db,
            task_no,
            graph_namespace,
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
    graph_namespace: str,
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
    checkpoint = task.checkpoint_json if task is not None and isinstance(task.checkpoint_json, dict) else {}
    execution = _running_execution(db, task) if task is not None else None
    if (
        task is None
        or task.status != "RUNNING"
        or execution is None
        or execution.node_code != task.current_node
        or checkpoint.get("graph_checkpoint_ns") != graph_namespace
    ):
        return
    execution.status = "FAILED"
    execution.finished_at = datetime.now(UTC)
    execution.failure_code = code
    execution.failure_message = message
    task.status = "BLOCKED"
    _save_flow_checkpoint(db, task)
    db.commit()


def _graph_namespace_or_fail(task: AuditTask, graph_namespace: str) -> dict[str, Any]:
    return _validate_graph_namespace(task, graph_namespace)


def graph_load_evaluation(
    db: Session,
    task_no: str,
    graph_namespace: str,
    node_code: str,
    execution_id: int | None,
) -> dict[str, Any]:
    """加载全部生效规则的执行计划及冻结工具定义；没有规则时直接收尾。"""
    with _agent_step(db, task_no, graph_namespace):
        task = task_or_404(db, task_no, lock=True)
        _graph_namespace_or_fail(task, graph_namespace)
        manifest = _task_manifest(task)
        execution = _execution_in_progress(db, task, execution_id)
        from ...domain.rule_library import execution_order
        agent_rules = execution_order(_enabled_rules(task, manifest, node_code))
        scope = _evaluation_scope(manifest, node_code)
        return {
            "agent_mode": True,
            "evaluation_plan": [_plan_entry(node_code, index, rule) for index, rule in enumerate(agent_rules)],
            "rule_cursor": 0,
            "execution_id": execution.id,
            "tool_catalog_version": scope["policy"]["tool_catalog_version"],
            "tool_catalog_hash": scope["policy"]["tool_catalog_hash"],
            "tool_policy_version": scope["policy"]["tool_policy_version"],
            "allowed_tools": scope["node_tools"],
            "messages": None,
            "pending_tool_calls": [],
            "tool_results": [],
            "rule_outcomes": [],
            "candidate_verdict": None,
            "unresolved_items": [],
            "loop_status": LOOP_RUNNING,
            "loop_count": 0,
            "last_model_response": None,
            "evaluation_id": None,
            "rule_instance": None,
            "fact_snapshot_ref": None,
            "fact_snapshot_hash": None,
            "last_error": None,
        }


def graph_load_rule_instance(
    db: Session,
    task_no: str,
    graph_namespace: str,
    node_code: str,
    rule_cursor: int,
    execution_id: int | None,
) -> dict[str, Any]:
    """冻结一条规则实例：评估身份、事实快照、授权工具与输入事实。

    评估身份由身份推导（`_evaluation_id`），快照按
    (评估批次, 规则实例, 输入修订) 幂等冻结：重复执行不会产生第二份输入，
    恢复也不会重新生成评估身份或工具调用标识。
    """
    with _agent_step(db, task_no, graph_namespace):
        task = task_or_404(db, task_no, lock=True)
        _graph_namespace_or_fail(task, graph_namespace)
        manifest = _task_manifest(task)
        execution = _execution_in_progress(db, task, execution_id)
        rule = _plan_rule(task, manifest, node_code, rule_cursor)
        rule_code = str(rule.get("rule_code") or rule.get("rule_key"))
        agent = _agent_module()
        allowed = agent.allowed_tools_of(
            manifest, node_code=node_code, rule_codes=(rule_code,), rule_keys=(rule["rule_key"],),
        )
        identity = rule["rule_key"]
        rule_instance_id = _rule_instance_id(node_code, identity, execution.id)
        evaluation_id = _evaluation_id(task, graph_namespace, execution.id, rule_instance_id)
        policy = agent.policy_of(manifest)
        with db.begin_nested():
            snapshot = agent.freeze_snapshot(
                db,
                task=task,
                rule={**rule, "inputs": []} if rule.get("dependency_error") else rule,
                node_code=node_code,
                rule_instance_id=rule_instance_id,
                evaluation_id=evaluation_id,
                allowed_tools=allowed,
                policy=policy,
            )
        db.commit()
        record = agent.load_snapshot(db, task=task, snapshot_ref=snapshot["ref"])
        inputs = [] if rule.get("dependency_error") else agent.inputs_of_snapshot(record, rule)
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
                # 本规则本次依据的输入事实（从冻结快照解析，见 domain/fact_snapshot）。
                "inputs": inputs,
                # 材料范围 = 这条规则声明的材料位。模型请求超出它的输入会被拒绝。
                "material_refs": list(rule.get("materials") or []),
            },
            "fact_snapshot_ref": snapshot["ref"],
            "fact_snapshot_hash": snapshot["hash"],
            "allowed_tools": allowed,
            "messages": None,
            "pending_tool_calls": [],
            "tool_results": [],
            "candidate_verdict": None,
            "unresolved_items": [],
            "loop_status": LOOP_RUNNING,
            "loop_count": 0,
            "last_model_response": None,
            "last_error": None,
        }


def graph_prepare_agent_context(
    db: Session,
    task_no: str,
    graph_namespace: str,
    node_code: str,
    rule_cursor: int,
    rule_instance: dict[str, Any] | None,
    messages: list[dict[str, Any]] | None,
) -> dict[str, Any]:
    """准备模型上下文：系统提示 + 这条规则的要求与可用字段事实。

    消息历史只在首次进入这条规则实例时生成，已有历史就原样保留——重新生成等于把模型已经看到
    的内容换掉，恢复后的判断会与中断前不一致。

    提示里只用本规则实际使用的事实与标识：给全量事实会让模型在无关字段上做判断，也会让它有机会
    引用不属于这条规则的输入。
    """
    with _agent_step(db, task_no, graph_namespace):
        task = task_or_404(db, task_no, lock=True)
        _graph_namespace_or_fail(task, graph_namespace)
        manifest = _task_manifest(task)
        instance = rule_instance or {}
        rule = _rule_of_instance(manifest, node_code, instance)
        agent = _agent_module()
        inputs = list(instance.get("inputs") or [])
        # 输入缺项交给模型结合实际条件判断，不能在调用前拒绝整条规则。
        if messages:
            return {"messages": list(messages), "last_error": None}
        context_rule = dict(rule)
        planned_rule = _plan_rule(task, manifest, node_code, rule_cursor)
        if planned_rule.get("unavailable_dependencies"):
            context_rule["unavailable_dependencies"] = planned_rule["unavailable_dependencies"]
        from .rule_execution import evaluation_context
        context_rule.update(evaluation_context(db, task, rule))
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
    """本节点这一轮是否已经到达工具循环上限；到达时返回上限值。

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
                    f"工具调用循环达到上限（{max_rounds} 轮）仍未形成结论，本次未完成；"
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
    graph_namespace: str,
    node_code: str,
    rule_cursor: int,
    messages: list[dict[str, Any]] | None,
    allowed_tools: list[dict[str, Any]] | None,
    loop_count: int,
    state_evaluation_id: str | None,
    executed_tool_results: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """调用模型一次，把这一轮的响应写回 State。

    这里不执行工具：模型请求了工具就由 `graph_execute_tool_calls` 执行、由
    `graph_append_tool_results` 生成 tool 消息，然后回到本节点问下一轮。每次调用模型都发生在
    图节点里，因此每一轮都是可 checkpoint 的状态。

    轮次上限（`settings.llm_tool_max_rounds`，按模型调用次数计）在这里兜底：已用完额度时不再
    调用模型，直接进 `LOOP_BLOCKED`；这一轮是最后一轮而模型仍请求工具时，不执行这一批工具，
    同样进 `LOOP_BLOCKED`。

    两种情况都写入 `LLM_TOOL_LOOP_LIMIT` 与未解决事项，并保留已执行的工具账本
    （`executed_tool_results`）与已得到的模型响应，交后续按阻断处理——既不默认通过，
    也不静默结束。
    """
    with _agent_step(db, task_no, graph_namespace):
        from ...integrations.llm.gateway import TURN_CONTENT, TURN_REFUSAL, TURN_TOOL_CALLS

        task = task_or_404(db, task_no, lock=True)
        _graph_namespace_or_fail(task, graph_namespace)
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
                "last_model_response": None,
                "candidate_verdict": None,
                "pending_tool_calls": [],
                "messages": list(messages or []),
                **_loop_limit_state(exhausted, loop_count, executed),
                "last_error": None,
            }
        rule = _plan_rule(task, _task_manifest(task), node_code, rule_cursor)
        if rule.get("dependency_error"):
            return {"loop_status": LOOP_BLOCKED, "candidate_verdict": None, "pending_tool_calls": [], "last_error": None,
                    "unresolved_items": [{"kind": "RULE_DEPENDENCY_UNAVAILABLE", "message": "无法执行：显式依赖未生效、引用不存在或存在循环", "references": rule["dependency_error"]}]}
        tools = _openai_tools(allowed_tools)
        try:
            turn = agent.call_model_once(list(messages or []), tools=tools)
        except BusinessError as exc:
            logger.warning("规则模型请求失败，继续其他项 task_no=%s rule_key=%s code=%s message=%s", task_no, rule["rule_key"], exc.code, exc.message)
            return {"loop_status": LOOP_BLOCKED, "candidate_verdict": None, "pending_tool_calls": [], "last_error": None,
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
        }
        next_loop_count = loop_count + 1
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
    graph_namespace: str,
    node_code: str,
    pending_tool_calls: list[dict[str, Any]] | None,
    allowed_tools: list[dict[str, Any]] | None,
    fact_snapshot_ref: str | None,
    rule_instance: dict[str, Any] | None,
    existing_unresolved: list[dict[str, Any]] | None,
) -> dict[str, Any]:
    """执行 State 中的这一批待执行工具调用。

    每次只执行 `pending_tool_calls` 里的调用，执行完即清空——「下一批」由模型
    下一轮请求产生。因此本节点不构成循环，重复进入只会执行新的一批。

    工具输入从冻结快照解析，运行期状态（风险来源结论）由这里注入；
    模型给的只是身份引用与业务参数。账本负责「同一次调用不重复执行」。
    """
    with _agent_step(db, task_no, graph_namespace):
        task = task_or_404(db, task_no, lock=True)
        _graph_namespace_or_fail(task, graph_namespace)
        calls = [item for item in (pending_tool_calls or []) if isinstance(item, dict)]
        if not calls:
            return {"tool_results": [], "pending_tool_calls": [], "last_error": None}
        agent = _agent_module()
        scenario = _scenario(db, task)
        manifest = _task_manifest(task)
        record = agent.load_snapshot(db, task=task, snapshot_ref=fact_snapshot_ref) if fact_snapshot_ref else None
        if record is None:
            raise BusinessError(
                "AGENT_SNAPSHOT_MISSING",
                "规则评估缺少冻结事实快照，本次工具调用未执行",
                status_code=500,
                details={"node_code": node_code},
            )
        instance = rule_instance or {}
        rule = _rule_of_instance(manifest, node_code, instance)
        inputs = agent.inputs_of_snapshot(record, rule)
        authorized = {
            str(item["tool_id"])
            for item in (allowed_tools or [])
            if isinstance(item, dict) and item.get("tool_id")
        }
        context = agent.execution_context(
            db,
            task=task,
            snapshot=record,
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
            result, _ = agent.invoke_tool(
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
    中尚未出现的标识，checkpoint 重放也不会重复生成同一条消息。
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
        # 账本标识写在结果内容里，模型引用证据时用它。
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
                },
                result,
            )
        )
    return {"messages": appended, "last_error": None}


def graph_produce_candidate_verdict(
    db: Session,
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
) -> dict[str, Any]:
    """校验模型给出的候选结论，形成可采信的结论或明确的未解决事项。

    服务端只核对 `services.audit.agent.validate_verdict` 所定义的结果结构与真实引用。
    模型没有给出可解析结论时按 BLOCKED 处理并说明原因，不猜测状态。
    """
    with _agent_step(db, task_no, graph_namespace):
        task = task_or_404(db, task_no, lock=True)
        _graph_namespace_or_fail(task, graph_namespace)
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
        if raw is None:
            return {
                "candidate_verdict": None,
                "loop_status": LOOP_BLOCKED,
                "unresolved_items": [
                    *list(unresolved_items or []),
                    {
                        "kind": "VERDICT_UNPARSEABLE",
                        "message": "模型给出了文本但没有可解析的结论对象，本次未形成候选结论",
                    },
                ],
                "last_error": None,
            }
        record = agent.load_snapshot(db, task=task, snapshot_ref=fact_snapshot_ref) if fact_snapshot_ref else None
        facts = agent.facts_of_snapshot(record) if record is not None else []
        try:
            from .rule_execution import validate_outputs
            validate_outputs(rule, raw)
            verdict, status, extra = agent.validate_verdict(
                raw,
                rule=rule,
                tool_results=list(tool_results or []),
                facts=facts,
                tools=_openai_tools(allowed_tools),
            )
        except BusinessError as exc:
            return {
                "candidate_verdict": None,
                "loop_status": LOOP_BLOCKED,
                "unresolved_items": [
                    *list(unresolved_items or []),
                    {"kind": exc.code, "message": exc.message, "details": exc.details},
                ],
                "last_error": None,
            }
        if rule.get("operator") == "RISK_TRIGGER" and instance.get("risk_context"):
            verdict["risk_context"] = instance["risk_context"]
        return {
            "candidate_verdict": verdict,
            "loop_status": status,
            "unresolved_items": [*list(unresolved_items or []), *extra],
            "last_error": None,
        }


def graph_prepare_agent_review(
    db: Session,
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
) -> dict[str, Any]:
    """把「需要人工复核」变成真实的待办与合法的等待状态。

    顺序不能颠倒——必须先把规则结果与待办落库、再切等待状态：写规则结果（候选结论，
    `origin=AGENT`），待办才有可关联的业务对象；建 `manual_review` 待办，保存模型候选结论、
    确定性工具结果与冲突原因；生成复核集合哈希与条数，把任务状态与检查点切成 `WAITING_REVIEW`；
    返回进入 interrupt 所需的集合身份。

    切换期间任务当前节点临时指向人工复核节点，复核提交因此走既有入口；
    `graph_apply_agent_reviews` 会把当前节点恢复成正在评估的节点。
    """
    with _agent_step(db, task_no, graph_namespace):
        task = task_or_404(db, task_no, lock=True)
        checkpoint = _graph_namespace_or_fail(task, graph_namespace)
        manifest = _task_manifest(task)
        execution = _execution_in_progress(db, task, execution_id)
        instance = rule_instance or {}
        rule = _rule_of_instance(manifest, node_code, instance)
        _, nodes, _ = _scenario_nodes(db, task)
        review_index = _index_of(nodes, node_code)
        with db.begin_nested():
            result = _upsert_agent_rule_result(
                db,
                task=task,
                execution=execution,
                rule=rule,
                verdict=candidate_verdict,
                loop_status=LOOP_NEEDS_HUMAN,
                unresolved_items=unresolved_items,
                tool_results=tool_results,
                evaluation_id=evaluation_id,
                inputs=list(instance.get("inputs") or []),
                fact_snapshot_ref=fact_snapshot_ref,
            )
            review_evidences = _rule_evidence_snapshots(db, result.id)
            db.add(
                ManualReview(
                    review_no=public_no("REV"),
                    audit_task_id=task.id,
                    rule_result_id=result.id,
                    review_type="RULE_RESULT",
                    status="PENDING",
                    system_judgment_json={
                        "origin": "AGENT",
                        "evaluation_id": evaluation_id,
                        "rule_key": rule.get("rule_key"),
                        "rule_name": rule.get("name"),
                        "status": result.status,
                        "message": result.message,
                        "evidence": review_evidences[0] if review_evidences else None,
                        "evidences": review_evidences,
                        "candidate_verdict": candidate_verdict,
                        "deterministic_tool_results": tool_results or [],
                        "unresolved_items": unresolved_items or [],
                        "conflicts": (candidate_verdict or {}).get("conflicts") or [],
                    },
                    business_note=str((candidate_verdict or {}).get("message") or "规则评估需要人工复核"),
                )
            )
            db.flush()
            reviews = _current_review_batch(db, task)
            if not reviews:
                raise BusinessError(
                    "REVIEW_CREATE_FAILED",
                    "人工复核待办创建后取不到待复核集合，本次未进入等待",
                    status_code=500,
                )
            digest = _review_hash(reviews)
            task.pending_review_hash = digest
            task.status = "WAITING_REVIEW"
            task.current_node = node_code
            _set_task_checkpoint(
                task,
                node_index=review_index,
                phase="WAITING_REVIEW",
                review_hash=digest,
                review_count=len(reviews),
            )
            _save_flow_checkpoint(db, task)
        db.commit()
        return {
            "phase": "WAITING_REVIEW",
            "current_node": task.current_node,
            "node_index": review_index,
            "review_hash": digest,
            "review_count": len(reviews),
            # 复核提交后据此恢复正在评估的节点与检查点。
            "agent_review_restore": {
                "node_code": node_code,
                "node_index": checkpoint.get("node_index"),
                "phase": checkpoint.get("phase"),
                "rule_cursor": rule_cursor,
            },
            "last_error": None,
        }


def graph_apply_agent_reviews(
    db: Session,
    task_no: str,
    graph_namespace: str,
    review_hash: str | None,
    review_count: int | None,
    decisions: list[dict[str, Any]] | None,
    agent_review_restore: dict[str, Any] | None,
) -> dict[str, Any]:
    """应用 Agent 复核裁决，并把任务恢复到正在评估的节点上。

    裁决落库与人工复核节点共用同一实现（`reviews._apply_review_decisions`），
    这里只负责两件它不做的事：

    1. 校验提交的集合与进入等待时的集合逐条一致（哈希 + 条数）；
    2. 恢复任务当前节点与检查点——Agent 复核发生在某个校验节点执行期间，
       不能像人工复核节点那样推进到下一个节点，否则该节点剩下的规则会被跳过。
    """
    with _agent_step(db, task_no, graph_namespace):
        if not isinstance(review_hash, str) or not isinstance(review_count, int) or not isinstance(decisions, list):
            raise BusinessError("REVIEW_CHECKPOINT_MISSING", "人工复核流程检查点缺少待复核集合", status_code=409)
        try:
            request = ManualReviewRequest.model_validate({"reviews": decisions})
        except ValidationError as exc:
            raise BusinessError("MANUAL_REVIEW_PAYLOAD_INVALID", "人工复核提交内容无效", status_code=409) from exc
        restore = agent_review_restore if isinstance(agent_review_restore, dict) else {}
        node_code = restore.get("node_code")
        if not isinstance(node_code, str) or not node_code:
            raise BusinessError(
                "REVIEW_CHECKPOINT_MISSING",
                "人工复核流程检查点缺少复核前节点，无法恢复执行位置",
                status_code=409,
            )
        task = task_or_404(db, task_no, lock=True)
        checkpoint = _graph_namespace_or_fail(task, graph_namespace)
        if task.status != "WAITING_REVIEW":
            raise BusinessError("MANUAL_REVIEW_NOT_WAITING", "任务当前不在人工复核等待状态", status_code=409)
        reviews = _current_review_batch(db, task)
        current_hash = _review_hash(reviews)
        if current_hash != task.pending_review_hash or current_hash != review_hash or len(reviews) != review_count:
            raise BusinessError("MANUAL_REVIEW_SET_CHANGED", "待复核集合已变化，请刷新后重新提交", status_code=409)
        if (
            checkpoint.get("phase") != "WAITING_REVIEW"
            or checkpoint.get("review_hash") != current_hash
            or checkpoint.get("review_count") != len(reviews)
        ):
            raise BusinessError("REVIEW_CHECKPOINT_MISSING", "人工复核流程检查点不存在或与当前复核集合不一致", status_code=409)
        requested = {item.review_no: item for item in request.reviews}
        if len(requested) != len(request.reviews) or set(requested) != {item.review_no for item in reviews}:
            raise BusinessError("MANUAL_REVIEW_INCOMPLETE", "必须处理当前全部待复核项")
        normalized_values = _validate_review_payloads(db, task, reviews, requested)
        command_no = _decision_command_no(db, task)
        _, material_state_changed, field_changes = _apply_review_decisions(
            db, task, reviews, requested, normalized_values, command_no
        )
        db.flush()
        manifest = _task_manifest(task)
        _, nodes, _ = _scenario_nodes(db, task)
        file_quality_recompute = None
        if field_changes:
            execution = _running_execution(db, task)
            changed_field_keys = {str(item["field_key"]) for item in field_changes}
            invalidation = _invalidate_validation_results(db, task, manifest, changed_field_keys)
            recalculation_required = invalidation["invalidated_executions"] > 0
            if recalculation_required and execution is not None:
                affected_node_codes = set(invalidation["affected_node_codes"])
                if execution.node_code not in affected_node_codes:
                    # 受影响的已完成分支需要回退时，当前 Agent 执行无法继续悬挂为 RUNNING；
                    # 它尚未形成有效节点结果，显式终止后由通用规则循环重新执行。
                    _invalidate_execution_records(db, task, [execution.id])
                execution.finished_at = datetime.now(UTC)
                execution.result_json = {
                    **(execution.result_json or {}),
                    "invalidated_reason": (
                        "人工复核改变字段事实，原规则评估使用的事实快照已失效"
                        if execution.node_code in affected_node_codes
                        else "人工复核改变字段事实，流程转入受影响分支重算，当前未完成执行已终止"
                    ),
                }
            if recalculation_required:
                next_node = _next_node_for_recalculation(db, task, manifest, nodes)
                if next_node is None:
                    raise BusinessError(
                        "AUDIT_RECALCULATION_NODE_MISSING",
                        "人工复核改变评估依据并失效旧结果后没有可执行的重算节点",
                        status_code=500,
                    )
                next_index = _index_of(nodes, next_node["node_code"])
                task.current_node = next_node["node_code"]
                task.status = "READY"
                task.pending_review_hash = None
                _set_task_checkpoint(task, node_index=next_index, phase="AWAITING_CONFIRMATION")
                _save_flow_checkpoint(db, task)
                db.commit()
                return {
                    "current_node": task.current_node,
                    "node_index": next_index,
                    "phase": "AWAITING_CONFIRMATION",
                    "review_hash": None,
                    "review_count": None,
                    "review_decisions": None,
                    "manual_review_result": {
                        "task_no": task.task_no,
                        "resolved_count": len(reviews),
                        "status": task.status,
                        "field_changes": field_changes,
                        "material_state_changed": material_state_changed,
                        "agent_review": True,
                        "recalculation_node": next_node["node_code"],
                        "file_quality_recompute": file_quality_recompute,
                    },
                    "agent_review_pending": False,
                    "agent_review_recalculate": True,
                    "agent_review_restore": None,
                    "execution_id": None,
                    "rule_instance": None,
                    "evaluation_id": None,
                    "fact_snapshot_ref": None,
                    "fact_snapshot_hash": None,
                    "messages": None,
                    "pending_tool_calls": [],
                    "tool_results": [],
                    "candidate_verdict": None,
                    "unresolved_items": [],
                    "last_error": None,
                }
        # 恢复执行位置：任务回到正在评估的节点，检查点回到复核之前的那一份。
        task.current_node = node_code
        task.status = "RUNNING"
        task.pending_review_hash = None
        _set_task_checkpoint(
            task,
            node_index=_index_of(nodes, node_code),
            phase=str(restore.get("phase") or "AWAITING_CONFIRMATION"),
        )
        _save_flow_checkpoint(db, task)
        db.commit()
        return {
            "current_node": task.current_node,
            "node_index": _index_of(nodes, node_code),
            # 恢复后的阶段：任务回到正在评估的节点，检查点回到复核之前的那一份。
            "phase": str(restore.get("phase") or "AWAITING_CONFIRMATION"),
            "review_hash": None,
            "review_count": None,
            "manual_review_result": {
                "task_no": task.task_no,
                "resolved_count": len(reviews),
                "status": task.status,
                "field_changes": field_changes,
                "material_state_changed": material_state_changed,
                "agent_review": True,
                "file_quality_recompute": file_quality_recompute,
            },
            "agent_review_pending": False,
            "agent_review_recalculate": False,
            "agent_review_restore": None,
            # 本批人工结论已落库：这条规则的循环不再「等待人工复核」，
            # 汇总进节点执行结果的状态因此是 DONE 而不是 NEEDS_HUMAN。
            # 继续报 NEEDS_HUMAN 会让一个已经复核完的节点看起来还在等人。
            "loop_status": LOOP_DONE,
            "last_error": None,
        }


def graph_save_evaluation_result(
    db: Session,
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
) -> dict[str, Any]:
    """保存这一条规则实例的评估结果。

    保存是确定性生命周期的一部分：Agent 不能跳过或改写。按
    (节点执行记录, 规则编号) 唯一写一条结果——人工复核已经改写过它时是更新而非新增，
    因此「人工结论」不会被随后的保存覆盖成模型候选结论。

    BLOCKED 时不写「通过」类结果：没有结论就如实记录未解决原因。
    """
    with _agent_step(db, task_no, graph_namespace):
        task = task_or_404(db, task_no, lock=True)
        _graph_namespace_or_fail(task, graph_namespace)
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
            "unresolved_items": list(unresolved_items or []),
        }
        try:
            with db.begin_nested():
                from .rule_execution import save_outputs
                save_outputs(db, task, execution, rules[0], candidate_verdict, instance, tool_results)
        except BusinessError as exc:
            logger.warning("规则产出未保存 task_no=%s rule_key=%s code=%s message=%s", task_no, rules[0]["rule_key"], exc.code, exc.message)
            candidate_verdict = None
            loop_status = LOOP_BLOCKED
            unresolved_items = [*(unresolved_items or []), {"kind": exc.code, "message": exc.message, "details": exc.details}]
            outcome.update(loop_status=loop_status, unresolved_items=unresolved_items)
        with db.begin_nested():
            _upsert_agent_rule_result(db, task=task, execution=execution, rule=rules[0], verdict=candidate_verdict,
                                     loop_status=loop_status, unresolved_items=unresolved_items, tool_results=tool_results,
                                     evaluation_id=evaluation_id, inputs=list(instance.get("inputs") or []), fact_snapshot_ref=fact_snapshot_ref)
        db.commit()
        return {
            "rule_outcome": outcome,
            "unresolved_items": [],
            "pending_tool_calls": [],
            "tool_results": [],
            "last_error": None,
        }


def graph_advance_rule_cursor(
    rule_cursor: int,
    evaluation_plan: list[dict[str, Any]] | None,
) -> dict[str, Any]:
    """推进规则游标：还有规则就冻结下一条，没有就收尾节点执行。

    这一步是纯状态推进，因此不碰数据库、也不动已累积的逐规则结论：
    是否还有规则由冻结的评估计划回答，不重新查询任务或配置。
    """
    plan = [item for item in (evaluation_plan or []) if isinstance(item, dict)]
    next_cursor = rule_cursor + 1
    return {
        "rule_cursor": next_cursor,
        "agent_done": next_cursor >= len(plan),
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


# 工具结果里进页面/报告的分析字段。它们是节点路径同样产出的结构
# （审核标准 / 实际比较 / 算式 / 加总 / 逐份材料 / 检索），Agent 路径不能只留下
# 「工具名 + 状态」，否则「事项 → 标准 → 原文 → 比较 → 结论」这条链缺中间环。
_TOOL_RESULT_ANALYSIS_KEYS = (
    "inputs",
    "standard",
    "comparison",
    "expression",
    "sum_value",
    "per_material",
    "status_counts",
    "retrieval",
    "unresolved_items",
    "applicability",
    "missing_material_keys",
    "missing_material_names",
    "extracted_values",
    "blocked_reason",
    "capability_gap",
    "check_item",
)


def _project_tool_result(item: dict[str, Any]) -> dict[str, Any]:
    """把一次工具结果投影成页面可读的规则结果片段。

    键集合来自工具目录：该工具对应能力声明的 `output_keys`，加上节点路径
    同构的分析字段。白名单方式而不是原样透传：工具结果里可能带外部服务载荷或
    与本次判定无关的大对象，原样落进规则结果会同时扩大页面体积与泄露面。
    """
    result = item.get("result") if isinstance(item.get("result"), dict) else {}
    spec = tool_catalog.tool_spec(str(item.get("tool_name") or ""))
    allowed = set(spec.output_schema.get("properties") or {}) if spec is not None else set()
    if item.get("tool_name") in {"RULE_LIBRARY_QUERY", "TASK_DATA_READ", "DOCUMENT_PROCESS"}:
        allowed.update({"items", "source", "scenario_version_no", "plan_scope_hash", "manifest_hash"})
    allowed.update(_TOOL_RESULT_ANALYSIS_KEYS)
    projected: dict[str, Any] = {
        "tool_call_id": item.get("tool_call_id"),
        "tool_name": item.get("tool_name"),
        "status": item.get("status"),
        "message": item.get("message"),
    }
    for key in sorted(allowed):
        if key in result:
            projected[key] = result[key]
    return projected


def _agent_standard_of(rule: dict[str, Any]) -> dict[str, Any]:
    """本条规则的审核标准：比对什么、按什么方式比、阈值与口径。

    与节点路径的 `standard` 同构，取自已发布配置（不可变），不由模型改写。
    """
    parameters = rule.get("parameters") if isinstance(rule.get("parameters"), dict) else {}
    comparison = parameters.get("operator")
    return {
        "rule_code": rule.get("rule_code") or rule.get("rule_key"),
        "operator": rule.get("operator"),
        "operator_label": tool_catalog.OPERATOR_LABELS.get(rule.get("operator"), rule.get("operator")),
        "requirement": rule.get("requirement") or None,
        "comparison": comparison,
        "comparison_label": comparison_label(rule.get("operator", ""), comparison),
        "threshold": parameters.get("threshold"),
        "threshold_unit": parameters.get("threshold_unit"),
        "check_level": rule.get("check_level"),
    }


def _upsert_agent_rule_result(
    db: Session,
    *,
    task: AuditTask,
    execution: NodeExecution,
    rule: dict[str, Any],
    verdict: dict[str, Any] | None,
    loop_status: str,
    unresolved_items: list[dict[str, Any]] | None,
    tool_results: list[dict[str, Any]] | None,
    evaluation_id: str | None,
    inputs: list[dict[str, Any]] | None = None,
    fact_snapshot_ref: str | None = None,
) -> RuleResult:
    """把一条规则实例的 Agent 评估结果写成规则结果（按执行记录 + 规则编号唯一）。

    已存在时只补齐系统侧字段：`status` 与 `message` 一旦被人工复核改写过就保留人工结论——
    保存候选结论不能把人工裁决覆盖回模型候选值。

    结果里必须能回答完整的阅读链，而不只是「结论 + 一句说明 + 工具名」：审核标准 `standard`；
    实际输入 `inputs`（从冻结快照解析出的字段事实）；调用了哪些工具、返回了什么确定性比较结果
    `tool_calls`（按工具目录的 `output_keys` 投影）；模型结论引用了哪些证据 `verdict.evidence`；
    以及为什么是这个结论（`status` + `loop_status` + `unresolved_items`）。
    """
    record = db.scalar(
        select(RuleResult).where(
            RuleResult.node_execution_id == execution.id,
            RuleResult.rule_key == rule["rule_key"],
        )
    )
    payload = {
        "origin": "AGENT",
        "evaluation_id": evaluation_id,
        "fact_snapshot_ref": fact_snapshot_ref,
        "loop_status": loop_status,
        "standard": _agent_standard_of(rule),
        "inputs": list(inputs or []),
        "unresolved_items": unresolved_items or [],
        "tool_calls": [
            _project_tool_result(item)
            for item in (tool_results or [])
            if isinstance(item, dict)
        ],
        "verdict": verdict,
    }
    field_definitions = {field["field_key"]: field for field in rule.get("output_definitions") or []}
    material_names = _material_names(_task_manifest(task))
    payload["outputs"] = (verdict or {}).get("outputs") or []
    payload["extracted_values"] = [{"field_key": item["code"], "field_name": field_definitions[item["code"]]["name"],
                                    "material_key": field_definitions[item["code"]]["material_key"],
                                    "material_name": material_names.get(field_definitions[item["code"]]["material_key"]),
                                    "raw_value": item.get("raw_value"), "normalized_value": item["value"],
                                    "status": "PRESENT" if item["value"] is not None else "NOT_EXECUTED"}
                                   for item in payload["outputs"] if item["code"] in field_definitions]
    if rule.get("calculation_ref"):
        payload["calculation_definition"] = rule["calculation_definition"]
        payload["calculation_steps"] = [(verdict or {}).get("message")] if verdict else []
    if rule.get("operator") == "RISK_TRIGGER" or rule.get("risk_condition"):
        # 直接模型判断与工具调用共用本条评估时冻结的真实上游结果。
        payload.update((verdict or {}).get("risk_context") or {})
        payload["risk_level"] = rule.get("risk_level_value") or (rule.get("parameters") or {}).get("risk_level")
        payload["action"] = (rule.get("parameters") or {}).get("action")
        # 来源来自成功工具调用的账本结果，不在保存阶段重查当前结论。
        # 重放与人工复核继续保留工具当时依据的结果版本和材料范围。
        for item in tool_results or []:
            if item.get("tool_name") != "RISK_TRIGGER":
                continue
            result = item.get("result") or {}
            for key in (
                "source_rules", "source_instances", "source_rule_key", "source_rule_name",
                "risk_condition", "risk_level", "action", "evidence_sources",
            ):
                if key in result:
                    payload[key] = result[key]
    if verdict is None:
        status = "NOT_EXECUTED"
        message = "；".join(str(item["message"]) for item in unresolved_items or [] if item.get("message")) or "规则未能形成执行结果"
    else:
        status = str(verdict.get("status") or "NOT_EXECUTED")
        message = str(verdict.get("message") or "")
    if record is not None:
        record.status = status
        record.message = message
        record.result_json = {**(record.result_json or {}), **payload}
    else:
        record = RuleResult(
            audit_task_id=task.id,
            node_execution_id=execution.id,
            rule_key=rule["rule_key"],
            rule_code=rule.get("rule_code") or rule.get("rule_key"),
            legacy_rule_key=rule.get("legacy_rule_key"),
            rule_name=str(rule.get("name") or rule["rule_key"]),
            status=status,
            message=message,
            result_json=payload,
        )
        db.add(record)
        db.flush()
    evidence_numbers = list(
        db.scalars(
            select(Evidence.evidence_no)
            .where(Evidence.audit_task_id == task.id, Evidence.rule_result_id == record.id)
            .order_by(Evidence.id)
        ).all()
    )
    if not evidence_numbers:
        source_evidence_ids = [
            evidence_id
            for item in (inputs or [])
            if isinstance(item, dict)
            for evidence_id in (
                item.get("evidence_ids")
                or ([item["evidence_id"]] if item.get("evidence_id") else [])
            )
        ]
        if rule.get("operator") == "RISK_TRIGGER":
            source_evidence_ids.extend(
                evidence_id
                for source in payload.get("source_rules", [])
                for evidence_id in source["evidence_ids"]
            )
        evidence_numbers = _copy_evidence_to_rule(db, task, record, source_evidence_ids)
    record.result_json = {**(record.result_json or {}), "evidence_nos": evidence_numbers}
    return record
