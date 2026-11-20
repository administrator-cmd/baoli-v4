"""任务结果的只读视图：报告与风险。

这里只读当前有效批次的数据并如实表达状态，不写库、不重算业务结论：

- 报告正文从 `audit_report.content_md` 读（正文的唯一存放处）；执行过报告节点却没有
  可读正文时如实说明是记录缺失，不显示成「报告尚未生成」。
- 风险命中只统计 ABNORMAL；「没有风险记录」不能写成「无风险」。
- 原文依据随规则 CSV 保存，由任务/风险详情一并返回。"""


from typing import Any

from sqlalchemy.orm import Session

from app.common.exceptions import BusinessError
from app.constants.status import COMPLETED_RESULT_STATUSES, CONCLUSIVE_RESULT_STATUSES
from app.domain.audit.execution_scope import RESULT_STALE_PENDING_RERUN, result_state
from app.domain.configuration.rule_library import is_risk_rule
from app.modules.audit.execution.service.execution_scope import current_execution_ids, pending_rerun_by_execution
from app.modules.audit.field.service import value_store as task_value_service
from app.modules.audit.field.service.evidence import AuditFieldEvidenceService
from app.modules.audit.report.crud import AuditReportCRUD
from app.modules.audit.report.service.validity import AuditReportValidityService
from app.modules.audit.rule.crud import AuditRuleCRUD
from app.modules.audit.shared import _task_manifest
from app.modules.audit.task.service.lookup import AuditTaskLookupService


class AuditReportViewsService:
    """本实体的服务方法；db 由构造时注入。"""

    def __init__(self, db: Session | None = None) -> None:
        self.db = db

    def risks_view(self, task_no: str) -> dict[str, Any]:
        """风险结果只读视图：只统计 ABNORMAL 为命中，未配置 / 未执行 / 无法判断分开表达。"""
        task = AuditTaskLookupService(self.db).task_or_404( task_no, values=True)
        manifest = _task_manifest(task)
        enabled_keys = set(task.task_schema_json["enabled_rule_keys"])
        risk_rules = [
            item
            for item in manifest["rules"]
            if is_risk_rule(item) and item["rule_key"] in enabled_keys
        ]
        risk_configured = bool(risk_rules)
        risk_keys = {item["rule_key"] for item in risk_rules}
        # 只取当前有效执行批次的风险结果：失效批次与旧上传版本不进入当前结论。
        risk_node_codes = {item["node"] for item in risk_rules}
        current_ids = current_execution_ids(self.db, [task.id], node_codes=risk_node_codes)
        results = (
            AuditRuleCRUD(self.db).results_for_task(task.id, current_ids, risk_keys)
            if current_ids and risk_keys
            else []
        )
        # 待重跑的规则写着重跑前的旧结论，不能作为本次判定依据；口径与报告一致。
        pending_by_execution = pending_rerun_by_execution(self.db, current_ids)
        current_results = [
            item for item in results
            if result_state(
                execution_id=item.node_execution_id,
                rule_key=item.rule_key,
                current_ids=current_ids,
                pending_rerun_keys=pending_by_execution.get(item.node_execution_id, set()),
            ).current
        ]
        status_counts: dict[str, int] = {}
        for item in current_results:
            status_counts[item.status] = status_counts.get(item.status, 0) + 1
        # 本轮没有结论的风险规则（未执行与待重跑）都计入未执行：状态词表里没有「待重跑」这一档，
        # 单独造一个伪状态会让页面统计与逐条状态对不上。
        unexecuted_count = len(risk_keys - {item.rule_key for item in current_results})
        if unexecuted_count:
            status_counts["NOT_EXECUTED"] = status_counts.get("NOT_EXECUTED", 0) + unexecuted_count
        # 风险等级直接读索引列；业务结论（含风险上下文）在任务 CSV。
        risk_level_by_key = {item["rule_key"]: item.get("risk_level_value") or item.get("parameters", {}).get("risk_level") for item in risk_rules}
        source_sheet_by_key = {item["rule_key"]: (item.get("origin") or {}).get("sheet_name") for item in risk_rules}
        # 命中只统计 ABNORMAL，不能用结果总数冒充命中数。
        hit_count = sum(1 for item in current_results if item.status == "ABNORMAL")
        # 同一请求内复用证据链展开结果。
        memo = AuditFieldEvidenceService().evidence_memo()
        risk_conclusion_available = (
            risk_configured
            and len(current_results) == len(risk_rules)
            and all(item.status in CONCLUSIVE_RESULT_STATUSES for item in current_results)
        )
        return {
            "task_no": task.task_no,
            "risk_configured": risk_configured,
            "risk_executed": bool(current_results),
            "risk_results_stale": risk_configured and not current_results
            and AuditRuleCRUD(self.db).has_produced_result(task.id, risk_keys),
            "risk_conclusion_available": risk_conclusion_available,
            "hit_count": hit_count,
            "status_counts": status_counts,
            "items": [
                {
                    "rule_key": item.rule_key,
                    "rule_code": item.rule_code if item.rule_code != item.rule_key else None,
                    "name": item.rule_name,
                    "status": item.status,
                    "message": item.message,
                    "risk_level": item.risk_level or risk_level_by_key.get(item.rule_key),
                    "source_sheet": source_sheet_by_key.get(item.rule_key),
                    "details": {
                        **task_value_service.AuditFieldValueStoreService(self.db).value_of(task, task_value_service.RECORD_RULE_RESULT, item.id),
                        # 状态与等级只存数据库，响应里仍按原契约返回。
                        "status": item.status,
                        "message": item.message,
                        "risk_level": item.risk_level or risk_level_by_key.get(item.rule_key),
                    },
                    "evidence": AuditFieldEvidenceService(self.db).evidence_source_views( task, AuditFieldEvidenceService(self.db).rule_evidence( task, item.id, memo)),
                }
                for item in current_results
            ],
        }


    def report_view(self, task_no: str, report_no: str | None = None) -> dict[str, Any]:
        """读取真实保存的报告，不要求固定报告阶段或节点结果格式。"""
        task = AuditTaskLookupService(self.db).task_or_404( task_no, values=True)
        scope_signals = AuditReportValidityService().scope_signals(
            _task_manifest(task), set(task.task_schema_json["enabled_rule_keys"]), None,
        )
        reports = AuditReportCRUD(self.db).reports_for_task(task.id)
        # 判定数据一次取回：逐份报告查执行与结果会让报告页随份数线性退化。
        current_ids = current_execution_ids(self.db, [task.id])
        # 报告行与 CSV 恢复项都要判断是否在重跑清单里，一次取回两组执行批次的清单。
        pending_by_execution = pending_rerun_by_execution(
            self.db,
            {item.node_execution_id for item in reports if item.node_execution_id} | current_ids,
        )
        signals = AuditReportValidityService(self.db).result_signals(
             [(item.node_execution_id, (item.snapshot_json or {}).get("rule_key")) for item in reports],
        )

        def report_reason(item):
            snapshot = item.snapshot_json or {}
            rule_key = snapshot.get("rule_key")
            return AuditReportValidityService().report_current_reason(
                report_input_revision=item.input_revision,
                input_revision=task.input_revision,
                execution_id=item.node_execution_id,
                current_ids=current_ids,
                rule_key=rule_key,
                pending_rerun_keys=pending_by_execution.get(item.node_execution_id, set()),
                snapshot_evaluation_id=snapshot.get("evaluation_id"),
                result=signals.get((item.node_execution_id, rule_key)),
            )

        reasons_by_id = {item.id: report_reason(item) for item in reports}

        def is_current(item):
            return reasons_by_id[item.id] is None
        # 每个报告定义保留最新版本供选择；最新当前报告优先，历史报告明确标识。
        latest_by_rule = {}
        for item in reports:
            latest_by_rule.setdefault((item.snapshot_json or {}).get("rule_key") or item.report_no, item)
        choices = [{
            "report_no": item.report_no, "report_name": item.report_name, "current": is_current(item),
            # 每份报告的生成、覆盖与缺项分别给出，不用一个笼统状态概括。
            "report_generated": bool(item.content_md),
            "scope_execution_complete": bool(item.scope_execution_complete),
            "unresolved_count": len((item.reasons_json or {}).get("unresolved") or []),
        } for item in latest_by_rule.values()]
        report = next((item for item in reports if item.report_no == report_no), None) if report_no else next((item for item in reports if is_current(item)), reports[0] if reports else None)
        if report is None:
            report_keys = {r["rule_key"] for r in _task_manifest(task)["rules"] if r.get("kind") == "REPORT" and r["rule_key"] in task.task_schema_json["enabled_rule_keys"]}
            results = AuditRuleCRUD(self.db).results_for_task(task.id, current_ids, report_keys)
            all_current_results = AuditRuleCRUD(self.db).results_for_task(task.id, current_ids)
            # 显式转换的历史结果把旧 verdict.report 单独展开为 report，
            # 但没有建立 AuditReport。只读恢复正文，不伪造数据库状态；下一次报告节点执行
            # 会按新逻辑正式写入 AuditReport。
            recovered = []
            for result in results:
                payload = task_value_service.AuditFieldValueStoreService(self.db).value_of(task, task_value_service.RECORD_RULE_RESULT, result.id)
                report_data = payload.get("report")
                if isinstance(report_data, dict) and isinstance(report_data.get("content"), str) and report_data["content"].strip():
                    rule = next((item for item in _task_manifest(task)["rules"] if item.get("rule_key") == result.rule_key), {})
                    recovered.append({"rule_result": result, "report": report_data, "rule": rule})
            if recovered:
                # 报告只依赖非报告项；报告自己上一轮的结果不是本轮依据（与 save_outputs 同口径）。
                dependency_results = [item for item in all_current_results if item.rule_key not in report_keys]
                dependency_keys = {item.rule_key for item in dependency_results}
                complete = (set(task.task_schema_json["enabled_rule_keys"]) - report_keys) <= dependency_keys and all(item.status != "NOT_EXECUTED" for item in dependency_results)
                # 待重跑的规则写着重跑前的正文，恢复时与 AuditReport 同一口径标记为失效。
                is_pending_rerun = {
                    item["rule_result"].id: result_state(
                        execution_id=item["rule_result"].node_execution_id,
                        rule_key=item["rule_result"].rule_key,
                        current_ids=current_ids,
                        pending_rerun_keys=pending_by_execution.get(item["rule_result"].node_execution_id, set()),
                    ).reason == RESULT_STALE_PENDING_RERUN
                    for item in recovered
                }

                def recovered_current(item: dict[str, Any]) -> bool:
                    return not is_pending_rerun[item["rule_result"].id]

                choices = [
                    {
                        "report_no": f"CSV-{item['rule_result'].id}",
                        "report_name": item["rule"].get("name") or item["rule_result"].rule_name,
                        "current": recovered_current(item),
                        "report_generated": True,
                        "scope_execution_complete": complete,
                        "unresolved_count": sum(result.status not in COMPLETED_RESULT_STATUSES for result in dependency_results),
                    }
                    for item in recovered
                ]
                if report_no and not any(item["report_no"] == report_no for item in choices):
                    raise BusinessError("REPORT_NOT_FOUND", "该任务中没有对应的报告", status_code=404)
                selected = next((item for item in recovered if report_no == f"CSV-{item['rule_result'].id}"), None)
                if selected is None:
                    selected = next((item for item in recovered if recovered_current(item)), recovered[0])
                stale_reason = (
                    None if recovered_current(selected)
                    else "报告执行项已列入重跑，正文为上一次成功生成的内容"
                )
                report_data = selected["report"]
                risk_rules = [item for item in _task_manifest(task)["rules"] if item.get("kind") != "REPORT" and is_risk_rule(item) and item.get("rule_key") in task.task_schema_json["enabled_rule_keys"]]
                result_by_key = {item.rule_key: item for item in all_current_results}
                risk_available = bool(risk_rules) and all(result_by_key.get(item["rule_key"]) is not None and result_by_key[item["rule_key"]].status in CONCLUSIVE_RESULT_STATUSES for item in risk_rules)
                scope_signals = AuditReportValidityService().scope_signals(
                    _task_manifest(task), set(task.task_schema_json["enabled_rule_keys"]),
                    {"unresolved": [{"rule_key": item.rule_key, "status": item.status, "message": item.message}
                                    for item in dependency_results if item.status not in COMPLETED_RESULT_STATUSES]},
                )
                return {"task_no": task.task_no, "status": "COMPLETE" if complete and stale_reason is None else "INCOMPLETE",
                        "content_type": report_data.get("content_type", "text/markdown"), "content": report_data["content"],
                        "reasons": scope_signals["execution_notes"], "report_generated": True, "scope_execution_complete": complete,
                        "risk_conclusion_available": risk_available, **scope_signals, "results_scope": "CURRENT_RUN",
                        "current": stale_reason is None, "stale_reason": stale_reason,
                        "input_revision": task.input_revision, "snapshot": {"task_input_revision": task.input_revision, "rule_result_ids": [item["rule_result"].id for item in recovered]},
                        "reports": choices, "report_no": f"CSV-{selected['rule_result'].id}", "report_name": selected["rule"].get("name") or selected["rule_result"].rule_name,
                        "conclusion": None, "format_support": [], "business_section_count": len((selected["rule"].get("report_definition") or {}).get("sections") or [])}
            if report_no:
                # 指定编号既不在 audit_report 也不在可恢复的 CSV 结果里：明确报不存在。
                raise BusinessError("REPORT_NOT_FOUND", "该任务中没有对应的报告", status_code=404)
            reasons = [f"{r.rule_name}：已执行，但没有保存可读取的报告正文，请查看执行详情" for r in results]
            return {"task_no": task.task_no, "status": "NOT_GENERATED", "content_type": "text/markdown", "content": None,
                    "reasons": reasons or ["生效报告执行项尚未产生正文" if report_keys else "本任务没有生效的报告执行项"],
                    "report_generated": False, "scope_execution_complete": False, "risk_conclusion_available": False, **scope_signals,
                    "results_scope": "CURRENT_RUN", "current": False, "input_revision": task.input_revision, "snapshot": None,
                    "reports": choices, "report_no": None, "report_name": None,
                    "conclusion": None, "format_support": [], "business_section_count": 0}
        reason = reasons_by_id[report.id]
        current = reason is None
        unresolved = (report.reasons_json or {}).get("unresolved") or []
        scope_signals = AuditReportValidityService().scope_signals(
            _task_manifest(task), set(task.task_schema_json["enabled_rule_keys"]), report.reasons_json,
        )
        rule = next((item for item in _task_manifest(task)["rules"] if item["rule_key"] == (report.snapshot_json or {}).get("rule_key")), None)
        return {"task_no": task.task_no, "status": "COMPLETE" if current and report.scope_execution_complete and not unresolved else "INCOMPLETE",
                "content_type": report.content_type, "content": report.content_md,
                "reasons": scope_signals["execution_notes"] + [item["message"] for item in unresolved if item["status"] == "NOT_GENERATED"],
                # 三个信号分开表达：正文是否存在、生成时范围是否完整、生成时风险是否可判定；
                # 快照是否仍对应当前执行只由 current / stale_reason 表达。
                "report_generated": bool(report.content_md), "scope_execution_complete": report.scope_execution_complete,
                "risk_conclusion_available": report.risk_conclusion_available,
                **scope_signals,
                "results_scope": "CURRENT_RUN", "current": current,
                "stale_reason": reason,
                "input_revision": task.input_revision, "snapshot": report.snapshot_json, "conclusion": None,
                "report_generation": (report.snapshot_json or {}).get("report_generation"),
                "reports": choices, "report_no": report.report_no, "report_name": report.report_name,
                "format_support": (report.reasons_json or {}).get("format_support", [{"format": report.content_type, "supported": True}]),
                "business_section_count": len((rule or {}).get("report_definition", {}).get("sections") or [])}
