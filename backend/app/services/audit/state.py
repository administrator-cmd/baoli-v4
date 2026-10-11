"""任务执行状态、当前有效批次与流程检查点的读写。

三个「当前」口径在这里定义，其他地方不得自行重算：当前上传版本是
`TaskDocument.current_upload_id` 指向的那一份（历史上传保留为历史，不进入当前结果）；
当前有效执行批次是每个节点最新且未被 INVALIDATED 的执行（报告、风险与规则输入都从这里取结果，
执行项顺序由实际字段与上游结果引用决定）；当前事实版本
是字段事实按 (任务, 字段, 材料位) 唯一的那条 `is_current`。

检查点写在这里是为了让「业务事务」与「图等待点」的提交顺序只有一处实现：
`_set_task_checkpoint` 只改内存，`_save_flow_checkpoint` 落 WorkflowCheckpoint，
真正的 commit 由命令层负责。"""

from .. import task_csv_service

from ...domain.audit_vocab import is_type_unavailable
from ...common.exceptions import BusinessError
from ...entity import AuditTask, DocumentUpload, NodeExecution, ScenarioVersion, TaskDocument, TaskFieldValue, WorkflowCheckpoint
from .shared import _current_uploads, _scenario, _task_manifest
from sqlalchemy import select, update
from sqlalchemy.orm import Session
from typing import Any


def _material_classify_result(content: dict[str, Any] | None) -> dict[str, Any] | None:
    """待办快照里的 8018 识别结论：只取人工判断真用得上的字段。"""
    if not isinstance(content, dict):
        return None
    return {
        "declared_material_type": content.get("declared_material_type"),
        "detected_material_type": content.get("detected_material_type"),
        "matches_declared_material": content.get("matches_declared_material"),
        "confidence": content.get("confidence"),
        "matched_keywords": content.get("matched_keywords"),
    }

def _current_node_executions(db: Session, task: AuditTask) -> dict[str, NodeExecution]:
    """每个节点的“当前有效执行批次”。

    每个节点只取最新一条未失效的执行。DAG 的 ``depends_on`` 才是执行依据，
    ``sequence_no`` 只用于展示和确定性排序；如果允许高序号节点先于低序号节点执行，
    按当前节点序号截断会漏掉真实的依赖结果。因此这里保留任务场景中的所有节点，
    再按节点实例码选最新批次。

    同一节点被重跑但旧执行尚未失效时也能取到最新批次。最新执行若停留在 RUNNING，
    仍按该节点没有可用终态结果处理；这是执行中断时的显式状态，不用旧批次冒充当前结果。
    """
    _, nodes, _ = _scenario_nodes(db, task)
    allowed = {item["node_code"] for item in nodes}
    latest: dict[str, NodeExecution] = {}
    for execution in db.scalars(
        select(NodeExecution)
        .where(NodeExecution.audit_task_id == task.id, NodeExecution.status != "INVALIDATED")
        .order_by(NodeExecution.id)
    ).all():
        if execution.node_code in allowed:
            latest[execution.node_code] = execution
    return latest

def _current_execution_ids(db: Session, task: AuditTask) -> set[int]:
    return {item.id for item in _current_node_executions(db, task).values()}

def _material_states(db: Session, task: AuditTask) -> dict[str, str | None]:
    """材料位的当前状态，供算子判断依赖依据是否有效。

    TYPE_REVIEW=类型待复核（尚未由人定案）；TYPE_REJECTED=人工已确认与材料位不符；
    PREPARED=文件处理完成；ONLY_UPLOADED=只有上传、尚未形成处理产物；未上传返回 None
    （缺输入由字段状态表达）。

    这两种状态传给算子的后果不同：TYPE_REJECTED 是人工确定结论，依赖它的检查缺少有效依据；
    TYPE_REVIEW 不是对材料内容的否定（识别结论来自已下线为仅供参考的自动识别），依赖它的检查
    照常按材料中的真实原文判定，类型本身另行作为待确认事项暴露。
    """
    states: dict[str, str | None] = {}
    for document, upload in _current_uploads(db, task):
        status = document.status
        if is_type_unavailable(status):
            states[document.material_key] = status
        elif upload is None:
            states[document.material_key] = None
        elif status == "PREPARED":
            states[document.material_key] = "PREPARED"
        else:
            states[document.material_key] = "ONLY_UPLOADED"
    return states

def _task_field_value_of_current_upload(db: Session, task: AuditTask, field_value_id: int) -> TaskFieldValue | None:
    """取当前上传版本的当前字段事实。

    待复核项必须绑定当前事实版本。只校验上传版本而不校验 ``is_current``，
    会让已经被重新抽取或人工纠错替换的旧事实仍可被提交，随后覆盖新事实。
    历史事实仍保留给审计读取，但不能再作为待办裁决的写入目标。
    """
    row = db.scalar(
        select(TaskFieldValue)
        .join(DocumentUpload, DocumentUpload.id == TaskFieldValue.document_upload_id)
        .join(TaskDocument, TaskDocument.id == DocumentUpload.task_document_id)
        .where(
            TaskFieldValue.id == field_value_id,
            TaskFieldValue.audit_task_id == task.id,
            TaskFieldValue.scenario_version_id == task.scenario_version_id,
            TaskFieldValue.is_current.is_(True),
            TaskDocument.audit_task_id == task.id,
            TaskDocument.material_key == TaskFieldValue.material_key,
            DocumentUpload.audit_task_id == task.id,
            DocumentUpload.is_current.is_(True),
            TaskDocument.current_upload_id == TaskFieldValue.document_upload_id,
        )
    )
    return task_csv_service.from_index(db, task, TaskFieldValue, [row])[0] if row is not None else None


def _result_material_keys(record: Any) -> set[str]:
    """一条检查/风险结果实际涉及的材料 key。

    来源是执行时保存的输入摘要（每条输入都带 material_key），而不是规则声明的适用材料：
    规则可能适用于三种材料，但这次异常只发生在其中一种，报告按实际发生的材料筛选。
    文件质量检查没有字段输入，它的归属写在同一结构里的 `inputs` 摘要中
    （由 `_quality_material_inputs` 按逐份材料的判定结果生成）。Agent 工具的逐份判定
    保存于 tool_calls.per_material；存在性检查显式保存 checked_material_keys。
    """
    payload = record.result_json if isinstance(record.result_json, dict) else {}
    keys = {
        str(item.get("material_key"))
        for item in (payload.get("inputs") or [])
        if isinstance(item, dict) and item.get("material_key")
    }
    # 材料工具没有字段事实，不从配置猜归属；只读取执行结果实际检查过的材料。
    for result in [payload, *(payload.get("tool_calls") or [])]:
        keys.update(str(key) for key in result.get("checked_material_keys", []))
        keys.update(
            str(item["material_key"])
            for item in result.get("per_material", [])
            if item.get("material_key") and item.get("status") != "NOT_APPLICABLE"
        )
    if not keys:
        # 风险规则本身没有输入字段：它的材料归属只能来自当前有效的来源检查实例。
        # 只用规则声明的适用材料会把范围外材料算进来，也说明不了这条风险实际发生在哪份材料。
        keys = {
            str(item.get("material_key"))
            for item in (payload.get("source_instances") or [])
            if isinstance(item, dict) and item.get("material_key")
        }
    return keys

def _scope_results(
    results: list[Any],
    scope_keys: list[str],
    material_names: dict[str, str],
) -> tuple[list[Any], list[tuple[Any, list[str]]], int]:
    """按报告材料范围筛选已有结果，返回（范围内, [(跨范围结果, 范围外材料名)], 无归属条数）。

    范围内：结果实际涉及的材料至少有一个在报告范围内。
    跨范围：结果还涉及范围外的材料——它作为对照/关联证据保留，但不归责为范围内材料自身异常。
    无归属：结果完全没有材料归属时不能放进任何报告，但必须计数返回：
    调用方要能区分“确实没有结果”与“有结果但归不了属”，否则筛选漏项会被写成“没有风险”。
    """
    in_scope: list[Any] = []
    cross: list[tuple[Any, list[str]]] = []
    unattributed = 0
    scope = set(scope_keys)
    for record in results:
        keys = _result_material_keys(record)
        if not keys:
            unattributed += 1
            continue
        inside = keys & scope
        if not inside:
            continue
        outside = keys - scope
        if outside:
            cross.append(
                (record, [material_names.get(key, key) for key in sorted(outside)])
            )
        else:
            in_scope.append(record)
    return in_scope, cross, unattributed

def _risk_rule_refs(manifest: dict[str, Any]) -> set[tuple[str, str]]:
    return {
        (item["node"], item["rule_key"])
        for item in manifest.get("rules", [])
        if item.get("operator") == "RISK_TRIGGER" or item.get("risk_condition")
    }






def _scenario_nodes(db: Session, task: AuditTask) -> tuple[ScenarioVersion, list[dict[str, Any]], int]:
    scenario = _scenario(db, task)
    nodes = sorted(_task_manifest(task)["nodes"], key=lambda item: item["sequence_no"])
    current_index = next((index for index, item in enumerate(nodes) if item["node_code"] == task.current_node), None)
    if current_index is None:
        raise BusinessError("AUDIT_NODE_NOT_IN_SCENARIO", "当前节点不属于任务场景", status_code=500)
    return scenario, nodes, current_index

def _task_checkpoint(task: AuditTask) -> dict[str, Any]:
    if not isinstance(task.checkpoint_json, dict):
        raise BusinessError("AUDIT_CHECKPOINT_MISSING", "审核流程检查点不存在", status_code=409)
    return task.checkpoint_json

def _set_task_checkpoint(
    task: AuditTask,
    *,
    node_index: int,
    phase: str,
    review_hash: str | None = None,
    review_count: int | None = None,
) -> None:
    previous = _task_checkpoint(task)
    checkpoint: dict[str, Any] = {
        "node_index": node_index,
        "current_node": task.current_node,
        "phase": phase,
    }
    graph_namespace = previous.get("graph_checkpoint_ns")
    if isinstance(graph_namespace, str):
        checkpoint["graph_checkpoint_ns"] = graph_namespace
    if review_hash is not None:
        checkpoint["review_hash"] = review_hash
    if review_count is not None:
        checkpoint["review_count"] = review_count
    task.checkpoint_json = checkpoint

def _graph_namespace(task: AuditTask) -> str:
    value = _task_checkpoint(task).get("graph_checkpoint_ns")
    if not isinstance(value, str) or not value:
        raise BusinessError("AUDIT_CHECKPOINT_MISSING", "审核流程图检查点命名空间不存在", status_code=409)
    return value

def _validate_graph_namespace(task: AuditTask, graph_namespace: str) -> dict[str, Any]:
    checkpoint = _task_checkpoint(task)
    if checkpoint.get("graph_checkpoint_ns") != graph_namespace:
        raise BusinessError("AUDIT_GRAPH_STALE", "审核流程已被材料替换或新的流程实例取代", status_code=409)
    return checkpoint

def _flow_checkpoint_state(task: AuditTask) -> dict[str, Any]:
    checkpoint = _task_checkpoint(task)
    node_index = checkpoint.get("node_index")
    phase = checkpoint.get("phase")
    if not isinstance(node_index, int) or not isinstance(phase, str):
        raise BusinessError("AUDIT_CHECKPOINT_INVALID", "审核流程检查点内容无效", status_code=500)
    state: dict[str, Any] = {
        "flow": "AUDIT",
        "phase": phase,
        "current_node": task.current_node,
        "node_index": node_index,
    }
    if phase == "WAITING_REVIEW":
        review_hash = checkpoint.get("review_hash")
        review_count = checkpoint.get("review_count")
        if not isinstance(review_hash, str) or not isinstance(review_count, int):
            raise BusinessError("REVIEW_CHECKPOINT_MISSING", "人工复核流程检查点缺少待复核集合", status_code=409)
        state["review_hash"] = review_hash
        state["review_count"] = review_count
    return state

def _save_flow_checkpoint(db: Session, task: AuditTask) -> None:
    # 检查点随任务状态一起落盘：调用方可能刚把任务置为 BLOCKED（节点阻断），
    # 而 `task.status` 是普通列，未 flush 时 `_flow_checkpoint_state` 读到的仍是旧值。
    # 先 flush 再生成检查点，两者才不会一个说「阻断」一个说「执行中」。
    db.flush()
    graph_namespace = _graph_namespace(task)
    checkpoint = db.scalar(
        select(WorkflowCheckpoint).where(
            WorkflowCheckpoint.audit_task_id == task.id,
            WorkflowCheckpoint.checkpoint_ns == graph_namespace,
        )
    )
    state = _flow_checkpoint_state(task)
    status = "COMPLETED" if state["phase"] == "COMPLETED" else "WAITING"
    if checkpoint:
        checkpoint.status = status
        checkpoint.state_json = state
        return
    db.add(
        WorkflowCheckpoint(
            audit_task_id=task.id,
            thread_id=task.thread_id,
            checkpoint_ns=graph_namespace,
            status=status,
            state_json=state,
        )
    )

def _fail_unrecoverable_checkpoint(db: Session, task: AuditTask, message: str) -> None:
    """图等待点确实丢失时不能让任务继续显示成「可恢复」：显式落终态并说明原因。"""
    task.status = "FAILED"
    task.pending_review_hash = None
    db.execute(
        update(WorkflowCheckpoint)
        .where(
            WorkflowCheckpoint.audit_task_id == task.id,
            WorkflowCheckpoint.status == "WAITING",
        )
        .values(status="FAILED")
    )
    db.commit()
    raise BusinessError("AUDIT_CHECKPOINT_MISSING", message, status_code=409)
