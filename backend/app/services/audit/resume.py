"""继续未完成的执行批次：同一批次只重跑未完成项及其传递下游。

可继续的批次：BLOCKED、业务结果已提交但图未推进的 SUCCEEDED，以及因服务重启中断的
FAILED（``AUDIT_EXECUTION_INTERRUPTED``）。已提交且不受重跑影响的规则原样保留；
上游被重跑时，依赖它的下游一并重跑。「重新尝试」是另一种语义：另建批次并重跑全部生效规则。
"""

import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ...common.exceptions import BusinessError, exception_summary
from ...constants.status import INTERRUPTED_EXECUTION_FAILURE_CODE, LOOP_BLOCKED, TASK_STATUS_RUNNING
from ...domain.rule_library import rule_dependencies
from ...entity.tasks import AuditTask, NodeExecution, RuleResult
from ...graphs.audit_graph import collect_agent_summary
from ...graphs.audit_rule_runner import extraction_group, run_rule_evaluation
from .flow.context import graph_load_evaluation, graph_prepare_materials
from .flow.errors import fail_running_execution
from .flow.execution import running_execution
from .flow.lifecycle import (
    graph_advance_workflow,
    graph_finalize_node_execution,
    graph_save_node_result,
)
from .flow.runner import audit_runner
from .tasks.lookup import task_or_404

logger = logging.getLogger(__name__)


def resumable_execution(task: AuditTask, execution: NodeExecution | None) -> NodeExecution | None:
    """该批次是否可继续：仍属于当前节点，且存在可补跑的规则或只差图推进。

    可继续状态：
    - BLOCKED：有规则未完成或失败，继续未完成项及其下游；
    - SUCCEEDED：业务结果已提交、图未推进（收尾后崩在推进前），只需补齐推进；
    - FAILED + 中断失败码：规则执行到一半进程退出，继续未完成的规则。
    """
    if execution is None or task.status not in {"BLOCKED", "FAILED"}:
        return None
    if execution.status in {"SUCCEEDED", "BLOCKED"}:
        return execution
    if execution.status == "FAILED" and execution.failure_code == INTERRUPTED_EXECUTION_FAILURE_CODE:
        return execution
    return None


def resumable_execution_of_task(db: Session, task: AuditTask) -> NodeExecution | None:
    """当前节点最近一次可恢复的执行批次；没有则为 None。"""
    execution = db.scalar(
        select(NodeExecution)
        .where(
            NodeExecution.audit_task_id == task.id,
            NodeExecution.node_code == task.current_node,
            NodeExecution.status != "INVALIDATED",
        )
        .order_by(NodeExecution.id.desc())
        .limit(1)
    )
    return resumable_execution(task, execution)


def _rule_finished(result: RuleResult) -> bool:
    """已提交且是真实结论的规则不再重跑；被阻断的规则在恢复时重新尝试。"""
    return (result.loop_status or "DONE") != LOOP_BLOCKED and result.status != "NOT_EXECUTED"


def _saved_outcome(result: RuleResult, counted_batches: set[str]) -> dict[str, Any]:
    """已提交规则的原样结果，用于恢复时汇总，不重新调用模型。"""
    diagnostics = result.result_json or {}
    loop_count = result.turn_count or 0
    if diagnostics.get("extraction_batch") is not None:
        evaluation_id = diagnostics["evaluation_id"]
        if evaluation_id in counted_batches:
            loop_count = 0
        else:
            counted_batches.add(evaluation_id)
    return {
        "rule_key": result.rule_key,
        "rule_code": result.rule_code,
        "rule_name": result.rule_name,
        "loop_status": result.loop_status or "DONE",
        "loop_count": loop_count,
        "unresolved_items": list((result.result_json or {}).get("unresolved_items") or []),
        "status": result.status,
    }


def _rules_to_rerun(
    plan: list[dict[str, Any]], saved: dict[str, RuleResult], *, all_rules: list[dict[str, Any]],
) -> set[str]:
    """本次恢复要重跑的规则：未完成项及其全部传递下游。

    上游在恢复时重新执行后，之前用缺失输入得到的下游结论不能再作为当前结论保留；
    报告依赖全部非报告项，也会被自动加入重跑集合。
    """
    plan_keys = {entry["rule_key"] for entry in plan}
    rerun = {
        entry["rule_key"] for entry in plan
        if entry["rule_key"] not in saved or not _rule_finished(saved[entry["rule_key"]])
    }
    dependents: dict[str, set[str]] = {}
    for entry in plan:
        rule = entry["rule"]
        deps = rule_dependencies(rule, all_rules=all_rules)
        if rule.get("kind") == "REPORT":
            deps |= {other["rule_key"] for other in plan if other["rule"].get("kind") != "REPORT"}
        for dependency in deps & plan_keys:
            dependents.setdefault(dependency, set()).add(entry["rule_key"])
    pending = list(rerun)
    while pending:
        key = pending.pop()
        for dependent in dependents.get(key, set()):
            if dependent not in rerun:
                rerun.add(dependent)
                pending.append(dependent)
    return rerun


def resume_interrupted_execution(db: Session, task_no: str, *, execution_id: int) -> dict[str, Any]:
    """继续一个未完成批次：只重跑未完成规则及其传递下游，然后照常收尾与推进。

    适用于服务中断留下的批次，也适用于规则失败后仍可继续的 BLOCKED 批次。
    已提交且不受本次重跑影响的规则原样保留；上游被重跑时，依赖它的下游一并重跑。
    """
    task = task_or_404(db, task_no, lock=True)
    if task.status not in {"BLOCKED", "FAILED"}:
        raise BusinessError(
            "AUDIT_TASK_NOT_RESUMABLE", "任务当前不在可恢复状态", status_code=409,
            details={"task_no": task_no, "status": task.status},
        )
    execution = db.get(NodeExecution, execution_id)
    if execution is None or execution.audit_task_id != task.id or execution.node_code != task.current_node:
        raise BusinessError(
            "AUDIT_EXECUTION_MISSING", "恢复目标执行批次不属于任务当前节点", status_code=409,
            details={"task_no": task_no, "execution_id": execution_id},
        )
    if resumable_execution(task, execution) is None:
        raise BusinessError(
            "AUDIT_EXECUTION_NOT_RESUMABLE",
            "该执行批次没有可继续的未完成项，请使用「重新尝试」",
            status_code=409,
            details={"execution_id": execution.id, "status": execution.status,
                     "failure_code": execution.failure_code},
        )
    if running_execution(db, task) is not None:
        raise BusinessError("AUDIT_NODE_RUNNING", "当前任务已有执行中的节点，不能并发恢复", status_code=409)

    # 已成功收尾的批次只差图推进；业务结果与逐条规则都已提交，不重跑规则。
    advance_only = execution.status == "SUCCEEDED"
    # 仅推进时保留成功终态与完成时间；只有未完成的批次重新进入执行中。
    if not advance_only:
        if execution.status == "FAILED" and execution.failure_code == INTERRUPTED_EXECUTION_FAILURE_CODE:
            # 批次随后置 RUNNING 并清空失败码，收尾阶段已看不到中断状态；
            # 先把中断事实写进结果，续跑成功后仍能追溯这次执行曾被服务重启打断。
            execution.result_json = {
                **(execution.result_json or {}),
                "recovered_from_interrupted_execution": {
                    "failure_code": execution.failure_code,
                    "failure_message": execution.failure_message,
                },
            }
        execution.status = "RUNNING"
        execution.finished_at = None
        execution.failure_code = None
        execution.failure_message = None
    task.status = TASK_STATUS_RUNNING
    db.commit()

    try:
        if advance_only:
            advanced = graph_advance_workflow(db, task_no, execution.id)
            graph_save_node_result(db, task_no)
            return advanced["last_execution"]
        graph_prepare_materials(db, task_no, execution.id)
        loaded = graph_load_evaluation(db, task_no, task.current_node, execution.id)
        plan = loaded["evaluation_plan"]
        saved = {
            result.rule_key: result
            for result in db.scalars(
                select(RuleResult).where(RuleResult.node_execution_id == execution.id)
            ).all()
        }
        outcomes: dict[str, dict[str, Any]] = {}
        counted_batches: set[str] = set()
        rerun_keys = _rules_to_rerun(
            plan, saved, all_rules=task.manifest_json["rules"],
        )
        # 与主执行路径同一套调度：同一材料上相邻的无输入抽取合并为一次模型调用。
        index = 0
        while index < len(plan):
            group = extraction_group(plan, index)
            index += len(group)
            pending = [entry for entry in group if entry["rule_key"] in rerun_keys]
            for entry in group:
                if entry["rule_key"] not in rerun_keys:
                    outcomes[entry["rule_key"]] = _saved_outcome(saved[entry["rule_key"]], counted_batches)
            if len(pending) > 1:
                for outcome in audit_runner.graph_execute_extraction_batch(
                    db, task_no, task.current_node, execution.id, pending,
                ):
                    outcomes[outcome["rule_key"]] = outcome
                continue
            for entry in pending:
                outcomes[entry["rule_key"]] = run_rule_evaluation(
                    db, audit_runner, task_no=task_no, node_code=task.current_node,
                    execution_id=execution.id, rule_cursor=entry["cursor"], planned_rule=entry["rule"],
                )
        ordered = [outcomes[entry["rule_key"]] for entry in plan]
        summary = collect_agent_summary(
            {entry["rule_key"] for entry in plan}, ordered,
            sum(item["loop_count"] for item in ordered),
        )
        graph_finalize_node_execution(db, task_no, execution.id, "AGENT", None, summary)
        advanced = graph_advance_workflow(db, task_no, execution.id)
        graph_save_node_result(db, task_no)
        return advanced["last_execution"]
    except BusinessError as exc:
        fail_running_execution(
            db, task_no, execution_id=execution.id, code=exc.code,
            message=str(exc.details.get("reason") or exc.message),
        )
        raise
    except Exception as exc:  # noqa: BLE001 - 未处理异常必须显式落库
        logger.exception("恢复审核执行发生未处理异常", extra={"task_no": task_no, "execution_id": execution.id})
        fail_running_execution(
            db, task_no, execution_id=execution.id,
            code="AUDIT_GRAPH_RUNTIME_FAILED", message=exception_summary(exc),
        )
        raise BusinessError(
            "AUDIT_GRAPH_RUNTIME_FAILED", "恢复执行失败，具体原因见执行记录和服务日志",
            status_code=500, details={"reason": exception_summary(exc)},
        ) from exc
