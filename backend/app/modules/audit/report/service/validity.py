"""报告正文是否仍代表当前执行的判定，以及判定所需结果信号的批量读取。

批次与续跑口径在 `services.audit.execution_scope`；报告只在此基础上增加输入版本与
评估标识两项检查，不另写一套有效性判断。
"""

from dataclasses import dataclass
from typing import Iterable

from sqlalchemy.orm import Session

from app.constants.status import LOOP_DONE
from app.domain.execution_scope import (
    RESULT_STALE_EXECUTION,
    RESULT_STALE_PENDING_RERUN,
    result_state,
)
from app.modules.audit.report.crud import AuditReportCRUD


@dataclass(frozen=True)
class ReportResultSignal:
    """报告规则在本批次的结果摘要；只保留判定有效性需要的三列。"""

    status: str
    loop_status: str | None
    evaluation_id: str | None



class AuditReportValidityService:
    """本实体的服务方法；db 由构造时注入。"""

    def __init__(self, db: Session | None = None) -> None:
        self.db = db

    def result_signals(self, pairs: Iterable[tuple[int | None, str | None]],
    ) -> dict[tuple[int, str], ReportResultSignal]:
        """按（执行批次, 规则键）一次取回结果信号；报告自己上一轮的结果也在这里。"""
        wanted = {
            (int(execution_id), str(rule_key))
            for execution_id, rule_key in pairs
            if execution_id is not None and rule_key
        }
        if not wanted:
            return {}
        rows = AuditReportCRUD(self.db).result_signal_rows(wanted)
        return {
            (int(row.node_execution_id), str(row.rule_key)): ReportResultSignal(
                status=str(row.status),
                loop_status=row.loop_status,
                evaluation_id=row.evaluation_id,
            )
            for row in rows
        }


    def report_current_reason(self, *,
        report_input_revision: int | None,
        input_revision: int | None,
        execution_id: int | None,
        current_ids: set[int],
        rule_key: str | None,
        pending_rerun_keys: set[str],
        snapshot_evaluation_id: str | None,
        result: ReportResultSignal | None,
    ) -> str | None:
        """报告正文是否仍代表本次执行；返回失效原因，有效时为 None。

        `current_ids` 来自 `execution_scope.current_execution_ids`，与风险、任务详情共用
        同一份当前批次。
        """
        if report_input_revision is not None and report_input_revision != input_revision:
            return "报告对应的输入版本已变更"
        state = result_state(
            execution_id=execution_id,
            rule_key=rule_key,
            current_ids=current_ids,
            pending_rerun_keys=pending_rerun_keys,
        )
        if state.reason == RESULT_STALE_EXECUTION:
            return "报告对应的执行批次已失效"
        if state.reason == RESULT_STALE_PENDING_RERUN:
            return "报告执行项已列入重跑，正文为上一次成功生成的内容"
        if result is None:
            return "报告执行项没有本批次的执行结果"
        if (result.loop_status or LOOP_DONE) != LOOP_DONE or result.status == "NOT_EXECUTED":
            return "报告执行项本次未形成结论，正文为上一次成功生成的内容"
        if snapshot_evaluation_id != result.evaluation_id:
            return "报告执行项本次未生成报告正文，正文为上一次成功生成的内容"
        return None
