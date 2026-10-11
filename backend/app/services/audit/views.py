"""任务结果的只读视图：报告、风险、证据。

这里只读当前有效批次的数据并如实表达状态，不写库、不重算业务结论：

- 报告正文从 `audit_report.content_md` 读（正文的唯一存放处）；执行过报告节点却没有
  可读正文时如实说明是记录缺失，不显示成「报告尚未生成」。
- 风险命中只统计 ABNORMAL；「没有风险记录」不能写成「无风险」。
- 证据只返回原文、来源和内容依据结论，不返回页码、坐标或定位状态。"""

from .. import task_csv_service

from ...common.exceptions import BusinessError
from ...entity import AuditReport, DocumentUpload, Evidence, NodeExecution, RuleResult, StoredArtifact, TaskDocument, TaskFieldValue
from ..task_lookup import task_or_404
from .evidence import _quote_check_of
from .shared import QUOTE_CHECK_LABELS, _task_manifest
from .state import _current_node_executions
from sqlalchemy import select
from sqlalchemy.orm import Session
from typing import Any


def risks_view(db: Session, task_no: str) -> dict[str, Any]:
    """风险结果只读视图：只统计 ABNORMAL 为命中，未配置 / 未执行 / 无法判断分开表达。"""
    task = task_or_404(db, task_no)
    manifest = _task_manifest(task)
    enabled_keys = set(task.task_schema_json["enabled_rule_keys"])
    risk_rules = [
        item
        for item in manifest["rules"]
        if (item.get("operator") == "RISK_TRIGGER" or bool(item.get("risk_condition"))) and item["rule_key"] in enabled_keys
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
    results = task_csv_service.from_index(db, task, RuleResult, results)
    status_counts: dict[str, int] = {}
    for item in results:
        status_counts[item.status] = status_counts.get(item.status, 0) + 1
    risk_level_by_key = {item["rule_key"]: item.get("risk_level_value") or item.get("parameters", {}).get("risk_level") for item in risk_rules}
    # 命中只统计 ABNORMAL，不能用结果总数冒充命中数。
    hit_count = sum(1 for item in results if item.status == "ABNORMAL")
    risk_conclusion_available = (
        risk_configured
        and len(results) == len(risk_rules)
        and all(item.status in {"PASSED", "ABNORMAL"} for item in results)
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
                "name": item.rule_name,
                "status": item.status,
                "message": item.message,
                "risk_level": risk_level_by_key.get(item.rule_key),
                "details": item.result_json or {},
            }
            for item in results
        ],
    }

def report_view(db: Session, task_no: str) -> dict[str, Any]:
    """读取真实保存的报告，不要求固定报告阶段或节点结果格式。"""
    task = task_or_404(db, task_no)
    report = db.scalar(select(AuditReport).where(AuditReport.audit_task_id == task.id).order_by(AuditReport.id.desc()))
    if report is None:
        report_keys = {r["rule_key"] for r in _task_manifest(task)["rules"] if r.get("kind") == "REPORT" and r["rule_key"] in task.task_schema_json["enabled_rule_keys"]}
        results = task_csv_service.records(db, task, RuleResult)
        reasons = [r.message for r in results if r.rule_key in report_keys and r.status not in {"PASSED", "ABNORMAL"}]
        return {"task_no": task.task_no, "status": "NOT_GENERATED", "content_type": "text/markdown", "content": None,
                "reasons": reasons or ["生效报告执行项尚未产生正文" if report_keys else "本任务没有生效的报告执行项"],
                "report_generated": False, "scope_execution_complete": False, "risk_conclusion_available": False,
                "results_scope": "CURRENT_RUN", "current": False, "input_revision": task.input_revision, "snapshot": None,
                "conclusion": None, "format_support": [], "business_section_count": 0}
    execution = db.get(NodeExecution, report.node_execution_id) if report.node_execution_id else None
    current = report.input_revision == task.input_revision and execution is not None and execution.status != "INVALIDATED"
    unresolved = (report.reasons_json or {}).get("unresolved") or []
    return {"task_no": task.task_no, "status": "COMPLETE" if current and report.scope_execution_complete and not unresolved else "INCOMPLETE",
            "content_type": report.content_type, "content": report.content_md, "reasons": [i["message"] for i in unresolved] + ([] if current else ["报告对应的输入或执行批次已失效"]),
            "report_generated": report.report_generated, "scope_execution_complete": report.scope_execution_complete,
            "risk_conclusion_available": report.risk_conclusion_available, "results_scope": "CURRENT_RUN", "current": current,
            "input_revision": task.input_revision, "snapshot": report.snapshot_json, "conclusion": None,
            "format_support": [{"format": report.content_type, "supported": True}],
            "business_section_count": len(_task_manifest(task).get("report_template") or [])}


def evidence_view(db: Session, task_no: str, evidence_no: str) -> dict[str, Any]:
    """单条证据只读视图：引用原文、来源与内容依据核对结论。"""
    task = task_or_404(db, task_no)
    evidence = db.scalar(
        select(Evidence).where(Evidence.audit_task_id == task.id, Evidence.evidence_no == evidence_no)
    )
    if not evidence:
        raise BusinessError("EVIDENCE_NOT_FOUND", "证据不存在", status_code=404)
    upload = db.get(DocumentUpload, evidence.document_upload_id) if evidence.document_upload_id else None
    document = db.get(TaskDocument, upload.task_document_id) if upload else None
    if upload is not None and (not document or document.audit_task_id != task.id):
        raise BusinessError("EVIDENCE_NOT_FOUND", "证据不存在", status_code=404)
    is_current_upload = bool(
        upload is not None
        and document is not None
        and document.audit_task_id == task.id
        and document.current_upload_id == upload.id
    )
    is_current_target = False
    if evidence.rule_result_id is not None:
        result = db.get(RuleResult, evidence.rule_result_id)
        if not result or result.audit_task_id != task.id:
            raise BusinessError("EVIDENCE_NOT_FOUND", "证据不存在", status_code=404)
        # 校验该结果的业务内容仍能从任务 CSV 恢复；身份与当前执行批次仍由数据库索引判断。
        task_csv_service.from_index(db, task, RuleResult, [result])
        is_current_target = (
            db.scalar(
                select(NodeExecution.id).where(
                    NodeExecution.id == result.node_execution_id,
                    NodeExecution.status != "INVALIDATED",
                )
            )
            is not None
        )
    elif evidence.field_value_id is not None:
        fact = db.get(TaskFieldValue, evidence.field_value_id)
        if not fact or fact.audit_task_id != task.id:
            raise BusinessError("EVIDENCE_NOT_FOUND", "证据不存在", status_code=404)
        fact_csv = task_csv_service.from_index(db, task, TaskFieldValue, [fact])[0]
        # 结构化字段可以有多条属性证据；只要所属事实仍是当前版本，这些证据都属于当前事实。
        is_current_target = bool(fact_csv.is_current)
    elif evidence.artifact_id is None:
        raise BusinessError("EVIDENCE_NOT_FOUND", "证据不存在", status_code=404)
    artifact = db.get(StoredArtifact, evidence.artifact_id) if evidence.artifact_id else None
    quote_check = _quote_check_of(db, evidence)
    return {
        "evidence_no": evidence.evidence_no,
        "document_name": upload.original_filename if upload else None,
        "field_path": evidence.field_path,
        "quote_text": evidence.quote_text,
        "quote_check": quote_check,
        "quote_check_label": QUOTE_CHECK_LABELS.get(quote_check, "内容依据状态未知"),
        "document_upload_no": upload.upload_no if upload else None,
        "artifact_type": artifact.artifact_type if artifact else None,
        "is_current_upload": is_current_upload,
        # 旧上传版本或已被替换的字段版本的证据仍可按历史身份查看。
        "historical": not (is_current_upload and is_current_target),
    }
