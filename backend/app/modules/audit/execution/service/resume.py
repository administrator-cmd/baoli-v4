"""继续未完成的执行批次：同一批次只重跑未完成项及其传递下游。

可继续的批次：BLOCKED、业务结果已提交但图未推进的 SUCCEEDED，以及失败原因允许原地重算的
FAILED（服务重启中断、模型渠道故障）。程序异常、存储故障等 FAILED 要先处理原因，直接续跑
只会再次撞上同一个问题。「全部重新执行」是另一种语义：另建批次并重跑全部生效规则。

恢复范围只由「未完成的规则 + 它们的传递下游」决定，不看结论好坏：异常、证据不足、不适用
都是已提交的有效结论，不会因为不是「通过」而重新调用模型。
"""

import logging
from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from app.common.exceptions import BusinessError, exception_summary
from app.constants.status import (
    AUDIT_LLM_CHANNEL_UNAVAILABLE,
    EXECUTION_STATUS_WAITING_FIELD_REVIEW,
    INTERRUPTED_EXECUTION_FAILURE_CODE,
    TASK_STATUS_RUNNING,
    TASK_STATUS_WAITING_FIELD_REVIEW,
)
from app.graphs.audit_graph import collect_agent_summary
from app.graphs.audit_rule_runner import extraction_group, run_rule_evaluation
from app.modules.audit.execution.crud import AuditExecutionCRUD
from app.modules.audit.execution.model import NodeExecution
from app.modules.audit.execution.service.execution_scope import latest_execution
from app.modules.audit.execution.service.flow.context import (
    execution_attempt_no,
    graph_load_evaluation,
    graph_prepare_materials,
)
from app.modules.audit.execution.service.flow.errors import fail_running_execution
from app.modules.audit.execution.service.flow.execution import running_execution
from app.modules.audit.execution.service.flow.lifecycle import (
    graph_advance_workflow,
    graph_finalize_node_execution,
    graph_pause_scheduling,
    graph_save_node_result,
)
from app.modules.audit.execution.service.flow.plan import (
    ReviewBoundary,
    assert_results_match_plan,
    evaluation_plan,
    field_review_boundary,
    rerun_split,
)
from app.modules.audit.execution.service.flow.runner import audit_runner
from app.modules.audit.field.service.field_review import AuditFieldReviewService
from app.modules.audit.rule.crud import AuditRuleCRUD
from app.modules.audit.rule.model import RuleResult
from app.modules.audit.shared import _task_manifest, pending_rerun_keys
from app.modules.audit.task.model import AuditTask
from app.modules.audit.task.service.lookup import AuditTaskLookupService

logger = logging.getLogger(__name__)

# 允许原地续跑的失败原因。
#
# 判断依据是「已提交的规则结果是否仍然可信」：每条规则的结果都是独立保存、独立提交的，
# 因此发生在规则处理之外的失败（服务重启、渠道故障、程序异常、存储暂时不可达）不会改写
# 已提交部分，可以沿用原批次只跑未完成项。输入或配置类业务错误（缺材料、缺执行方式、
# 批次对不上等）说明当前输入不满足执行条件，必须先处理原因，不在这里放行。
RESUMABLE_FAILURE_CODES = frozenset({
    INTERRUPTED_EXECUTION_FAILURE_CODE,
    AUDIT_LLM_CHANNEL_UNAVAILABLE,
    "AUDIT_GRAPH_EXECUTION_FAILED",
    "AUDIT_GRAPH_RUNTIME_FAILED",
    "AUDIT_NODE_UNEXPECTED_ERROR",
    "STORAGE_UNAVAILABLE",
})


@dataclass(frozen=True)
class ResumePlan:
    """续跑范围：四类互斥，合计就是当前计划里的全部规则。

    `deferred` 是「本轮不允许执行、留待字段核对确认之后」的规则：它们既没有结果，
    也不是本次未完成项，报成「补跑」会让用户以为确认后不会再跑。`closed` 是字段核对
    边界已经结束、本轮同样不执行但也无需再等的抽取段规则，计入保留而不是等待核对。
    """

    unfinished: tuple[str, ...]
    affected: tuple[str, ...]
    preserved: tuple[str, ...]
    deferred: tuple[str, ...] = ()
    closed: tuple[str, ...] = ()

    @property
    def rerun_keys(self) -> set[str]:
        return {*self.unfinished, *self.affected}

    @property
    def scope(self) -> set[str]:
        """本轮允许执行的规则键：保留项与重跑项，不含核对后才执行的项。"""
        return {*self.preserved, *self.unfinished, *self.affected}

    def summary(self) -> dict[str, int]:
        return {
            # 已结束的抽取段不会再跑，与已有结果同属「保留」。
            "preserved": len(self.preserved) + len(self.closed),
            "pending": len(self.unfinished),
            "dependent": len(self.affected),
            "waiting_review": len(self.deferred),
        }



class AuditExecutionResumeService:
    """本实体的服务方法；db 由构造时注入。"""

    def __init__(self, db: Session | None = None) -> None:
        self.db = db

    def _saved_results(self, execution: NodeExecution) -> dict[str, RuleResult]:
        """该批次已提交的逐条结果，按规则键索引。"""
        return {
            item.rule_key: item
            for item in AuditRuleCRUD(self.db).results_for_execution(execution.id)
        }


    def resume_blocked_reason(self, task: AuditTask, execution: NodeExecution | None) -> str | None:
        """不能续跑的原因；可以续跑返回 None。

        只按错误原因与已提交状态判断，不因为「失败过」或「结论不理想」拒绝，也不对所有
        FAILED 一律放行。
        """
        if execution is None:
            return "当前节点还没有执行批次，无法继续执行"
        if task.status not in {"BLOCKED", "FAILED", TASK_STATUS_WAITING_FIELD_REVIEW}:
            return f"任务当前状态为 {task.status}，不在可恢复状态"
        if execution.status in {"SUCCEEDED", "BLOCKED", EXECUTION_STATUS_WAITING_FIELD_REVIEW}:
            return None
        if execution.status != "FAILED":
            return f"执行批次状态为 {execution.status}，不能继续执行"
        if execution.failure_code not in RESUMABLE_FAILURE_CODES:
            reason = execution.failure_message or execution.failure_code or "未记录原因"
            return f"上次失败需要先处理：{reason}"
        return None


    def resumable_execution(self, task: AuditTask, execution: NodeExecution | None) -> NodeExecution | None:
        """该批次是否可继续；不可继续返回 None。"""
        return execution if self.resume_blocked_reason(task, execution) is None else None


    def resume_plan(self, task: AuditTask, execution: NodeExecution, *,
        plan: list[dict[str, Any]] | None = None,
        allowed_keys: set[str] | None = None,
        deferred_keys: set[str] | None = None,
    ) -> ResumePlan:
        """当前批次的续跑范围；页面预览、恢复资格与真实续跑共用这一份计算。

        范围由已提交结果与持久化的待重跑清单共同决定：清单里的规则即使已有结论，也说明
        上一轮已判定必须重算，不能再算作可保留结果。`allowed_keys` 是字段核对边界给出的
        本轮允许范围；`deferred_keys` 是其中「确认后才执行」的那一段，范围外只有这一段
        记入 `deferred`，边界外已经结束的抽取段记入 `closed`。
        """
        plan = evaluation_plan(task, task.current_node) if plan is None else plan
        plan_keys = {entry["rule_key"] for entry in plan}
        scope = plan_keys if allowed_keys is None else (plan_keys & allowed_keys)
        pending = pending_rerun_keys(execution)
        unknown = sorted(pending - plan_keys)
        if unknown:
            raise BusinessError(
                "AUDIT_RESUME_PLAN_MISMATCH",
                "续跑清单包含不在当前执行计划中的规则，无法确定重跑范围",
                status_code=500,
                details={"rule_keys": unknown},
            )
        saved = self._saved_results( execution)
        # 未完成与依赖传播只在允许范围内计算：字段核对已关闭时，抽取段的旧结局不再
        # 代表本轮输入（人工核对已经接手），不能再用它把核对后的下游判成受影响重跑。
        scoped_plan = [entry for entry in plan if entry["rule_key"] in scope]
        unfinished, affected = rerun_split(
            scoped_plan, saved, all_rules=_task_manifest(task)["rules"],
        )
        dependent = (affected | (pending - unfinished - affected)) & scope
        preserved = scope - unfinished - dependent
        outside = plan_keys - scope
        deferred = outside if deferred_keys is None else (outside & deferred_keys)
        return ResumePlan(
            unfinished=tuple(sorted(unfinished)),
            affected=tuple(sorted(dependent)),
            preserved=tuple(sorted(preserved)),
            deferred=tuple(sorted(deferred)),
            closed=tuple(sorted(outside - deferred)),
        )


    def review_scope(self, task: AuditTask, execution: NodeExecution,
        plan: list[dict[str, Any]] | None = None,
        pairs: dict[tuple[str, str], Any] | None = None,
    ) -> tuple[ReviewBoundary, bool, set[str], list[dict[str, Any]]]:
        """(字段核对边界, 当前字段版本是否已确认, 本轮允许执行范围, 执行计划)。

        任务详情、受理校验与真实续跑都从这里取，避免各自判断「现在该不该再核对一次」。
        """
        entries = evaluation_plan(task, task.current_node) if plan is None else plan
        boundary = field_review_boundary(entries, all_rules=_task_manifest(task)["rules"])
        confirmed = AuditFieldReviewService(self.db).confirmation_is_current( task, execution, pairs)
        allowed = boundary.phase_keys(
            confirmed=confirmed, extraction_closed=AuditFieldReviewService().extraction_closed(execution),
        )
        return boundary, confirmed, allowed, entries


    def resume_state(self, task: AuditTask, execution: NodeExecution | None, *,
        plan: list[dict[str, Any]] | None = None,
        pairs: dict[tuple[str, str], Any] | None = None,
    ) -> dict[str, Any]:
        """统一的恢复判断：能否续跑、不能的原因、续跑范围与渠道故障信息。

        任务详情、受理接口与后台执行都以这一份为准，各层不再各自推导重跑范围。
        规则结果已全部提交、只差收尾的批次同样可续跑：续跑会补齐汇总与推进，不重新调用模型。
        `plan` 允许调用方传入已经算好的执行计划，避免同一请求重复编译整份配置；
        `pairs` 同理，让页面预览与字段核对状态共用一次当前字段事实配对。
        """
        reason = self.resume_blocked_reason(task, execution)
        if execution is None or reason is not None:
            return {
                "execution_id": None, "blocked_reason": reason, "plan": None,
                "channel_failure": None, "field_review": {"required": False, "confirmed": False},
            }
        failure_json = (execution.result_json or {}).get("channel_failure")
        channel = failure_json if isinstance(failure_json, dict) else None
        if execution.status == "SUCCEEDED":
            # 业务结果已提交、只差图推进：没有需要重跑的规则。
            return {
                "execution_id": execution.id, "blocked_reason": None,
                "plan": ResumePlan((), (), ()).summary(), "channel_failure": channel,
                "field_review": {"required": False, "confirmed": False},
            }
        try:
            boundary, confirmed, allowed, entries = self.review_scope( task, execution, plan, pairs)
            plan = self.resume_plan(
                 task, execution, plan=entries, allowed_keys=allowed,
                deferred_keys=set(boundary.after),
            )
        except BusinessError as exc:
            if exc.code not in {"AUDIT_RESUME_PLAN_MISMATCH", "AUDIT_FIELD_REVIEW_DEPENDENCY_CONFLICT"}:
                raise
            # 清单与计划对不上时不能猜一个范围，明确拒绝并交给「全部重新执行」；
            # 抽取依赖了核对后的业务规则时，二者都不是自动能修的，原样说明原因。
            fallback = "；请使用「全部重新执行」" if exc.code == "AUDIT_RESUME_PLAN_MISMATCH" else ""
            return {
                "execution_id": None,
                "blocked_reason": f"{exc.message}{fallback}",
                "plan": None,
                "channel_failure": channel,
                "field_review": {"required": False, "confirmed": False},
            }
        return {
            "execution_id": execution.id,
            "blocked_reason": None,
            "plan": plan.summary(),
            "channel_failure": channel,
            "field_review": {
                "required": boundary.required,
                "confirmed": confirmed,
                "extraction_rule_count": len(boundary.extraction),
                "after_review_rule_count": len(boundary.after),
            },
        }


    def resumable_execution_of_task(self, task: AuditTask) -> NodeExecution | None:
        """当前节点最近一次可恢复的执行批次；没有则为 None。"""
        execution = latest_execution(self.db, task.id, node_code=task.current_node)
        return self.resumable_execution(task, execution)


    def resume_state_of_task(self, task: AuditTask) -> dict[str, Any]:
        """当前节点最近一次批次的恢复判断。"""
        return self.resume_state( task, latest_execution(self.db, task.id, node_code=task.current_node))


    def _saved_outcome(self, result: RuleResult, counted_batches: set[str]) -> dict[str, Any]:
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


    def _save_pending_rerun_keys(self, execution: NodeExecution, keys: set[str]) -> None:
        execution.result_json = {**(execution.result_json or {}), "pending_rerun_keys": sorted(keys)}
        AuditExecutionCRUD(self.db).commit()


    def resume_interrupted_execution(self, task_no: str, *, execution_id: int) -> dict[str, Any]:
        """继续一个未完成批次：只重跑未完成规则及其传递下游，然后照常收尾与推进。

        适用于服务中断留下的批次、模型渠道故障暂停的批次，以及规则失败后仍可继续的 BLOCKED
        批次。已提交且不受本次重跑影响的规则原样保留；上游被重跑时，依赖它的下游一并重跑。
        """
        task = AuditTaskLookupService(self.db).task_or_404( task_no, lock=True)
        execution = AuditExecutionCRUD(self.db).by_id(execution_id)
        if execution is None or execution.audit_task_id != task.id or execution.node_code != task.current_node:
            raise BusinessError(
                "AUDIT_EXECUTION_MISSING", "恢复目标执行批次不属于任务当前节点", status_code=409,
                details={"task_no": task_no, "execution_id": execution_id},
            )
        reason = self.resume_blocked_reason(task, execution)
        if reason is not None:
            raise BusinessError(
                "AUDIT_EXECUTION_NOT_RESUMABLE", reason, status_code=409,
                details={"execution_id": execution.id, "status": execution.status,
                         "failure_code": execution.failure_code},
            )
        if running_execution(self.db, task) is not None:
            raise BusinessError("AUDIT_NODE_RUNNING", "当前任务已有执行中的节点，不能并发恢复", status_code=409)

        # 已成功收尾的批次只差图推进；业务结果与逐条规则都已提交，不重跑规则。
        advance_only = execution.status == "SUCCEEDED"
        # 仅推进时保留成功终态与完成时间；只有未完成的批次重新进入执行中。
        if not advance_only:
            # 每次继续执行都是一次新的模型请求轮次，幂等键不能沿用上一轮。
            previous_failure = execution.result_json or {}
            result_json = {**previous_failure, "attempt_no": execution_attempt_no(execution) + 1}
            # 上一轮的渠道故障只属于上一轮：新一轮的结论要按本次真实结果判定，
            # 旧的暂停原因转到追溯字段，不能继续影响收尾判断。
            previous_channel_failure = result_json.pop("channel_failure", None)
            if isinstance(previous_channel_failure, dict):
                result_json["recovered_from_channel_failure"] = previous_channel_failure
            execution.result_json = result_json
            if execution.status == "FAILED" and execution.failure_code == INTERRUPTED_EXECUTION_FAILURE_CODE:
                # 批次随后置 RUNNING 并清空失败码，收尾阶段已看不到中断状态；
                # 先把中断事实写进结果，续跑成功后仍能追溯这次执行曾被服务重启打断。
                execution.result_json = {
                    **execution.result_json,
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
        AuditExecutionCRUD(self.db).commit()

        try:
            if advance_only:
                pending = pending_rerun_keys(execution)
                if pending:
                    logger.warning(
                        "已成功批次仍记录待重跑规则，按已收尾结果推进 task_no=%s execution_id=%s rule_keys=%s",
                        task_no, execution.id, sorted(pending),
                    )
                advanced = graph_advance_workflow(self.db, task_no, execution.id)
                graph_save_node_result(self.db, task_no)
                return advanced["last_execution"]
            # 重跑范围与详情页预览出自同一个函数：待重跑清单、未完成项与依赖下游不再各算一份。
            # 字段核对边界同样取自服务端：未确认时本轮只跑抽取段，确认后才是完整计划。
            loaded = graph_load_evaluation(self.db, task_no, task.current_node, execution.id)
            plan = loaded["evaluation_plan"]
            review = loaded["field_review"]
            resume = self.resume_plan(
                 task, execution, plan=plan,
                allowed_keys=set(review["allowed_rule_keys"]),
                deferred_keys=set(review.get("after_rule_keys") or ()),
            )
            rerun_keys = resume.rerun_keys
            covered = resume.scope
            if rerun_keys:
                # 没有重跑项时不重复材料前置处理：规则已经跑过，本次只补收尾与推进。
                graph_prepare_materials(self.db, task_no, execution.id)
            saved = self._saved_results( execution)
            assert_results_match_plan(plan, saved)
            # 先落库再逐条执行：上游成功后进程再中断，下游的重算意图仍然保留。
            self._save_pending_rerun_keys( execution, rerun_keys)
            outcomes: dict[str, dict[str, Any]] = {}
            counted_batches: set[str] = set()
            channel_failure: dict[str, Any] | None = None
            # 本轮允许执行的计划项：核对后才执行的规则不参与，也不能按「已保存结果」取。
            scoped = [entry for entry in plan if entry["rule_key"] in covered]
            # 与主执行路径同一套调度：同一材料上相邻的无输入抽取合并为一次模型调用。
            index = 0
            while index < len(scoped):
                group = extraction_group(scoped, index)
                index += len(group)
                pending = [entry for entry in group if entry["rule_key"] in rerun_keys]
                for entry in group:
                    if entry["rule_key"] not in rerun_keys:
                        outcomes[entry["rule_key"]] = self._saved_outcome(saved[entry["rule_key"]], counted_batches)
                if not pending:
                    continue
                produced: list[dict[str, Any]] = []
                if len(pending) > 1:
                    produced = list(audit_runner.graph_execute_extraction_batch(
                        self.db, task_no, task.current_node, execution.id, pending,
                    ))
                    # 每个字段的待重跑标记在该字段保存的同一事务里清除，这里不再统一清理。
                else:
                    for entry in pending:
                        produced.append(run_rule_evaluation(
                            self.db, audit_runner, task_no=task_no, node_code=task.current_node,
                            execution_id=execution.id, rule_cursor=entry["cursor"], planned_rule=entry["rule"],
                        ))
                for outcome in produced:
                    outcomes[outcome["rule_key"]] = outcome
                failure = next(
                    (item["channel_failure"] for item in produced if item.get("channel_failure")), None,
                )
                if failure is not None:
                    # 渠道故障：已提交的结果保留，本次续跑到此为止，批次仍可在恢复后继续。
                    channel_failure = failure
                    graph_pause_scheduling(self.db, task_no, execution.id, failure=failure)
                    break
            ordered = [outcomes[entry["rule_key"]] for entry in scoped if entry["rule_key"] in outcomes]
            summary = collect_agent_summary(
                covered, ordered, sum(item["loop_count"] for item in ordered),
            )
            if review["required"] and not review["confirmed"] and channel_failure is None:
                # 抽取段已跑完、字段尚未确认：批次停在核对等待点，后续规则本轮不执行。
                summary["field_review_paused"] = True
            graph_finalize_node_execution(self.db, task_no, execution.id, "AGENT", None, summary)
            advanced = graph_advance_workflow(self.db, task_no, execution.id)
            graph_save_node_result(self.db, task_no)
            result = advanced["last_execution"]
            if channel_failure is not None or result.get("failure_code") == AUDIT_LLM_CHANNEL_UNAVAILABLE:
                # 暂停不是成功：后台操作按真实失败结束，页面据此提示继续执行。
                raise BusinessError(
                    AUDIT_LLM_CHANNEL_UNAVAILABLE,
                    str(result.get("failure_message") or "远程大模型渠道不可用，本次执行已暂停"),
                    status_code=502,
                )
            return result
        except BusinessError as exc:
            fail_running_execution(
                self.db, task_no, execution_id=execution.id, code=exc.code,
                message=str(exc.details.get("reason") or exc.message),
            )
            raise
        except Exception as exc:  # noqa: BLE001 - 未处理异常必须显式落库
            logger.exception("恢复审核执行发生未处理异常", extra={"task_no": task_no, "execution_id": execution.id})
            fail_running_execution(
                self.db, task_no, execution_id=execution.id,
                code="AUDIT_GRAPH_RUNTIME_FAILED", message=exception_summary(exc),
            )
            raise BusinessError(
                "AUDIT_GRAPH_RUNTIME_FAILED", "恢复执行失败，具体原因见执行记录和服务日志",
                status_code=500, details={"reason": exception_summary(exc)},
            ) from exc
