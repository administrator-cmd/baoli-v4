"""任务结果的只读视图：报告、风险、证据。

这里只读当前有效批次的数据并如实表达状态，不写库、不重算业务结论：

- 报告正文从 `audit_report.content_md` 读（正文的唯一存放处）；执行过报告节点却没有
  可读正文时如实说明是记录缺失，不显示成「报告尚未生成」。
- 风险命中只统计 ABNORMAL；「没有风险记录」不能写成「无风险」。
- 证据的位置结论与内容依据结论分别透出，页面不能把「无法定位」显示成「原文不存在」。"""

from ...domain.manifest import rule_node_type_of
from ...common.exceptions import BusinessError
from ...entities import AuditReport, DocumentUpload, Evidence, NodeExecution, RuleResult, StoredArtifact, TaskDocument, TaskFieldValue
from ..task_lookup import task_or_404
from .evidence import _quote_check_of
from .reporting import _configured_format_support
from .shared import QUOTE_CHECK_LABELS, _task_manifest
from .state import _current_execution_ids, _node_codes_of_type
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
        if rule_node_type_of(item) == "RISK_RATING" and item["operator"] == "RISK_TRIGGER" and item["rule_key"] in enabled_keys
    ]
    risk_configured = bool(risk_rules)
    risk_keys = {item["rule_key"] for item in risk_rules}
    # 只取当前有效执行批次的风险结果：失效批次与旧上传版本不进入当前结论。
    current_execution_ids = _current_execution_ids(db, task)
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
    risk_level_by_key = {item["rule_key"]: item.get("parameters", {}).get("risk_level") for item in risk_rules}
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
    """报告只读视图：正文取自 audit_report.content_md，三信号与范围快照取自本次执行结果。"""
    task = task_or_404(db, task_no)
    # 报告节点的**实例码**由用户配置，不能写死能力类型当实例码查询。
    report_node_codes = _node_codes_of_type(_task_manifest(task), "REPORT_OUTPUT")
    if not report_node_codes:
        raise BusinessError(
            "AUDIT_NODE_UNKNOWN",
            "本任务绑定的场景没有配置报告输出节点",
            status_code=500,
            details={"task_no": task.task_no},
        )
    execution = db.scalar(
        select(NodeExecution)
        .where(
            NodeExecution.audit_task_id == task.id,
            NodeExecution.node_code.in_(report_node_codes),
            NodeExecution.status != "INVALIDATED",
        )
        .order_by(NodeExecution.id.desc())
    )
    if not execution or execution.status == "SKIPPED" or not execution.result_json:
        if execution and execution.status == "SKIPPED" and execution.result_json:
            reason = execution.result_json["skip_reason"]
        else:
            stale_report = db.scalar(
                select(NodeExecution.id)
                .where(
                    NodeExecution.audit_task_id == task.id,
                    NodeExecution.node_code.in_(report_node_codes),
                    NodeExecution.status == "INVALIDATED",
                )
                .limit(1)
            )
            reason = (
                "此前生成的报告已随字段、规则或材料变化失效，需要重新执行报告输出节点"
                if stale_report
                else "报告输出节点尚未执行"
            )
        return {
            "task_no": task.task_no,
            "status": "NOT_GENERATED",
            "content_type": "text/markdown",
            "content": None,
            "reasons": [reason],
            "report_generated": False,
            "scope_execution_complete": False,
            "risk_conclusion_available": False,
            "results_scope": "CURRENT_RUN",
            "current": False,
            "input_revision": task.input_revision,
            "snapshot": None,
            # 报告未生成时也要给出声明格式的支持状态：模板要求 PDF 而系统只产 Markdown，
            # 这件事不因报告尚未生成而消失。
            "conclusion": None,
            "format_support": _configured_format_support(task),
            "business_section_count": 0,
        }
    result = execution.result_json
    # 不可判定的原因必须分开表达，不能都写成“未配置风险”。
    reason_labels = {
        "unfinished_fields": "存在未形成有效结果的字段",
        "unfinished_rules": "存在未执行、证据不足或待复核的规则，或有已启用规则尚未形成结果",
        "pending_reviews": "存在待处理的人工复核项",
        # 两种材料类型状态对已完成结论的影响不同，合并成一句会让人以为普通审核也被阻断。
        "material_type_mismatches": "存在尚未定案或已确认不符的材料类型（待确认事项，普通审核结论仍按真实原文有效）",
        "risk_not_configured": "本模板未配置风险评级规则，本次审核未形成风险结论",
        "risk_inconclusive": "已配置的风险规则尚未全部形成可用结论，风险不可判定",
        "missing_rule_keys": "有已启用规则尚未形成任何结果",
        "missing_required_materials": "存在尚未上传的必传材料",
        "failed_nodes": "存在执行失败或被阻断的审核节点",
    }
    reasons: list[str] = []
    for key, value in (result.get("reasons") or {}).items():
        label = reason_labels.get(key)
        if label is None:
            continue
        if isinstance(value, list):
            if value:
                reasons.append(label)
        elif value:
            reasons.append(label)
    # 报告的三信号、范围快照与结构化结论都随执行结果保存，这里直接读它。
    # 报告规则结果（RuleResult）只作为「报告已生成」的逐条结果供规则页展示，不再承载这些内容。
    snapshot = result.get("snapshot") if isinstance(result.get("snapshot"), dict) else None
    generated_revision = snapshot.get("task_input_revision") if snapshot else result.get("input_revision")
    current = isinstance(generated_revision, int) and generated_revision == task.input_revision
    if not current:
        reasons.append(
            "报告对应的输入修订已过期，需要重新执行报告输出节点"
            if snapshot is not None
            else "报告缺少输入修订快照，无法确认它是否对应当前输入，请重新执行报告输出节点"
        )
    # 结构化结论：页面优先读它，不再靠匹配正文行首文案。
    # 结论随执行结果保存（与三信号同一个载荷），不再依赖报告规则结果的 result_json。
    conclusion = result.get("conclusion") if isinstance(result.get("conclusion"), dict) else None
    format_support = result.get("format_support") if isinstance(result.get("format_support"), list) else []
    if not isinstance(result.get("format_support"), list):
        # 报告结果里没有时从任务冻结配置取，
        # 保证页面对旧报告也能显示格式缺口，而不是显示成「没有声明」。
        configured_manifest = _task_manifest(task)
        groups = configured_manifest.get("report_groups") or {}
        format_support = [
            entry
            for report in (groups.get("reports") or [])
            for entry in (report.get("format_support") or [])
        ]
    # 正文从 report.content_md 读：它是正文的唯一存放处（本节点执行、发生失效时都一并处理）。
    report = db.scalar(
        select(AuditReport)
        .where(AuditReport.node_execution_id == execution.id)
        .order_by(AuditReport.id.desc())
    )
    if report is None or not report.content_md:
        # 报告节点执行过却没有可读正文：如实说明是记录缺失，不能显示成「报告尚未生成」。
        reasons.append("本次报告输出没有留下可读的报告正文记录，请重新执行报告输出节点")
    return {
        "task_no": task.task_no,
        "status": result["report_status"],
        "content_type": result["content_type"],
        "content": report.content_md if report else None,
        "reasons": reasons,
        # 三个信号分开表达：报告是否生成 / 已配置范围是否执行完整 / 风险是否可判定。
        "report_generated": bool(result.get("report_generated", True)),
        "scope_execution_complete": bool(result.get("scope_execution_complete", result["report_status"] == "COMPLETE")),
        "risk_conclusion_available": bool(result.get("risk_conclusion_available", False)),
        "results_scope": result.get("results_scope", "CURRENT_RUN"),
        "current": current,
        "input_revision": task.input_revision,
        "snapshot": snapshot,
        "conclusion": conclusion,
        "format_support": format_support,
        "business_section_count": int(result.get("business_section_count") or 0),
    }

def evidence_view(db: Session, task_no: str, evidence_no: str) -> dict[str, Any]:
    """单条证据只读视图：引用原文、依据类型与定位核对结论。"""
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
        # 结构化字段可以有多条属性证据；只要所属事实仍是当前版本，这些证据都属于当前事实。
        is_current_target = bool(fact.is_current)
    elif evidence.artifact_id is None:
        raise BusinessError("EVIDENCE_NOT_FOUND", "证据不存在", status_code=404)
    artifact = db.get(StoredArtifact, evidence.artifact_id) if evidence.artifact_id else None
    quote_check = _quote_check_of(db, evidence)
    return {
        "evidence_no": evidence.evidence_no,
        "document_name": upload.original_filename if upload else None,
        "field_path": evidence.field_path,
        "page_no": evidence.page_no,
        "quote_text": evidence.quote_text,
        "bbox": evidence.bbox_json,
        # 位置核对与内容依据是两个结论，必须分别透出：定位失败不影响内容可用于审核，
        # 页面也不能把「无法定位」显示成「这段原文不存在」。
        "locator_status": evidence.locator_status,
        "locator_note": evidence.locator_note,
        "quote_check": quote_check,
        "quote_check_label": QUOTE_CHECK_LABELS.get(quote_check, "内容依据状态未知"),
        "document_upload_no": upload.upload_no if upload else None,
        "artifact_type": artifact.artifact_type if artifact else None,
        "is_current_upload": is_current_upload,
        # 旧上传版本或已被替换的字段版本的证据仍可按历史身份查看。
        "historical": not (is_current_upload and is_current_target),
    }
