"""任务与节点执行的建立、收尾、进度推进。"""

import logging
from datetime import UTC, datetime
from typing import Any
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from ....common.exceptions import BusinessError
from ....domain.audit_vocab import LOOP_BLOCKED
from ....entity import AuditTask, NodeExecution
from ..tasks.lookup import task_or_404
from ..reviews import _invalidate_retry_results
from ..execution_history import invalidate_executions
from ..shared import _task_manifest
from ..state import _scenario_nodes, _set_task_checkpoint, _task_checkpoint
from .execution import running_execution, execution_in_progress
from .errors import graph_error_state


logger = logging.getLogger(__name__)

MODE_AGENT = "AGENT"

# 服务重启导致的中断失败允许被后续真实执行结果覆盖，原失败信息保留。
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


def _execution_mode(task: AuditTask, manifest: dict[str, Any]) -> str:
    """所有生效执行项统一走模型与工具循环，不按业务阶段分发。"""
    if manifest.get("execution_contract_version") != "rule_library_v1":
        raise BusinessError("RULE_LIBRARY_EXECUTION_REQUIRED", "任务采用已移除的九阶段执行配置，不能按新执行器执行；原任务快照未被改写", status_code=409)
    return MODE_AGENT


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
        attempt = db.info.get("audit_execution_attempt")
        if attempt is not None and attempt["task_no"] == task_no:
            attempt["execution_id"] = execution.id
    except BusinessError as exc:
        db.rollback()
        return graph_error_state(db, task_no, current_node, exc)
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
    active_execution = running_execution(db, task)
    if active_execution:
        raise BusinessError("AUDIT_NODE_RUNNING", "当前任务已有执行中的节点，不能并发执行或重跑", status_code=409)
    if task.status == "BLOCKED":
        _invalidate_retry_results(db, task, nodes, current_index)
    else:
        invalidate_executions(db, NodeExecution.audit_task_id == task.id,
                              NodeExecution.node_code == task.current_node,
                              NodeExecution.status.in_(["BLOCKED", "FAILED"]), reason="用户重新执行审核")
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
    execution = execution_in_progress(db, task, execution_id)
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
    execution = execution_in_progress(db, task, execution_id)
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
