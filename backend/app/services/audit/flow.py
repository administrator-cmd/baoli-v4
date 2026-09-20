"""审核图的节点实现：状态推进、节点分发与人工复核等待点。

节点分发按任务当前节点走，业务任务表是「当前节点」的权威来源：人工复核或材料裁决
会把任务退回更早的节点，此时图 state 里记录的仍是退回前的节点，按图 state 执行会被
误判成「提交节点不一致」。

事务边界（必须保持）：

- 节点执行前先把执行记录与任务状态提交，节点内部**不做**跨调用的事务；
- `DOCUMENT_PREPARE` 不加嵌套事务——它要真实调用外部服务，产物的幂等占位必须在
  调用前就已提交（见 `artifacts`）；其余节点用 `begin_nested()` 包住业务写入，
  失败时业务写入整体回滚，执行记录与任务状态另行落 FAILED/BLOCKED；
- 图节点函数只推进状态并提交，不承载业务规则本身。"""

import logging
from ...errors import BusinessError, exception_summary
from ...models import AuditTask, NodeExecution
from ...schemas import ExecuteNodeRequest, ManualReviewRequest
from ..task_lookup import task_or_404
from .facts import _field_extraction, _material_check
from .quality import _document_prepare, _sync_type_review_pending
from .reporting import _report_output
from .reviews import _apply_manual_reviews_direct, _current_review_batch, _invalidate_retry_results, _review_hash
from .rules import _execute_rules
from .shared import _scenario
from .state import _flow_checkpoint_state, _save_flow_checkpoint, _scenario_nodes, _set_task_checkpoint, _validate_graph_namespace
from datetime import UTC, datetime
from pydantic import ValidationError
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session
from typing import Any


logger = logging.getLogger(__name__)


def _dispatch(db: Session, task: AuditTask, manifest: dict[str, Any], execution: NodeExecution) -> dict[str, Any]:
    node = task.current_node
    if node == "MATERIAL_CHECK":
        return _material_check(db, task, manifest, execution)
    if node == "DOCUMENT_PREPARE":
        return _document_prepare(db, task, manifest, execution)
    if node == "FIELD_EXTRACTION":
        return _field_extraction(db, task, manifest, execution)
    if node in {"INTERNAL_VALIDATE", "CROSS_VALIDATE", "RISK_RATING"}:
        return _execute_rules(db, task, manifest, execution, node)
    if node == "REPORT_OUTPUT":
        return _report_output(db, task, manifest, execution)
    raise BusinessError("AUDIT_NODE_UNKNOWN", "审核节点不受支持", details={"node": node})

def graph_load_task(db: Session, task_no: str, graph_namespace: str) -> dict[str, Any]:
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
        if task.status != "WAITING_REVIEW" or task.current_node != "MANUAL_REVIEW":
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

def _execute_current_node_direct(db: Session, task_no: str, request: ExecuteNodeRequest) -> dict[str, Any]:
    task = task_or_404(db, task_no, lock=True)
    if not task.checkpoint_json:
        raise BusinessError("AUDIT_CHECKPOINT_MISSING", "审核流程检查点不存在", status_code=409)
    if task.status in {"COMPLETED", "FAILED"}:
        raise BusinessError("AUDIT_TASK_FINISHED", "审核任务已结束，不能继续执行", status_code=409)
    if task.status == "RUNNING":
        raise BusinessError("AUDIT_TASK_RUNNING", "任务正在执行当前节点，不能并发执行或重跑", status_code=409)
    if request.node_code != task.current_node:
        raise BusinessError("AUDIT_NODE_MISMATCH", "提交节点与任务当前节点不一致", status_code=409)
    scenario = _scenario(db, task)
    manifest = scenario.manifest_json
    nodes = sorted(manifest["nodes"], key=lambda item: item["sequence_no"])
    current_index = next((index for index, item in enumerate(nodes) if item["node_code"] == task.current_node), None)
    if current_index is None:
        raise BusinessError("AUDIT_NODE_NOT_IN_SCENARIO", "当前节点不属于任务场景", status_code=500)

    if task.current_node == "MANUAL_REVIEW":
        raise BusinessError("AUDIT_GRAPH_MANUAL_ROUTE", "人工复核必须由审核流程图处理", status_code=500)
    running_execution = db.scalar(
        select(NodeExecution).where(
            NodeExecution.audit_task_id == task.id,
            NodeExecution.status == "RUNNING",
        )
    )
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
    db.commit()
    try:
        if task.current_node == "DOCUMENT_PREPARE":
            result = _dispatch(db, task, manifest, execution)
        else:
            with db.begin_nested():
                result = _dispatch(db, task, manifest, execution)
        if result.get("blocked"):
            execution.status = "BLOCKED"
            execution.finished_at = datetime.now(UTC)
            execution.result_json = result
            execution.failure_code = "REQUIRED_MATERIAL_MISSING"
            execution.failure_message = "必传材料尚未上传：" + "、".join(result.get("missing_materials") or [])
            task.status = "BLOCKED"
            _save_flow_checkpoint(db, task)
            db.commit()
            return {
                "task_no": task.task_no,
                "node_name": nodes[current_index]["name"],
                "execution_status": "BLOCKED",
                "task_status": task.status,
                "next_node_name": None,
                "result_summary": result,
            }
        execution.status = "SKIPPED" if result.get("skipped") else "SUCCEEDED"
        execution.finished_at = datetime.now(UTC)
        execution.result_json = result
    except BusinessError as exc:
        execution.status = "BLOCKED" if exc.code in {"REQUIRED_MATERIAL_MISSING"} else "FAILED"
        execution.finished_at = datetime.now(UTC)
        execution.failure_code = exc.code
        execution.failure_message = exc.message
        task.status = "BLOCKED"
        _save_flow_checkpoint(db, task)
        db.commit()
        raise
    except Exception as exc:
        # 摘要带上异常类型与出错位置并落库：只写一句通用文案会让这次失败事后无法诊断
        # （完整堆栈仍在日志里）。执行记录页与任务页据此能直接看到是哪一行抛的。
        logger.exception("审核节点执行发生未处理异常", extra={"task_no": task.task_no, "node_code": task.current_node})
        execution.status = "FAILED"
        execution.finished_at = datetime.now(UTC)
        execution.failure_code = "AUDIT_NODE_UNEXPECTED_ERROR"
        execution.failure_message = exception_summary(exc)
        task.status = "BLOCKED"
        _save_flow_checkpoint(db, task)
        db.commit()
        raise BusinessError("AUDIT_NODE_UNEXPECTED_ERROR", "审核节点执行失败，请查看任务失败信息", status_code=500) from exc
    next_node = nodes[current_index + 1] if current_index + 1 < len(nodes) else None
    if next_node:
        task.current_node = next_node["node_code"]
        task.status = "READY"
        _set_task_checkpoint(task, node_index=current_index + 1, phase="AWAITING_CONFIRMATION")
    else:
        task.status = "COMPLETED"
        _set_task_checkpoint(task, node_index=current_index, phase="COMPLETED")
    _save_flow_checkpoint(db, task)
    db.commit()
    return {
        "task_no": task.task_no,
        "node_name": nodes[current_index]["name"],
        "execution_status": execution.status,
        "task_status": task.status,
        "next_node_name": next_node["name"] if next_node else None,
        "result_summary": result,
    }

def graph_execute_current_node(
    db: Session,
    task_no: str,
    graph_namespace: str,
    current_node: str,
    confirmed_node: str | None,
) -> dict[str, Any]:
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
    _, _, current_index = _scenario_nodes(db, task)
    if task.current_node == "MANUAL_REVIEW":
        return {
            "executed_node": task.current_node,
            "current_node": task.current_node,
            "node_index": current_index,
            "phase": "MANUAL_REVIEW_PREPARE",
            "last_execution": None,
            "last_error": None,
        }
    try:
        response = _execute_current_node_direct(db, task_no, ExecuteNodeRequest(node_code=task.current_node))
    except BusinessError as exc:
        return _graph_error_state(db, task_no, graph_namespace, current_node, exc)
    task = task_or_404(db, task_no)
    checkpoint = _validate_graph_namespace(task, graph_namespace)
    _, _, next_index = _scenario_nodes(db, task)
    return {
        "executed_node": current_node,
        "current_node": task.current_node,
        "node_index": next_index,
        "phase": checkpoint["phase"],
        "last_execution": response,
        "last_error": None,
    }

def graph_prepare_manual_review(db: Session, task_no: str, graph_namespace: str) -> dict[str, Any]:
    task = task_or_404(db, task_no, lock=True)
    checkpoint = _validate_graph_namespace(task, graph_namespace)
    if checkpoint.get("phase") != "AWAITING_CONFIRMATION" or task.current_node != "MANUAL_REVIEW":
        raise BusinessError("MANUAL_REVIEW_NOT_WAITING", "任务当前不在人工复核准备状态", status_code=409)
    _, nodes, current_index = _scenario_nodes(db, task)
    # 进入复核节点前先把「类型待复核」材料位的待办补齐：历史任务在这里可能一个待办都没有，
    # 直接按“没有待复核项”跳过，等于让类型待复核永远得不到裁决。
    type_review_pending = _sync_type_review_pending(db, task)
    # 本批只处理“事实批次”：存在字段确认时先解决字段事实与材料类型，规则复核留到下一轮。
    reviews = _current_review_batch(db, task)
    if not reviews:
        # 显式跳过并说明原因，绝不等价于复核通过。跳过会改写 current_node 与检查点，
        # 因此重复执行不可能再次推进（重复进入会因为节点/阶段不匹配被明确拒绝）。
        result = {
            "review_count": 0,
            "skipped": True,
            "skip_reason": "当前没有待人工复核项（字段事实与规则结果均无待确认项），人工复核节点已显式跳过并继续",
            # 本次为类型待复核材料位补齐的待办；为空表示确实没有任何类型待复核材料位。
            "type_review_pending": type_review_pending,
        }
        sequence_no = (db.scalar(select(func.max(NodeExecution.sequence_no)).where(NodeExecution.audit_task_id == task.id)) or 0) + 1
        db.add(
            NodeExecution(
                audit_task_id=task.id,
                scenario_version_id=task.scenario_version_id,
                node_code=task.current_node,
                sequence_no=sequence_no,
                status="SKIPPED",
                started_at=datetime.now(UTC),
                finished_at=datetime.now(UTC),
                result_json=result,
            )
        )
        next_node = nodes[current_index + 1] if current_index + 1 < len(nodes) else None
        if next_node:
            task.current_node = next_node["node_code"]
            task.status = "READY"
            _set_task_checkpoint(task, node_index=current_index + 1, phase="AWAITING_CONFIRMATION")
        else:
            task.status = "COMPLETED"
            _set_task_checkpoint(task, node_index=current_index, phase="COMPLETED")
        _save_flow_checkpoint(db, task)
        db.commit()
        return {
            "current_node": task.current_node,
            "node_index": current_index + 1 if next_node else current_index,
            "phase": "AWAITING_CONFIRMATION" if next_node else "COMPLETED",
            "last_execution": {
                "task_no": task.task_no,
                "node_name": nodes[current_index]["name"],
                "execution_status": "SKIPPED",
                "task_status": task.status,
                "next_node_name": next_node["name"] if next_node else None,
                "result_summary": result,
            },
            "last_error": None,
        }

    digest = _review_hash(reviews)
    task.pending_review_hash = digest
    task.status = "WAITING_REVIEW"
    _set_task_checkpoint(
        task,
        node_index=current_index,
        phase="WAITING_REVIEW",
        review_hash=digest,
        review_count=len(reviews),
    )
    _save_flow_checkpoint(db, task)
    db.commit()
    return {
        "current_node": task.current_node,
        "node_index": current_index,
        "phase": "WAITING_REVIEW",
        "review_hash": digest,
        "review_count": len(reviews),
        "last_execution": {
            "task_no": task.task_no,
            "node_name": nodes[current_index]["name"],
            "execution_status": "WAITING_REVIEW",
            "task_status": task.status,
            "next_node_name": None,
            "result_summary": {"pending_review_count": len(reviews)},
        },
        "last_error": None,
    }

def graph_apply_manual_reviews(
    db: Session,
    task_no: str,
    graph_namespace: str,
    review_hash: str | None,
    review_count: int | None,
    decisions: list[dict[str, Any]] | None,
) -> dict[str, Any]:
    if not isinstance(review_hash, str) or not isinstance(review_count, int) or not isinstance(decisions, list):
        raise BusinessError("REVIEW_CHECKPOINT_MISSING", "人工复核流程检查点缺少待复核集合", status_code=409)
    try:
        request = ManualReviewRequest.model_validate({"reviews": decisions})
    except ValidationError as exc:
        raise BusinessError("MANUAL_REVIEW_PAYLOAD_INVALID", "人工复核提交内容无效", status_code=409) from exc
    result = _apply_manual_reviews_direct(
        db,
        task_no,
        request,
        graph_namespace,
        review_hash,
        review_count,
    )
    task = task_or_404(db, task_no)
    checkpoint = _validate_graph_namespace(task, graph_namespace)
    _, _, current_index = _scenario_nodes(db, task)
    return {
        "current_node": task.current_node,
        "node_index": current_index,
        "phase": checkpoint["phase"],
        # 本批提交后可能仍有待办（复核分批）：把当前等待集合的哈希与条数写回图 state，
        # 否则下一次 await_manual_review 用的还是上一批的值，提交会被判成集合已变化。
        "review_hash": checkpoint.get("review_hash"),
        "review_count": checkpoint.get("review_count"),
        "manual_review_result": result,
        "review_decisions": None,
        "last_error": None,
    }

def graph_save_node_result(db: Session, task_no: str, graph_namespace: str) -> dict[str, Any]:
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
