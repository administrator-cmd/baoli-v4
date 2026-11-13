"""执行记录管理列表与详情查询：SQL 在 crud。

列表统计只按本批次真实产生的规则结果与未执行清单计算执行进度；模型请求轮次按
evaluation_id 去重，批量抽取共享的请求只计一次。展示分类只依赖状态码与结构化诊断，
不从中文错误文本猜原因。
"""

from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from app.common.pagination import PageResult, page_result
from app.constants.status import AUDIT_LLM_CHANNEL_UNAVAILABLE, LOOP_BLOCKED
from app.domain.field_review import reviewed_extraction_keys
from app.modules.audit.execution.crud import AuditExecutionCRUD
from app.modules.audit.execution.service.execution_history import AuditExecutionHistoryService
from app.modules.audit.execution.service.flow.lifecycle import incomplete_failure_message
from app.modules.audit.rule.crud import AuditRuleCRUD
from app.modules.audit.rule.model import RuleResult

_EXECUTION_INCOMPLETE_CODE = "AUDIT_RULE_EXECUTION_INCOMPLETE"
_EXECUTION_INTERRUPTED_CODE = "AUDIT_EXECUTION_INTERRUPTED"
_GRAPH_FAILED_CODE = "AUDIT_GRAPH_EXECUTION_FAILED"
_STORAGE_UNAVAILABLE_CODE = "STORAGE_UNAVAILABLE"
_BLOCKED_EXECUTION_STATUSES = frozenset({"BLOCKED", "FAILED"})


class AuditExecutionExecutionsService:
    """本实体的服务方法；db 由构造时注入。"""

    def __init__(self, db: Session | None = None) -> None:
        self.db = db

    def _failure_message(self, failure_code: str | None, result_json: dict[str, Any] | None, stored: str | None) -> str | None:
        """未完成批次按逐条结果重新汇总原因，旧记录也能显示清楚。"""
        if failure_code == _EXECUTION_INCOMPLETE_CODE:
            return incomplete_failure_message(result_json) or stored
        return stored

    def _reviewed_extraction_keys(self, result_json: dict[str, Any]) -> set[str]:
        """字段核对点已经交给人工接手的抽取规则键；它们不再属于当前执行阻断。"""
        return reviewed_extraction_keys(result_json)


    def _model_turn_stats(self, results: list[RuleResult]) -> dict[str, Any]:
        """模型请求轮次：同一 evaluation_id 的字段共享请求，只计一次。

        批量抽取的修正轮也属于同一次评估编号，不能按字段重复累加。同一评估编号下出现
        不同轮次说明保存结果彼此不一致，此时宁可标记统计不可用，也不挑一个数字充数。
        """
        groups: dict[str, list[int]] = {}
        unattributed: list[int] = []
        available = True
        for result in results:
            diagnostics = result.result_json if isinstance(result.result_json, dict) else {}
            evaluation_id = diagnostics.get("evaluation_id")
            turns = int(result.turn_count or 0)
            if isinstance(evaluation_id, str) and evaluation_id.strip():
                groups.setdefault(evaluation_id, []).append(turns)
            elif diagnostics.get("extraction_batch") is not None:
                # 批量结果缺少评估编号时无法判断哪些字段共享同一次请求。
                available = False
            else:
                unattributed.append(turns)
        total = sum(unattributed)
        for values in groups.values():
            unique = set(values)
            if len(unique) != 1:
                available = False
                continue
            total += unique.pop()
        if not available:
            return {"count": None, "available": False}
        return {"count": total, "available": True}

    def _block_reason(
        self,
        *,
        displayed_status: str,
        invalidated: bool,
        failure_code: str | None,
        failure_message: str | None,
        current_blocked: int,
        not_executed: int,
    ) -> dict[str, Any] | None:
        """列表用的简短阻断原因；优先按错误码生成，无法归类时保留原始原因。"""
        if invalidated or displayed_status not in _BLOCKED_EXECUTION_STATUSES:
            return None
        text: str | None = None
        if failure_code == AUDIT_LLM_CHANNEL_UNAVAILABLE:
            text = "模型请求渠道不可用，已暂停后续调度"
        elif failure_code == _EXECUTION_INCOMPLETE_CODE:
            pending = current_blocked + not_executed
            text = f"{pending} 项规则未完成执行" if pending else "有规则未完成执行"
        elif failure_code == _EXECUTION_INTERRUPTED_CODE:
            text = "上次运行中断，批次未完成"
        elif failure_code == _GRAPH_FAILED_CODE:
            text = "审核流程执行发生错误，批次未完成"
        elif failure_code == _STORAGE_UNAVAILABLE_CODE:
            text = "数据库暂时不可达，批次未完成"
        if not text:
            text = failure_message or ("执行失败" if displayed_status == "FAILED" else "执行阻断")
        return {"code": failure_code, "text": text}

    def _execution_statistics(
        self,
        *,
        results: list[RuleResult],
        result_json: dict[str, Any] | None,
        displayed_status: str,
        invalidated: bool,
        failure_code: str | None,
        failure_message: str | None,
    ) -> dict[str, Any]:
        """列表共用的执行进度、当前阻断与模型请求轮次。"""
        result_json = result_json if isinstance(result_json, dict) else {}
        reviewed = self._reviewed_extraction_keys(result_json)
        result_keys = {result.rule_key for result in results}
        missing_raw = result_json.get("missing_rule_keys")
        missing_keys = [
            str(key) for key in (missing_raw if isinstance(missing_raw, list) else [])
            if str(key) not in reviewed and str(key) not in result_keys
        ]
        blocking_active = not invalidated and displayed_status in _BLOCKED_EXECUTION_STATUSES
        current_blocked = 0
        if blocking_active:
            for result in results:
                # 旧记录没有循环状态时，NOT_EXECUTED 不能显示成「完成」。
                loop_status = result.loop_status or (
                    "NOT_EXECUTED" if result.status == "NOT_EXECUTED" else "DONE"
                )
                if loop_status == LOOP_BLOCKED and result.rule_key not in reviewed:
                    current_blocked += 1
        progress = {
            "executed": len(results),
            "blocked": sum(1 for result in results if (result.loop_status or "DONE") == LOOP_BLOCKED),
            "not_executed": len(missing_keys),
        }
        return {
            "progress": progress,
            "model_turns": self._model_turn_stats(results),
            "current_blocked": current_blocked,
            "block_reason": self._block_reason(
                displayed_status=displayed_status,
                invalidated=invalidated,
                failure_code=failure_code,
                failure_message=failure_message,
                current_blocked=current_blocked,
                not_executed=progress["not_executed"],
            ),
        }

    def list_executions_page(
        self,
        *,
        task_no: str | None,
        node_code: str | None,
        status: str | None,
        started_from: datetime | None,
        started_to: datetime | None,
        page: int,
        page_size: int,
    ) -> PageResult[dict[str, Any]]:
        """节点执行记录分页列表（跨任务）。"""
        rows, total = AuditExecutionCRUD(self.db).page_rows(
            task_no=task_no,
            node_code=node_code,
            status=status,
            started_from=started_from,
            started_to=started_to,
            page=page,
            page_size=page_size,
        )
        results_by_execution: dict[int, list[RuleResult]] = {}
        for result in AuditRuleCRUD(self.db).results_for_executions([row.id for row in rows]):
            results_by_execution.setdefault(result.node_execution_id, []).append(result)
        items = []
        for row in rows:
            duration_ms = None
            if row.started_at and row.finished_at:
                duration_ms = int((row.finished_at - row.started_at).total_seconds() * 1000)
            status_view = AuditExecutionHistoryService().execution_status_view(row.status, row.invalidated_from)
            result_summary = row.result_json if isinstance(row.result_json, dict) else None
            failure_message = self._failure_message(row.failure_code, result_summary, row.failure_message)
            statistics = self._execution_statistics(
                results=results_by_execution.get(row.id, []),
                result_json=result_summary,
                displayed_status=str(status_view["status"]),
                invalidated=bool(status_view["invalidated"]),
                failure_code=row.failure_code,
                failure_message=failure_message,
            )
            items.append(
                {
                    "execution_id": row.id,
                    "task_no": row.task_no,
                    "task_name": row.task_name,
                    "scenario_name": row.scenario_name,
                    "scenario_version_no": row.version_no,
                    "node_code": row.node_code,
                    "node_name": row.node_name or row.node_code,
                    "sequence_no": row.sequence_no,
                    **status_view,
                    "invalidated_at": row.invalidated_at,
                    "invalidation_reason": row.invalidation_reason,
                    "started_at": row.started_at,
                    "finished_at": row.finished_at,
                    "duration_ms": duration_ms,
                    "failure_code": row.failure_code,
                    "failure_message": failure_message,
                    "result_summary": result_summary,
                    "progress": statistics["progress"],
                    "model_turns": statistics["model_turns"],
                    "current_blocked": statistics["current_blocked"],
                    "block_reason": statistics["block_reason"],
                }
            )
        return page_result(items, total, page, page_size)
