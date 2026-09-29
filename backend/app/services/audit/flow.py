"""审核图的节点实现：状态推进、节点分发与人工复核等待点。

节点分发按任务当前节点走，业务任务表是「当前节点」的权威来源：人工复核或材料裁决
会把任务退回更早的节点，此时图 state 里记录的仍是退回前的节点，按图 state 执行会被
误判成「提交节点不一致」。

分发方式：**查节点注册表**（domain.node_registry），不再写 if/elif。
注册发生在应用启动（`register_node_handlers`），因此 domain 层不必反向依赖 services。

一次节点执行被拆成四步，同一规则实例只会由其中一条路径写结果：

    graph_execute_current_node     开始执行：建执行记录、按冻结配置确定执行模式
    graph_run_deterministic_node   确定性路径：原节点实现（固定流程 + 按规则分发）
    （Agent 路径：load_evaluation → … → save_evaluation_result → advance_rule_cursor）
    graph_finalize_node_execution  收尾：写执行记录的终态与结果
    graph_advance_workflow         推进：按 DAG 选下一个节点、更新任务状态与检查点

拆分的理由：Agent 评估期间任务仍停在**当前节点**上，`task.current_node` 不能被提前推进，
否则 Agent 拿到的「当前节点」已经是下一个，且规则结果会被写两次。旧实现在执行完
当前节点后才判断要不要进 Agent，那时 current_node 已经变了。

执行模式在执行**之前**由冻结 manifest 决定（`_execution_mode`），依据是：

- 节点能力类型是否属于按规则执行的校验节点（内部/交叉校验、风险评定）；
- 该节点是否有启用规则、且这些规则引用了**可调用的 Agent 工具**。

节点里没有授权工具的规则仍由确定性路径执行，与 Agent 规则互补——
每条规则实例只由一条路径写结果，不存在「两边都写」或「两边都不写」。

事务边界（必须保持）：

- 节点执行前先把执行记录与任务状态提交，节点内部**不做**跨调用的事务；
- `DOCUMENT_PREPARE` 不加嵌套事务——它要真实调用外部服务，产物的幂等占位必须在
  调用前就已提交（见 `artifacts`）；其余节点用 `begin_nested()` 包住业务写入，
  失败时业务写入整体回滚，执行记录与任务状态另行落 FAILED/BLOCKED；
- Agent 的模型调用与工具执行期间**不持有**数据库行锁：登记、执行、写回各是短事务；
- 图节点函数只推进状态并提交，不承载业务规则本身。"""

import hashlib
import logging
from contextlib import contextmanager
from ...domain import capabilities, node_registry, workflow_dag
from ...domain.identity import public_no
from ...domain.manifest import comparison_label
from ...errors import BusinessError, exception_summary
from ...models import AuditTask, Evidence, ManualReview, NodeExecution, RuleResult
from ...schemas import ManualReviewRequest
from ..task_lookup import task_or_404
from .facts import _field_extraction, _material_check, _refresh_extraction_rule_results
from .evidence import _copy_evidence_to_rule, _rule_evidence_snapshots
from .calculation import _calculation
from .quality import _document_prepare, _sync_type_review_pending
from .reporting import _report_output
from .reviews import (
    _apply_manual_reviews_direct,
    _apply_review_decisions,
    _current_review_batch,
    _decision_command_no,
    _invalidate_execution_records,
    _invalidate_file_quality_results,
    _invalidate_retry_results,
    _invalidate_validation_results,
    _next_node_for_recalculation,
    _recompute_file_quality_results,
    _review_hash,
    _validate_review_payloads,
)
from .rules import _execute_rules, _risk_source_result_keys
from .shared import _material_names, _rule_applicability, _scenario, _task_manifest
from .state import _current_execution_ids, _flow_checkpoint_state, _is_node_of_type, _node_codes_of_type, _save_flow_checkpoint, _scenario_nodes, _set_task_checkpoint, _single_node_code_of_type, _validate_graph_namespace
from datetime import UTC, datetime
from pydantic import ValidationError
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session
from typing import Any, Iterator


logger = logging.getLogger(__name__)

# 已注册的节点执行实现。应用启动时调用一次；重复调用不重复注册。
_HANDLERS_REGISTERED = False

# 会走「规则评估 Agent」的节点能力类型。声明在 `domain/node_registry.AGENT_NODE_TYPES`：
# 工具目录判定「一个工具是否 Agent 可选择」用的是同一份集合，两处同源才不会出现
# 「目录说模型能调、执行上却没有这条路径」。
_AGENT_NODE_TYPES = node_registry.AGENT_NODE_TYPES

# 执行模式。写进图 state，由条件边决定走哪条路径。
MODE_DETERMINISTIC = "DETERMINISTIC"
MODE_AGENT = "AGENT"
MODE_MANUAL_REVIEW = "MANUAL_REVIEW_PREPARE"

# Agent 循环状态。与 `services.audit.agent` 的常量一致。
LOOP_RUNNING = "RUNNING"
LOOP_NEEDS_HUMAN = "NEEDS_HUMAN"
LOOP_DONE = "DONE"
LOOP_BLOCKED = "BLOCKED"

# 被服务重启打断、由 `commands.recover_interrupted_executions` 写成 FAILED 的执行记录。
# 这类终态是「进程被杀」而不是「节点真的失败」，Agent 路径在人工复核之后仍会收尾，
# 因此允许被真实结果覆盖，并在结果里保留原失败信息。
_INTERRUPTED_FAILURE_CODE = "AUDIT_EXECUTION_INTERRUPTED"


def register_node_handlers() -> None:
    """把节点执行实现注册进节点注册表。

    在应用启动时调用。它把「能力类型 → 实现」的映射集中到一处，
    分发处只查表；新增节点类型只需在这里加一行，不会再出现
    「节点清单与 if/elif 两处不一致」的情况。
    """
    global _HANDLERS_REGISTERED
    if _HANDLERS_REGISTERED:
        return
    node_registry.register_node_handler(node_registry.NODE_TYPE_MATERIAL_CHECK, _material_check)
    node_registry.register_node_handler(node_registry.NODE_TYPE_DOCUMENT_PREPARE, _document_prepare)
    node_registry.register_node_handler(node_registry.NODE_TYPE_FIELD_EXTRACTION, _field_extraction)
    # 三类校验节点共用同一个规则执行实现：它们的差别在规则数据里（rule.node），
    # 不在执行逻辑里。这里分别注册，是为了让「哪些节点类型可执行」可枚举。
    node_registry.register_node_handler(node_registry.NODE_TYPE_INTERNAL_VALIDATE, _execute_rules)
    node_registry.register_node_handler(node_registry.NODE_TYPE_CROSS_VALIDATE, _execute_rules)
    node_registry.register_node_handler(node_registry.NODE_TYPE_CALCULATION, _calculation)
    node_registry.register_node_handler(node_registry.NODE_TYPE_RISK_RATING, _execute_rules)
    node_registry.register_node_handler(node_registry.NODE_TYPE_REPORT_OUTPUT, _report_output)
    _HANDLERS_REGISTERED = True


def node_type_of(task: AuditTask, manifest: dict[str, Any]) -> str:
    """任务当前节点的**能力类型**。

    节点的能力类型决定谁来执行。旧 manifest 的节点没有 `node_type`：
    那时节点码本身就是能力类型，因此直接用它——这不是兜底，是旧格式的确定性语义。
    """
    node_code = task.current_node
    for item in manifest.get("nodes", []):
        if item.get("node_code") == node_code:
            node_type = item.get("node_type")
            return str(node_type) if node_type else node_code
    raise BusinessError("AUDIT_NODE_UNKNOWN", "审核节点不受支持", details={"node": node_code})


def _dispatch(db: Session, task: AuditTask, manifest: dict[str, Any], execution: NodeExecution) -> dict[str, Any]:
    node_type = node_type_of(task, manifest)
    handler = node_registry.handler_for(node_type)
    if node_type in {
        node_registry.NODE_TYPE_INTERNAL_VALIDATE,
        node_registry.NODE_TYPE_CROSS_VALIDATE,
        node_registry.NODE_TYPE_RISK_RATING,
        # 计算节点同样按**实例码**归属规则：同一份计算规则可以被多个计算节点引用，
        # 不传实例码会让它拿能力类型当实例码去过滤，一条规则都匹配不上、
        # 节点静默报「本模板未配置计算规则」并记为 SKIPPED。
        node_registry.NODE_TYPE_CALCULATION,
    }:
        # 这些节点的规则执行需要知道**节点实例码**（规则按它归属），
        # 因此把当前节点码显式传进去；其余节点不需要这个参数。
        return handler(db, task, manifest, execution, task.current_node)
    return handler(db, task, manifest, execution)

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
        if task.status != "WAITING_REVIEW" or task.current_node != _single_node_code_of_type(
            _task_manifest(task), "MANUAL_REVIEW", purpose="人工复核等待点"
        ):
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


def _execution_mode(db: Session, task: AuditTask, manifest: dict[str, Any]) -> str:
    """本次节点执行的模式。**在执行之前**由冻结配置决定。

    依据只有两条，都是确定性事实，不看模型的任何输出：

    1. 节点的能力类型是否属于按规则执行的校验节点（内部/交叉校验、风险评定）——
       其余节点的执行方式是固定的，不存在「让模型选工具」的空间；
    2. 该节点是否有启用规则、且这些规则引用了**可调用的 Agent 工具**
       （`manifest.allowed_tools` 由发布期按规则实际引用的能力算出）。

    两者同时成立才走 Agent 路径；否则与改造前完全一致地走确定性路径。
    判定发生在执行之前，因此「当前节点」在执行期间不会被提前推进，
    同一条规则实例也只可能由其中一条路径写结果。
    """
    node_type = node_type_of(task, manifest)
    if node_type not in _AGENT_NODE_TYPES:
        return MODE_DETERMINISTIC
    agent_rules, _ = _partition_rules(db, task, manifest, task.current_node)
    # 没有任何规则引用了可调用的 Agent 工具：该节点没有工具可用，这是配置事实，
    # 不是错误。旧场景（没有工具目录的 manifest）因此完全不受影响。
    return MODE_AGENT if agent_rules else MODE_DETERMINISTIC


def _enabled_rules(db: Session, task: AuditTask, manifest: dict[str, Any], node_code: str) -> list[dict[str, Any]]:
    enabled_keys = set(task.task_schema_json["enabled_rule_keys"])
    return [
        rule
        for rule in manifest["rules"]
        if rule.get("node") == node_code and rule.get("rule_key") in enabled_keys
    ]


def _partition_rules(
    db: Session,
    task: AuditTask,
    manifest: dict[str, Any],
    node_code: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """把本节点的启用规则分成（Agent 规则, 确定性规则）。

    划分依据是「这条规则引用的能力是否有可调用的 Agent 工具」——它就是
    `allowed_tools` 的生成口径（见 `domain/tool_catalog.allowed_tools_of`），
    因此两边不会各算一套。两类规则互斥且互补：每条规则实例只由一条路径写结果。

    这样划分是必要的：主体检索、语义判断等能力没有独立可调用的工具，
    若把它们一并交给 Agent，模型既没有工具可用、也无法得出这几类结论，
    规则会被静默丢掉。
    """
    agent = _agent_module()
    rules = _enabled_rules(db, task, manifest, node_code)
    agent_rules: list[dict[str, Any]] = []
    deterministic_rules: list[dict[str, Any]] = []
    for rule in rules:
        rule_code = str(rule.get("rule_code") or rule.get("rule_key") or "")
        if agent.tools_for_node(
            manifest, node_code=node_code, rule_codes=(rule_code,), rule_keys=(rule["rule_key"],),
        ):
            agent_rules.append(rule)
        else:
            deterministic_rules.append(rule)
    return agent_rules, deterministic_rules


def graph_execute_current_node(
    db: Session,
    task_no: str,
    graph_namespace: str,
    current_node: str,
    confirmed_node: str | None,
) -> dict[str, Any]:
    """开始执行当前节点：建执行记录、确定执行模式并提交。

    这里**不执行**节点本身。执行由确定性路径或 Agent 路径完成，两条路径共用
    同一条执行记录，最后由 `graph_finalize_node_execution` 写终态、由
    `graph_advance_workflow` 推进任务。
    """
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
        # 成员判断：场景没配人工复核节点时答案是「不是」，不是「配置缺失」。
        if _is_node_of_type(manifest, task.current_node, "MANUAL_REVIEW"):
            return {
                "executed_node": task.current_node,
                "current_node": task.current_node,
                "node_index": current_index,
                "phase": "MANUAL_REVIEW_PREPARE",
                "execution_id": None,
                "execution_mode": MODE_MANUAL_REVIEW,
                "node_result": None,
                "last_execution": None,
                "last_error": None,
            }
        execution = _begin_node_execution(db, task, manifest, nodes, current_index)
        mode = _execution_mode(db, task, manifest)
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

    事务边界与改造前一致：执行记录与任务状态在节点真正开始之前就已提交，
    这样进程在节点执行中崩溃时，留下的 RUNNING 执行记录是「上次执行中断」的真实表达。
    """
    # 同上：成员判断必须容错，否则没配复核节点的场景连普通节点都执行不了。
    if _is_node_of_type(manifest, task.current_node, "MANUAL_REVIEW"):
        raise BusinessError("AUDIT_GRAPH_MANUAL_ROUTE", "人工复核必须由审核流程图处理", status_code=500)
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


def graph_run_deterministic_node(
    db: Session,
    task_no: str,
    graph_namespace: str,
    execution_id: int | None,
) -> dict[str, Any]:
    """确定性路径：调节点原有的执行实现。

    执行实现与改造前完全一致（`_dispatch` 按能力类型查注册表）。唯一的差别是
    「推进到下一个节点」被移到 `graph_advance_workflow`：Agent 路径与确定性路径
    必须共用同一处推进逻辑，否则两条路径的节点顺序会各写一套。
    """
    task = task_or_404(db, task_no, lock=True)
    _validate_graph_namespace(task, graph_namespace)
    manifest = _task_manifest(task)
    execution = _execution_in_progress(db, task, execution_id)
    try:
        # 文件处理节点不套嵌套事务：它要真实调用外部服务，产物占位必须先提交。
        # 按**能力类型**判断，不写死实例码。
        if node_type_of(task, manifest) == "DOCUMENT_PREPARE":
            result = _dispatch(db, task, manifest, execution)
        else:
            with db.begin_nested():
                result = _dispatch(db, task, manifest, execution)
        db.commit()
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
    if result.get("blocked"):
        execution.status = "BLOCKED"
        execution.finished_at = datetime.now(UTC)
        execution.result_json = result
        execution.failure_code = "REQUIRED_MATERIAL_MISSING"
        execution.failure_message = "必传材料尚未上传：" + "、".join(result.get("missing_materials") or [])
        task.status = "BLOCKED"
        _save_flow_checkpoint(db, task)
        db.commit()
    return {"node_result": result, "executed_node": task.current_node, "last_error": None}


def graph_finalize_node_execution(
    db: Session,
    task_no: str,
    graph_namespace: str,
    execution_id: int | None,
    execution_mode: str,
    node_result: dict[str, Any] | None,
    agent_summary: dict[str, Any] | None,
) -> dict[str, Any]:
    """收尾当前节点执行：写执行记录的终态与结果。

    两条路径共用这一处：

    - 确定性路径的结果来自节点实现（`node_result`）；
    - Agent 路径的结果来自 `advance_rule_cursor` 汇总的逐规则结论（`agent_summary`）。

    已经写过终态的记录（必传材料缺失的 BLOCKED、或在执行中途被服务重启打断的 FAILED）
    不再覆盖，只如实回报：把一次未完成说成完成，比失败本身更危险。
    """
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
    if execution_mode == MODE_AGENT:
        summary = dict(agent_summary or {})
        execution.result_json = summary
        if summary.get("loop_status") == LOOP_BLOCKED:
            # 规则评估整体无法继续（轮次上限、模型响应不可用、工具未授权……）：
            # 执行记录写**阻断终态**，任务与检查点一起停在这里，由既有重试路径重跑。
            # 不能写成 SUCCEEDED 再在结果里附一个错误字段——那会让节点显示「已完成」
            # 并让 DAG 继续推进，而实际有规则从未形成结论。
            execution.status = "BLOCKED"
            execution.failure_code = "AGENT_EVALUATION_BLOCKED"
            execution.failure_message = "部分规则未能形成候选结论，原因见执行结果的未解决事项"
            task.status = "BLOCKED"
        else:
            execution.status = "SUCCEEDED"
    else:
        result = node_result or {}
        execution.status = "SKIPPED" if result.get("skipped") else "SUCCEEDED"
        execution.result_json = result
    if recovered_from is not None:
        execution.result_json = {
            **(execution.result_json or {}),
            "recovered_from_interrupted_execution": {
                "failure_code": recovered_from,
                "failure_message": recovered_message,
            },
        }
        # 覆盖被中断的 FAILED 只在本次执行**确实成功**时做：本次又阻断/失败时，
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
    """推进流程：按 DAG 选下一个就绪节点，更新任务状态与检查点。

    与改造前 `_execute_current_node_direct` 尾部的语义完全一致：节点的完成与否
    由执行记录的终态表达，DAG 据此选择下一个节点；全部完成则任务 COMPLETED。
    """
    task = task_or_404(db, task_no, lock=True)
    checkpoint = _validate_graph_namespace(task, graph_namespace)
    manifest = _task_manifest(task)
    nodes = sorted(manifest["nodes"], key=lambda item: item["sequence_no"])
    _, _, current_index = _scenario_nodes(db, task)
    execution = _execution_in_progress(db, task, execution_id)
    if execution.status == "BLOCKED":
        # 必传材料缺失：任务显式阻断在**当前节点**，检查点不动，用户补传后可重新执行。
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


def _completed_nodes(db: Session, task: AuditTask) -> set[str]:
    """本任务已完成（SUCCEEDED 或显式 SKIPPED）的节点实例码。

    口径与「当前有效执行批次」一致：只取未失效的执行记录。
    RUNNING 与 FAILED/BLOCKED 都不算完成——失败的节点必须显式失败或阻断，
    不能被当成「做完了」而让下游继续。
    """
    return {
        item.node_code
        for item in db.scalars(
            select(NodeExecution).where(
                NodeExecution.audit_task_id == task.id,
                NodeExecution.status.in_(["SUCCEEDED", "SKIPPED"]),
            )
        ).all()
    }


def _next_node_after(
    db: Session,
    task: AuditTask,
    manifest: dict[str, Any],
    nodes: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """按 DAG 选出下一个可执行节点；没有就绪节点时返回 None。

    与旧的 `nodes[current_index + 1]` 的差别：旧实现假设流程是线性表，
    节点顺序就是执行顺序。这里按**依赖是否满足**选择，因此：

    - 旧场景（依赖写成「依赖前一个节点」）得到的顺序与旧实现完全一致；
    - 新场景（财报：字段抽取 → 计算 → 风险）按数据流推进，
      即使 sequence_no 与依赖顺序不一致也不会执行错。

    没有就绪节点时区分两种情况：全部完成（返回 None，任务置 COMPLETED）与
    DAG 卡住（抛错，任务保持可诊断状态）。把卡住当成完成会让任务显示为已完成
    而实际有节点从未执行。
    """
    plans = workflow_dag.plans_of(manifest)
    completed = _completed_nodes(db, task)
    # 刚执行的节点已写入 SUCCEEDED/SKIPPED，因此 completed 里已包含它。
    completed.add(task.current_node)
    pending_reviews = list(
        db.scalars(
            select(ManualReview.review_no).where(
                ManualReview.audit_task_id == task.id,
                ManualReview.status == "PENDING",
            )
        ).all()
    )
    ready = (
        workflow_dag.ready_node_for_pending_review(plans, completed=completed)
        if pending_reviews
        else workflow_dag.ready_node(plans, completed=completed)
    )
    if pending_reviews and ready is None:
        raise BusinessError(
            "AUDIT_REVIEW_NODE_UNAVAILABLE",
            "仍有待人工复核项，但当前流程无法到达人工复核节点",
            status_code=409,
            details={"review_nos": pending_reviews},
        )
    if ready is not None:
        return next((item for item in nodes if item["node_code"] == ready.node_instance_code), None)
    blocked = workflow_dag.blocked_nodes(plans, completed=completed)
    if blocked:
        raise BusinessError(
            "AUDIT_WORKFLOW_BLOCKED",
            "审核流程无法继续：以下节点的依赖尚未完成——"
            + "、".join(f"“{item.label}”" for item in blocked),
            status_code=409,
            details={"blocked_nodes": [item.node_instance_code for item in blocked]},
        )
    return None

def graph_prepare_manual_review(db: Session, task_no: str, graph_namespace: str) -> dict[str, Any]:
    """进入人工复核前的准备：补齐类型待复核的待办，并选出本批待复核项。"""
    task = task_or_404(db, task_no, lock=True)
    checkpoint = _validate_graph_namespace(task, graph_namespace)
    manifest = _task_manifest(task)
    if checkpoint.get("phase") != "AWAITING_CONFIRMATION" or task.current_node != _single_node_code_of_type(
        manifest, "MANUAL_REVIEW", purpose="人工复核等待点"
    ):
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
        # 人工复核节点显式跳过：按同一套 DAG 选下一个就绪节点，不用线性下标。
        # 这里不重新查 _completed_nodes：刚写入的 SKIPPED 执行记录尚未 flush 到
        # 可查状态时会被漏掉，因此把当前节点直接并入已完成集合。
        next_node = _next_node_after(
            db, task, manifest, nodes
        ) if manifest.get("nodes") else None
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
    """把本批人工复核结论交回图内执行；待复核集合身份缺失或不符时明确 409。"""
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
# 一次评估只处理**一条规则实例**：State 里保存当前规则实例与游标，
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
    db: Session,
    task: AuditTask,
    manifest: dict[str, Any],
    node_code: str,
) -> dict[str, Any]:
    """本节点的评估范围：节点级授权工具与工具策略版本。

    两者都在评估开始时**冻结**，本次评估的全部工具调用共用同一份身份——
    这是「工具结果绑定到当时的事实快照与工具策略版本」的落点。
    """
    agent = _agent_module()
    return {
        "node_tools": agent.allowed_tools_of(manifest, node_code=node_code),
        "policy": agent.policy_of(manifest),
    }


def _plan_entry(node_code: str, cursor: int, rule: dict[str, Any]) -> dict[str, Any]:
    """评估计划里的一条规则实例。

    计划只包含**引用了可调用 Agent 工具**的启用规则，与 `_execution_mode` 的划分
    同源（同一个 `_partition_rules`）。其余规则由确定性路径执行，不会被漏掉、
    也不会被两条路径各写一次。
    """
    return {
        "cursor": cursor,
        "rule_key": rule.get("rule_key"),
        "rule_code": rule.get("rule_code") or rule.get("rule_key"),
        "node_code": node_code,
    }


def _plan_rule(
    db: Session,
    task: AuditTask,
    manifest: dict[str, Any],
    node_code: str,
    rule_cursor: int,
) -> dict[str, Any]:
    """按游标取本节点的第 `rule_cursor` 条 Agent 规则（顺序与评估计划一致）。

    取的是 `_partition_rules` 划分出的**Agent 规则**，不是本节点全部规则：
    没有授权工具的规则由确定性路径执行，指到它会得到一条永远评估不了的规则。
    """
    agent_rules, _ = _partition_rules(db, task, manifest, node_code)
    if rule_cursor < 0 or rule_cursor >= len(agent_rules):
        raise BusinessError(
            "AGENT_RULE_CURSOR_INVALID",
            "规则评估游标越界，本次评估未继续",
            status_code=500,
            details={"node_code": node_code, "rule_cursor": rule_cursor, "rule_count": len(agent_rules)},
        )
    return agent_rules[rule_cursor]


def _evaluation_id(task: AuditTask, graph_namespace: str, execution_id: int, rule_instance_id: str) -> str:
    """一次规则实例评估的批次标识。**由身份推导**，不随机生成。

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
    """规则实例标识：同一条规则在**不同节点执行批次**里是不同实例。"""
    return f"{node_code}:{rule_identity}:NE{execution_id}"


def _openai_tools(allowed_tools: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """按冻结的工具定义构造模型的 `tools` 参数。

    模型看到的必须是**发布期冻结的那一份定义**，不是当前工具目录：目录升级后
    旧任务仍按冻结定义执行，用当前目录重算会让历史结论无法复现，也会让
    「模型当时看到的是什么」失去答案。清单里缺少冻结定义时由
    `agent.frozen_tool_definitions` 显式报错，不静默改用当前目录。
    """
    definitions = _agent_module().frozen_tool_definitions(allowed_tools)
    return [
        {"type": "function", "function": dict(definition)}
        for definition in sorted(definitions, key=lambda item: str(item.get("name")))
    ]


@contextmanager
def _agent_step(db: Session, task_no: str, graph_namespace: str) -> Iterator[None]:
    """Agent 路径单步的失败处理：先落成显式的节点失败，再原样抛出。

    模型调用与工具执行会失败（配置缺失、供应商超时、快照过期、凭据失效），
    也可能抛出**未预期异常**（唯一键冲突、JSON 序列化失败、工具参数错误）。
    两类都必须写进执行记录并把任务置 BLOCKED，**异常摘要原样保留**：
    不记录的话任务会停在 RUNNING、执行记录停在 RUNNING，用户既不能重跑也看不到原因。

    错误码的分工是刻意的：

    - `BusinessError` 的码**原样上抛**（快照冲突、输入缺失、工具参数非法……），
      调用方据此知道真实原因；
    - 其余异常写 `AUDIT_NODE_UNEXPECTED_ERROR` 并把异常类型、消息与出错位置
      一起落进 `failure_message`，同时以**同一个错误码的 BusinessError** 抛出。
      这样图包装层不会把它再包成「检查点不可达」——那会把快照唯一键冲突、
      序列化失败这类真实缺陷伪装成存储故障。

    只对「确实有一次执行正在进行」的情况落失败：任务号、执行记录与图命名空间三者
    都对得上才写。对不上说明这次调用根本不属于当前流程（例如等待点已被材料替换取代），
    此时不碰任何记录，只把原错误抛出。
    """
    try:
        yield
    except BusinessError as exc:
        _fail_agent_execution(db, task_no, graph_namespace, code=exc.code, message=exc.message)
        raise
    except Exception as exc:  # noqa: BLE001 - 未预期异常必须落库后再暴露
        logger.exception(
            "规则评估发生未处理异常 task_no=%s graph_namespace=%s", task_no, graph_namespace, exc_info=exc
        )
        summary = exception_summary(exc)
        _fail_agent_execution(
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


def _fail_agent_execution(
    db: Session,
    task_no: str,
    graph_namespace: str,
    *,
    code: str,
    message: str,
) -> None:
    """把当前执行记录写成 FAILED、任务置 BLOCKED，并提交。

    对不上任务号 / 执行记录 / 图命名空间时不写任何记录（见 `_agent_step` 的说明）。
    """
    db.rollback()
    task = db.scalar(select(AuditTask).where(AuditTask.task_no == task_no))
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
    """加载节点评估：评估计划、节点级授权工具与工具策略版本。

    本节点只在**执行模式为 Agent** 时进入（见 `_execution_mode`），因此计划必然非空：
    没有任何规则引用可调用工具时该节点走确定性路径，根本到不了这里。

    节点里没有授权工具的规则在这里由确定性实现执行掉（`deterministic_result`）：
    两类规则互补，每条规则实例只由一条路径写结果。
    """
    with _agent_step(db, task_no, graph_namespace):
        task = task_or_404(db, task_no, lock=True)
        _graph_namespace_or_fail(task, graph_namespace)
        manifest = _task_manifest(task)
        execution = _execution_in_progress(db, task, execution_id)
        agent_rules, deterministic_rules = _partition_rules(db, task, manifest, node_code)
        deterministic_result: dict[str, Any] | None = None
        if deterministic_rules:
            with db.begin_nested():
                deterministic_result = _execute_rules(db, task, manifest, execution, node_code, deterministic_rules)
            db.commit()
        scope = _evaluation_scope(db, task, manifest, node_code)
        return {
            "agent_mode": True,
            "evaluation_plan": [_plan_entry(node_code, index, rule) for index, rule in enumerate(agent_rules)],
            "rule_cursor": 0,
            "deterministic_result": deterministic_result,
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
    """冻结**一条**规则实例：评估身份、事实快照、授权工具与输入事实。

    评估身份由身份推导（`_evaluation_id`），快照按
    (评估批次, 规则实例, 输入修订) 幂等冻结：重复执行不会产生第二份输入，
    恢复也不会重新生成评估身份或工具调用标识。
    """
    with _agent_step(db, task_no, graph_namespace):
        task = task_or_404(db, task_no, lock=True)
        _graph_namespace_or_fail(task, graph_namespace)
        manifest = _task_manifest(task)
        execution = _execution_in_progress(db, task, execution_id)
        rule = _plan_rule(db, task, manifest, node_code, rule_cursor)
        rule_code = str(rule.get("rule_code") or rule.get("rule_key"))
        agent = _agent_module()
        allowed = agent.allowed_tools_of(
            manifest, node_code=node_code, rule_codes=(rule_code,), rule_keys=(rule["rule_key"],),
        )
        # 旧任务的授权清单没有 rule_keys，继续沿用原身份，避免恢复时改写评估批次。
        identity = rule["rule_key"] if any("rule_keys" in item for item in allowed) else rule_code
        rule_instance_id = _rule_instance_id(node_code, identity, execution.id)
        evaluation_id = _evaluation_id(task, graph_namespace, execution.id, rule_instance_id)
        policy = agent.policy_of(manifest)
        with db.begin_nested():
            snapshot = agent.freeze_snapshot(
                db,
                task=task,
                manifest=manifest,
                node_code=node_code,
                rule_instance_id=rule_instance_id,
                evaluation_id=evaluation_id,
                allowed_tools=allowed,
                policy=policy,
            )
        db.commit()
        record = agent.load_snapshot(db, task=task, snapshot_ref=snapshot["ref"])
        inputs = agent.inputs_of_snapshot(record, rule)
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

    消息历史只在**首次**进入这条规则实例时生成；已有历史就原样保留——
    重新生成等于把模型已经看到的内容换掉，恢复后的判断会与中断前不一致。

    输入门禁先于消息复用：恢复或重放时即使已有消息，也不能让无效字段事实
    绕过当前规则的输入校验。正常恢复的输入没有变化，仍会原样复用消息。

    提示里只用**本规则实际使用的**事实与标识：给全量事实会让模型在无关字段上
    做判断，也会让它有机会引用不属于这条规则的输入。
    """
    with _agent_step(db, task_no, graph_namespace):
        task = task_or_404(db, task_no, lock=True)
        _graph_namespace_or_fail(task, graph_namespace)
        manifest = _task_manifest(task)
        instance = rule_instance or {}
        rule = _rule_of_instance(manifest, node_code, instance)
        agent = _agent_module()
        inputs = list(instance.get("inputs") or [])
        invalid = [
            item for item in inputs
            if item.get("status") not in {"PRESENT", "REVIEWED"}
            or item.get("normalized_value") is None
        ]
        if invalid:
            # Agent 规则无论使用哪个算子，都只能读取已经定型的字段事实。
            # 候选原文保留给人工复核，但不会进入提示词或工具调用参数。
            return {
                "messages": [],
                "loop_status": LOOP_NEEDS_HUMAN,
                "unresolved_items": [{
                    "kind": "INCOMPLETE_FIELD_FACTS",
                    "message": (
                        "规则输入没有全部形成有效字段事实，未调用大模型："
                        + "、".join(
                            f"{item.get('field_key')}@{item.get('material_key')}（{item.get('status')}）"
                            for item in invalid
                        )
                    ),
                    "missing_inputs": [
                        f"{item.get('field_key')}@{item.get('material_key')}" for item in invalid
                    ],
                }],
                "last_error": None,
            }
        if messages:
            return {"messages": list(messages), "last_error": None}
        return {
            "messages": agent.initial_messages(
                rule,
                inputs,
                material_names=_material_names(manifest),
                manifest=manifest,
            ),
            "last_error": None,
        }


def _loop_limit_reached(loop_count: int) -> int | None:
    """本节点这一轮是否已经到达工具循环上限；到达时返回上限值。

    **轮次的定义全链路只有一种：一次模型调用算一轮**（与 `loop_count` 的计数口径、
    与 `integrations.llm.gateway.call_with_tools` 的 `rounds` 完全一致）。
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
    """到达上限时的显式阻断状态：原因、已用轮次与**已经执行过**的工具调用。"""
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
    """**调用模型一次**，把这一轮的响应写回 State。

    这里不执行工具：模型请求了工具就由 `graph_execute_tool_calls` 执行，
    再由 `graph_append_tool_results` 生成 tool 消息，然后回到本节点问下一轮。
    每次调用模型都发生在图节点里，因此每一轮都是可 checkpoint 的状态。

    轮次上限（`settings.llm_tool_max_rounds`，按**模型调用次数**计）在这里兜底：

    - 已用完额度时不再调用模型，直接进 `LOOP_BLOCKED`；
    - 这一轮是最后一轮、而模型仍请求工具时，**不执行这一批工具**，
      同样进 `LOOP_BLOCKED`。

    两种情况都写入 `LLM_TOOL_LOOP_LIMIT` 与未解决事项，并保留已执行的工具账本
    （`executed_tool_results`）与已得到的模型响应，交后续按阻断处理——
    既不默认通过，也不静默结束。
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
        tools = _openai_tools(allowed_tools)
        turn = agent.call_model_once(list(messages or []), tools=tools)
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
                "loop_status": LOOP_NEEDS_HUMAN,
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
    """执行 State 中的**这一批**待执行工具调用。

    每次只执行 `pending_tool_calls` 里的调用，执行完即清空——「下一批」由模型
    下一轮请求产生。因此本节点不构成循环，重复进入只会执行新的一批。

    工具输入从**冻结快照**解析，运行期状态（风险来源结论）由这里注入；
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
        runtime = _tool_runtime(db, task, rule)
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
            result, _ = agent.invoke_tool(db, call=call, inputs=inputs, context=context, runtime=runtime, rule=rule, manifest=manifest)
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

    单独成一个节点而不是塞进 `execute_tool_calls`：消息历史与工具执行是两件事，
    合在一起会让「工具执行成功但消息没写进 State」这种半成品状态无法分辨。

    **同一个供应商调用标识在消息历史里只能有一条 `tool` 消息**：`tool_results`
    会跨轮累积（供最终证据校验使用），若把整份累积结果逐项追加，第 N 轮会把
    前 N-1 轮的消息再追加一遍——同一 `tool_call_id` 出现多条，上下文平方级膨胀，
    还可能被供应商判为协议错误。因此这里只追加历史中尚未出现的标识，
    Graph checkpoint 重放也不会重复生成同一条消息。
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
        # 回填消息用**供应商**的调用标识：模型认的是它自己给出的那个；
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
    """校验模型给出的候选结论，形成**可采信的结论**或明确的未解决事项。

    服务端校验在 `services.audit.agent.validate_verdict`：状态枚举、证据引用、
    与确定性工具结果的冲突、以及不得越过 EVIDENCE_INSUFFICIENT / FAILED / NEEDS_RECONCILE。
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

    顺序不能颠倒——必须先把规则结果与待办落库、再切等待状态：

    1. 写规则结果（候选结论，`origin=AGENT`），待办才有可关联的业务对象；
    2. 建 `manual_review` 待办，保存模型候选结论、确定性工具结果与冲突原因；
    3. 生成复核集合哈希与条数，把任务状态与检查点切成 `WAITING_REVIEW`；
    4. 返回进入 interrupt 所需的集合身份。

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
        review_index = _index_of(nodes, _single_node_code_of_type(manifest, "MANUAL_REVIEW", purpose="人工复核等待点"))
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
            task.current_node = _single_node_code_of_type(manifest, "MANUAL_REVIEW", purpose="人工复核等待点")
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

    裁决落库与人工复核节点**共用同一实现**（`reviews._apply_review_decisions`），
    这里只负责两件它不做的事：

    1. 校验提交的集合与进入等待时的集合逐条一致（哈希 + 条数）；
    2. 恢复任务当前节点与检查点——Agent 复核发生在某个校验节点**执行期间**，
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
        if task.status != "WAITING_REVIEW" or task.current_node != _single_node_code_of_type(
            _task_manifest(task), "MANUAL_REVIEW", purpose="人工复核等待点"
        ):
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
        if material_state_changed:
            invalidated_quality = _invalidate_file_quality_results(db, task, manifest)
            file_quality_recompute = _recompute_file_quality_results(
                db, task, manifest, invalidated_quality["node_codes"]
            )
        if field_changes:
            execution = _running_execution(db, task)
            _refresh_extraction_rule_results(db, task, manifest)
            changed_field_keys = {str(item["field_key"]) for item in field_changes}
            invalidation = _invalidate_validation_results(db, task, manifest, changed_field_keys)
            recalculation_required = invalidation["invalidated_executions"] > 0
            if recalculation_required and execution is not None:
                affected_node_codes = set(invalidation["affected_node_codes"])
                if execution.node_code not in affected_node_codes:
                    # 受影响的已完成分支需要回退时，当前 Agent 执行无法继续悬挂为 RUNNING；
                    # 它尚未形成有效节点结果，显式终止后由 DAG 在合适位置重新执行。
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
    """保存**这一条规则实例**的评估结果。

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
        with db.begin_nested():
            _upsert_agent_rule_result(
                db,
                task=task,
                execution=execution,
                rule=rules[0],
                verdict=candidate_verdict,
                loop_status=loop_status,
                unresolved_items=unresolved_items,
                tool_results=tool_results,
                evaluation_id=evaluation_id,
                inputs=list(instance.get("inputs") or []),
                fact_snapshot_ref=fact_snapshot_ref,
            )
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
    是否还有规则由**冻结的评估计划**回答，不重新查询任务或配置。
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


# 工具结果里进页面/报告的**分析字段**。它们是节点路径同样产出的结构
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

    键集合来自**工具目录**：该工具对应能力声明的 `output_keys`，加上节点路径
    同构的分析字段。白名单方式而不是原样透传：工具结果里可能带外部服务载荷或
    与本次判定无关的大对象，原样落进规则结果会同时扩大页面体积与泄露面。
    """
    result = item.get("result") if isinstance(item.get("result"), dict) else {}
    capability = capabilities.capability_of(str(item.get("tool_name") or ""))
    allowed = set(capability.output_keys) if capability is not None else set()
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
    """本条规则的**审核标准**：比对什么、按什么方式比、阈值与口径。

    与节点路径的 `standard` 同构，取自已发布配置（不可变），不由模型改写。
    """
    parameters = rule.get("parameters") if isinstance(rule.get("parameters"), dict) else {}
    comparison = parameters.get("operator")
    return {
        "rule_code": rule.get("rule_code") or rule.get("rule_key"),
        "operator": rule.get("operator"),
        "operator_label": capabilities.OPERATOR_LABELS.get(rule.get("operator"), rule.get("operator")),
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

    已存在时**只补齐系统侧字段**：`status` 与 `message` 一旦被人工复核改写过，
    就保留人工结论——保存候选结论不能把人工裁决覆盖回模型候选值。

    结果里必须能回答完整的阅读链，而不只是「结论 + 一句说明 + 工具名」：

    - 审核标准：`standard`（本条规则比对什么、按什么方式、阈值与口径）；
    - 实际输入：`inputs`（从冻结快照解析出的字段事实）；
    - 调用了哪些工具、工具返回了什么确定性比较结果：`tool_calls`（按工具目录的
      `output_keys` 投影，含 `comparison` / `expression` / `sum_value` / `per_material`）；
    - 模型结论引用了哪些证据：`verdict.evidence`；
    - 为什么通过 / 异常 / 不可判定 / 转人工：`status` + `loop_status` + `unresolved_items`。
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
    if verdict is None:
        status = "NOT_EXECUTED"
        message = "规则评估未能形成候选结论，原因见未解决事项"
    else:
        status = str(verdict.get("status") or "NOT_EXECUTED")
        message = str(verdict.get("message") or "")
    if record is not None:
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
        evidence_numbers = _copy_evidence_to_rule(db, task, record, source_evidence_ids)
    record.result_json = {**(record.result_json or {}), "evidence_nos": evidence_numbers}
    return record


def _tool_runtime(db: Session, task: AuditTask, rule: dict[str, Any]) -> dict[str, Any] | None:
    """本次工具调用需要的**运行期状态**，由调用方按身份解析后注入。

    这些值模型不能提供：它们是数据库里的真实结果（来源检查的当前有效结论）或
    已发布配置事实（风险成立条件、规则适用条件、审核要求原文）。模型只给身份引用。

    返回 None 表示本规则的工具不需要任何运行期状态；工具目录声明了 `runtime_keys`
    却拿不到时，`services.tool_service._merge_runtime` 会显式报缺失，不静默放行。
    """
    capability_id = capabilities.capability_id_of_rule(rule)
    if capability_id == "RISK_TRIGGER":
        return _risk_runtime(db, task, rule)
    if capability_id == "MATERIAL_EXISTS":
        # 材料存在性检查的适用条件与检查口径原文来自**已发布配置**：
        # 模型填一个适用条件就等于它自己决定这条规则适不适用。
        applicability = _rule_applicability(rule)
        return {
            "applicability": applicability,
            "requirement": rule.get("requirement"),
            "check_level": rule.get("check_level"),
        }
    return None


def _risk_runtime(db: Session, task: AuditTask, rule: dict[str, Any]) -> dict[str, Any] | None:
    """风险规则需要的运行期状态：来源检查的当前有效结论与成立条件。

    模型不能提供这些值（它们是数据库里的真实结果与冻结配置），
    也不能由 domain 层查库，因此在这里解析后注入。
    """
    if rule.get("operator") != "RISK_TRIGGER":
        return None
    source_keys = _risk_source_result_keys(rule, _task_manifest(task))
    current_ids = _current_execution_ids(db, task)
    prior = (
        {
            item.rule_key: item.status
            for item in db.scalars(
                select(RuleResult).where(
                    RuleResult.audit_task_id == task.id,
                    RuleResult.node_execution_id.in_(current_ids),
                )
            ).all()
        }
        if current_ids
        else {}
    )
    return {
        "source_statuses": [prior.get(key) for key in source_keys],
        "risk_condition": rule.get("risk_condition"),
    }
