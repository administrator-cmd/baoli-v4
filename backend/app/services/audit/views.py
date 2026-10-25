"""任务结果的只读视图：报告与风险。

这里只读当前有效批次的数据并如实表达状态，不写库、不重算业务结论：

- 报告正文从 `audit_report.content_md` 读（正文的唯一存放处）；执行过报告节点却没有
  可读正文时如实说明是记录缺失，不显示成「报告尚未生成」。
- 风险命中只统计 ABNORMAL；「没有风险记录」不能写成「无风险」。
- 原文依据随规则 CSV 保存，由任务/风险详情一并返回。"""


from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ...common.exceptions import BusinessError
from ...constants.status import CONCLUSIVE_RESULT_STATUSES
from ...domain.rule_library import is_risk_rule
from ...entity.reports import AuditReport
from ...entity.tasks import NodeExecution, RuleResult
from . import value_store as task_value_service
from .evidence import evidence_source_views, rule_evidence
from .shared import _task_manifest
from .state import _current_node_executions
from .tasks.lookup import task_or_404


def risks_view(db: Session, task_no: str) -> dict[str, Any]:
    """风险结果只读视图：只统计 ABNORMAL 为命中，未配置 / 未执行 / 无法判断分开表达。"""
    task = task_or_404(db, task_no, values=True)
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
    current_executions = _current_node_executions(db, task)
    risk_node_codes = {item["node"] for item in risk_rules}
    current_execution_ids = {
        item.id for item in current_executions.values() if item.node_code in risk_node_codes
    }
    results = (
        db.scalars(
            select(RuleResult)
            .where(
                RuleResult.audit_task_id == task.id,
                RuleResult.node_execution_id.in_(current_execution_ids),
                RuleResult.rule_key.in_(risk_keys),
            )
            .order_by(RuleResult.id)
        ).all()
        if current_execution_ids and risk_keys
        else []
    )
    status_counts: dict[str, int] = {}
    for item in results:
        status_counts[item.status] = status_counts.get(item.status, 0) + 1
    unexecuted_count = len(risk_keys - {item.rule_key for item in results})
    if unexecuted_count:
        status_counts["NOT_EXECUTED"] = status_counts.get("NOT_EXECUTED", 0) + unexecuted_count
    # 风险等级直接读索引列；业务结论（含风险上下文）在任务 CSV。
    risk_level_by_key = {item["rule_key"]: item.get("risk_level_value") or item.get("parameters", {}).get("risk_level") for item in risk_rules}
    source_sheet_by_key = {item["rule_key"]: (item.get("origin") or {}).get("sheet_name") for item in risk_rules}
    # 命中只统计 ABNORMAL，不能用结果总数冒充命中数。
    hit_count = sum(1 for item in results if item.status == "ABNORMAL")
    risk_conclusion_available = (
        risk_configured
        and len(results) == len(risk_rules)
        and all(item.status in CONCLUSIVE_RESULT_STATUSES for item in results)
    )
    return {
        "task_no": task.task_no,
        "risk_configured": risk_configured,
        "risk_executed": bool(results),
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
                "details": task_value_service.value_of(db, task, task_value_service.RECORD_RULE_RESULT, item.id),
                "evidence": evidence_source_views(db, task, rule_evidence(db, task, item.id)),
            }
            for item in results
        ],
    }

def report_view(db: Session, task_no: str, report_no: str | None = None) -> dict[str, Any]:
    """读取真实保存的报告，不要求固定报告阶段或节点结果格式。"""
    task = task_or_404(db, task_no, values=True)
    reports = list(db.scalars(select(AuditReport).where(AuditReport.audit_task_id == task.id).order_by(AuditReport.id.desc())))
    latest_execution_ids = set(db.scalars(select(func.max(NodeExecution.id)).where(
        NodeExecution.audit_task_id == task.id, NodeExecution.status != "INVALIDATED",
    ).group_by(NodeExecution.node_code)))
    def is_current(item):
        execution = db.get(NodeExecution, item.node_execution_id) if item.node_execution_id else None
        return item.input_revision == task.input_revision and execution is not None and execution.id in latest_execution_ids
    # 每个报告定义保留最新版本供选择；最新当前报告优先，历史报告明确标识。
    latest_by_rule = {}
    for item in reports:
        latest_by_rule.setdefault((item.snapshot_json or {}).get("rule_key") or item.report_no, item)
    choices = [{"report_no": item.report_no, "report_name": item.report_name, "current": is_current(item)} for item in latest_by_rule.values()]
    report = next((item for item in reports if item.report_no == report_no), None) if report_no else next((item for item in reports if is_current(item)), reports[0] if reports else None)
    if report_no and report is None:
        raise BusinessError("REPORT_NOT_FOUND", "该任务中没有对应的报告", status_code=404)
    if report is None:
        report_keys = {r["rule_key"] for r in _task_manifest(task)["rules"] if r.get("kind") == "REPORT" and r["rule_key"] in task.task_schema_json["enabled_rule_keys"]}
        current_ids = {item.id for item in _current_node_executions(db, task).values()}
        results = list(db.scalars(select(RuleResult).where(RuleResult.audit_task_id == task.id, RuleResult.node_execution_id.in_(current_ids), RuleResult.rule_key.in_(report_keys)).order_by(RuleResult.id)))
        all_current_results = list(db.scalars(select(RuleResult).where(RuleResult.audit_task_id == task.id, RuleResult.node_execution_id.in_(current_ids)).order_by(RuleResult.id)))
        # 显式转换的历史结果把旧 verdict.report 单独展开为 report，
        # 但没有建立 AuditReport。只读恢复正文，不伪造数据库状态；下一次报告节点执行
        # 会按新逻辑正式写入 AuditReport。
        recovered = []
        for result in results:
            payload = task_value_service.value_of(db, task, task_value_service.RECORD_RULE_RESULT, result.id)
            report_data = payload.get("report")
            if isinstance(report_data, dict) and isinstance(report_data.get("content"), str) and report_data["content"].strip():
                rule = next((item for item in _task_manifest(task)["rules"] if item.get("rule_key") == result.rule_key), {})
                recovered.append({"rule_result": result, "report": report_data, "rule": rule})
        if recovered:
            current_result_keys = {item.rule_key for item in all_current_results}
            complete = (set(task.task_schema_json["enabled_rule_keys"]) - report_keys) <= current_result_keys and all(item.status != "NOT_EXECUTED" for item in all_current_results)
            choices = [{"report_no": f"CSV-{item['rule_result'].id}", "report_name": item["rule"].get("name") or item["rule_result"].rule_name, "current": True} for item in recovered]
            selected = next((item for item in recovered if report_no == f"CSV-{item['rule_result'].id}"), recovered[0])
            if report_no and not any(item["report_no"] == report_no for item in choices):
                raise BusinessError("REPORT_NOT_FOUND", "该任务中没有对应的报告", status_code=404)
            report_data = selected["report"]
            risk_rules = [item for item in _task_manifest(task)["rules"] if item.get("kind") != "REPORT" and is_risk_rule(item) and item.get("rule_key") in task.task_schema_json["enabled_rule_keys"]]
            result_by_key = {item.rule_key: item for item in all_current_results}
            risk_available = bool(risk_rules) and all(result_by_key.get(item["rule_key"]) is not None and result_by_key[item["rule_key"]].status in CONCLUSIVE_RESULT_STATUSES for item in risk_rules)
            return {"task_no": task.task_no, "status": "COMPLETE" if complete else "INCOMPLETE",
                    "content_type": report_data.get("content_type", "text/markdown"), "content": report_data["content"],
                    "reasons": [], "report_generated": True, "scope_execution_complete": complete,
                    "risk_conclusion_available": risk_available, "results_scope": "CURRENT_RUN", "current": True,
                    "input_revision": task.input_revision, "snapshot": {"task_input_revision": task.input_revision, "rule_result_ids": [item["rule_result"].id for item in recovered]},
                    "reports": choices, "report_no": f"CSV-{selected['rule_result'].id}", "report_name": selected["rule"].get("name") or selected["rule_result"].rule_name,
                    "conclusion": None, "format_support": [], "business_section_count": len((selected["rule"].get("report_definition") or {}).get("sections") or [])}
        reasons = [f"{r.rule_name}：已执行，但没有保存可读取的报告正文，请查看执行详情" for r in results]
        return {"task_no": task.task_no, "status": "NOT_GENERATED", "content_type": "text/markdown", "content": None,
                "reasons": reasons or ["生效报告执行项尚未产生正文" if report_keys else "本任务没有生效的报告执行项"],
                "report_generated": False, "scope_execution_complete": False, "risk_conclusion_available": False,
                "results_scope": "CURRENT_RUN", "current": False, "input_revision": task.input_revision, "snapshot": None,
                "reports": choices, "report_no": None, "report_name": None,
                "conclusion": None, "format_support": [], "business_section_count": 0}
    current = is_current(report)
    unresolved = (report.reasons_json or {}).get("unresolved") or []
    rule = next((item for item in _task_manifest(task)["rules"] if item["rule_key"] == (report.snapshot_json or {}).get("rule_key")), None)
    return {"task_no": task.task_no, "status": "COMPLETE" if current and report.scope_execution_complete and not unresolved else "INCOMPLETE",
            "content_type": report.content_type, "content": report.content_md, "reasons": [i["message"] for i in unresolved] + ([] if current else ["报告对应的输入或执行批次已失效"]),
            "report_generated": report.report_generated, "scope_execution_complete": report.scope_execution_complete,
            "risk_conclusion_available": report.risk_conclusion_available, "results_scope": "CURRENT_RUN", "current": current,
            "input_revision": task.input_revision, "snapshot": report.snapshot_json, "conclusion": None,
            "reports": choices, "report_no": report.report_no, "report_name": report.report_name,
            "format_support": (report.reasons_json or {}).get("format_support", [{"format": report.content_type, "supported": True}]),
            "business_section_count": len((rule or {}).get("report_definition", {}).get("sections") or [])}
