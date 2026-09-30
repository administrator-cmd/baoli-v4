"""报告生成：系统固定事实章节 + 已配置的业务报告章节。

报告必须同时、分开表达三件事，不能用一句「成功」代替：

- 报告是否生成；
- 已配置的审核范围是否**执行完整**（按预期集合核对：启用字段、启用规则、必传材料、
  启用节点都必须有有效结果）；
- 风险是否**可判定**（未配置风险评级不是执行故障，但也不能据此认定无风险）。

结论同时以结构化键写入执行结果，页面不靠匹配正文行首文案提取结论——正文任何一次
措辞调整都会让页面静默退化成「没有结论」。

正文只落 `audit_report.content_md`：执行结果会作为操作结果回传，把几百行正文塞进去
会被页面当成「结果摘要」铺开成一条无换行的长文本。"""

from ...domain.audit_vocab import LEGACY_TYPE_MISMATCH, MATERIAL_TYPE_REJECTED, MATERIAL_TYPE_REVIEW, MATERIAL_TYPE_UNAVAILABLE_STATUSES, read_material_type_status
from ...domain.identity import public_no
from ...domain.manifest import NODE_DEFINITIONS, rule_node_type_of
from .state import _node_codes_of_type
from ...common.exceptions import BusinessError
from ...entities import AuditReport, AuditTask, DocumentUpload, Evidence, ManualReview, NodeExecution, RuleResult, TaskDocument, TaskFieldValue
from .evidence import _fact_evidence_suffix, _quote_check_of
from .facts import _current_field_fact_pairs, _input_trace
from .shared import BASIS_TYPE_LABELS, LOCATOR_STATUS_LABELS, QUOTE_CHECK_LABELS, _current_uploads, _material_names, _scenario
from .state import _current_execution_ids, _current_node_executions, _risk_rule_keys, _scope_results
from datetime import UTC, datetime
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from typing import Any


def _render_report_groups(
    db: Session,
    task: AuditTask,
    manifest: dict[str, Any],
    groups: list[dict[str, Any]],
) -> list[str]:
    """按报告编号逐份渲染报告章节。

    内容类型决定章节的数据来源，章节标题不参与推断；结果排序只作用于审核/风险结果，
    不改变章节顺序与字段顺序。报告筛选已有结果，不重新评定风险。
    """
    material_names = _material_names(manifest)
    field_defs = {item["field_key"]: item for item in manifest["fields"]}
    current_pairs = _current_field_fact_pairs(db, task)
    facts = {key: pair[0] for key, pair in current_pairs.items()}
    fact_statuses = {key: pair[1].status for key, pair in current_pairs.items()}
    evidence_by_id = {
        item.id: item for item in db.scalars(select(Evidence).where(Evidence.audit_task_id == task.id)).all()
    }
    current_execution_ids = _current_execution_ids(db, task)
    results = (
        db.scalars(
            select(RuleResult)
            .where(
                RuleResult.audit_task_id == task.id,
                RuleResult.node_execution_id.in_(current_execution_ids),
            )
            .order_by(RuleResult.id)
        ).all()
        if current_execution_ids
        else []
    )
    lines: list[str] = []
    for group in groups:
        sections = [item for item in group.get("sections", []) if item.get("config_status") != "DISABLED"]
        if not sections:
            lines.append(f"- 报告“{group.get('report_name')}”（{group.get('report_code')}）：全部章节已停用，本次不输出该报告。")
            lines.append("")
            continue
        formats = "、".join(group.get("formats") or [])
        scope_keys = list(group.get("material_keys") or [])
        scope_names = "、".join(material_names.get(key, key) for key in scope_keys)
        pending_sections = [item["section_name"] for item in sections if item.get("config_status") == "PENDING"]
        lines.append(f"### {group.get('report_name')}（报告编号 {group.get('report_code')}）")
        lines.append(f"- 输出格式：{formats}")
        lines.append(f"- 材料范围：{scope_names}")
        if group.get("result_sort"):
            lines.append(f"- 结果排序：{group['result_sort']}")
        if pending_sections:
            lines.append(f"- 配置状态：未完成（以下章节仍为待确认：{'、'.join(pending_sections)}）")
        lines.append("")
        for section in sections:
            lines.append(f"#### {section['section_order']}. {section['section_name']}")
            if section.get("content_instruction"):
                lines.append(f"- 本节内容要求（展示说明，不代表已生成正文）：{section['content_instruction']}")
            content_type = section.get("content_type")
            if content_type in {"FIELD_DETAIL", "FIELD_AND_RESULT"}:
                for field_key in section.get("field_keys", []):
                    definition = field_defs.get(field_key)
                    if definition is None:
                        continue
                    fact = facts.get((field_key, definition["material_key"]))
                    label = f"{material_names.get(definition['material_key'], '未知材料')} · {definition['name']}"
                    if fact is not None and fact_statuses.get((field_key, definition["material_key"])) in {"PRESENT", "REVIEWED"}:
                        lines.append(f"- {label}：{fact.raw_value}{_fact_evidence_suffix(evidence_by_id, fact)}")
                    else:
                        # 未取得的值明确标记，不编造内容；也不把“未提供”写成“不适用”。
                        lines.append(
                            f"- {label}：未形成有效结果（"
                            f"{fact_statuses.get((field_key, definition['material_key']), 'NOT_EXECUTED')}）"
                        )
            if content_type in {"AUDIT_RESULT", "FIELD_AND_RESULT", "CONCLUSION"}:
                # 按**实际结果涉及的材料**筛选，而不是按规则声明的适用材料：
                # 只发生在补充协议内部的异常不能混进主合同报告；跨材料检查涉及两侧，
                # 因此只要有一侧在范围内就保留，并保留完整对照证据。
                selected, related, unattributed = _scope_results(results, scope_keys, material_names)
                if selected or related:
                    for item in selected:
                        lines.append(f"- 检查「{item.rule_name}」：{item.status}（{item.message}）")
                    for item, extra in related:
                        names = "、".join(extra)
                        lines.append(
                            f"- 检查「{item.rule_name}」（涉及范围外材料作为对照证据：{names}）："
                            f"{item.status}（{item.message}）"
                        )
                elif unattributed:
                    # 有结果但无法归属材料：必须如实说明，不能写成“没有可用的检查结果”。
                    lines.append(
                        f"- 报告范围内没有可归属的检查结果；另有 {unattributed} 条检查结果缺少材料归属，"
                        "无法判断是否属于本报告范围，请先核对证据归属。"
                    )
                else:
                    lines.append("- 报告范围内没有可用的检查结果。")
            if content_type == "RISK_LIST":
                risk_results = [
                    item for item in results if item.rule_key in _risk_rule_keys(manifest)
                ]
                selected, related, unattributed = _scope_results(risk_results, scope_keys, material_names)
                if selected or related:
                    for item in selected:
                        lines.append(f"- 风险「{item.rule_name}」：{item.status}（{item.message}）")
                    for item, extra in related:
                        names = "、".join(extra)
                        lines.append(
                            f"- 风险「{item.rule_name}」（关联对照材料：{names}）："
                            f"{item.status}（{item.message}）"
                        )
                elif unattributed:
                    # 筛选漏项时绝不能显示“没有已成立风险”：那是把系统的归属缺口
                    # 说成业务结论。这里如实说明有多少条风险结果无法归属。
                    lines.append(
                        f"- 报告范围内没有可归属的风险结果；另有 {unattributed} 条风险结果缺少材料归属，"
                        "无法判断是否属于本报告范围。这不等于没有风险。"
                    )
                else:
                    lines.append("- 报告范围内没有已成立的风险或待复核事项；这不等于自动通过。")
            if content_type == "CONCLUSION":
                lines.append("- 本节结论按报告范围内的检查、风险与人工复核结果形成；仍有不合格或无法判断项时不得自动通过。")
            lines.append("")
    return lines

def _report_output(db: Session, task: AuditTask, manifest: dict[str, Any], execution: NodeExecution) -> dict[str, Any]:
    enabled_keys = set(task.task_schema_json["enabled_rule_keys"])
    report_rules = [
        item
        for item in manifest["rules"]
        if rule_node_type_of(item) == "REPORT_OUTPUT" and item["operator"] == "REPORT_RENDER" and item["rule_key"] in enabled_keys
    ]
    if not report_rules:
        # 本任务没有选入报告生成规则：**显式跳过**，不报错。
        #
        # 方案是用户自己的范围选择——勾了什么就跑什么。没勾报告生成就没有报告，
        # 这正是这份方案表达的意思，不该让任务在最后一个节点整个失败。
        # 跳过必须写明原因：**「没有报告」不等于「审核通过」**，也不等于
        # 「报告生成了但是空的」。因此三个信号与正常路径同一口径，report_generated=False。
        return {
            "skipped": True,
            "skip_reason": "本任务未选入报告生成规则，本次不生成审核报告",
            "report_generated": False,
            "results_scope": "CURRENT_RUN",
            "input_revision": task.input_revision,
        }
    risk_rules = [
        item
        for item in manifest["rules"]
        if rule_node_type_of(item) == "RISK_RATING" and item["operator"] == "RISK_TRIGGER" and item["rule_key"] in enabled_keys
    ]
    # 未配置风险评级不是执行失败：报告照常输出，但必须写明未配置且不能给出无风险结论。
    risk_configured = bool(risk_rules)
    # 报告只取当前有效执行批次：节点序号不晚于当前节点、每个节点取最新一条未失效执行。
    current_executions = _current_node_executions(db, task)
    current_execution_ids = {item.id for item in current_executions.values()}
    unfinished_node_codes = sorted(
        node["node_code"]
        for node in manifest["nodes"]
        if node.get("enabled", True)
        and (node.get("node_type") or node["node_code"]) != "REPORT_OUTPUT"
        and (
            node["node_code"] not in current_executions
            or current_executions[node["node_code"]].status not in {"SUCCEEDED", "SKIPPED"}
        )
    )
    if unfinished_node_codes:
        raise BusinessError(
            "REPORT_UPSTREAM_NODE_UNFINISHED",
            "仍有已启用的审核节点未完成，不能提前生成报告",
            status_code=409,
            details={"node_codes": unfinished_node_codes},
        )
    # 报告输出节点按**能力类型**解析实例码：`node_code` 是用户配置的实例码，
    # 写死能力类型当实例码会在用户改过实例码后一条都匹配不上——
    # 报告输出规则的产物就会被误算进「预期结果缺失」。
    report_node_codes = set(_node_codes_of_type(manifest, "REPORT_OUTPUT"))
    risk_node_codes = set(_node_codes_of_type(manifest, "RISK_RATING"))
    output_execution_ids = {
        item.id for item in current_executions.values() if item.node_code in report_node_codes
    }
    # 已配置但供应商没有质量结论的检查：必须在报告里如实呈现，不能被当成已通过。
    capability_gap_items = manifest.get("capability_gaps") or []
    current_pairs = _current_field_fact_pairs(db, task)
    fields = {key: pair[0] for key, pair in current_pairs.items()}
    field_statuses = {key: pair[1].status for key, pair in current_pairs.items()}
    field_status_labels = {
        "PENDING": "尚未形成结果",
        "NOT_PRESENT": "未抽取到",
        "EXTRACTION_FAILED": "字段抽取失败",
        "EVIDENCE_UNCLEAR": "证据不足",
        "NORMALIZATION_FAILED": "字段标准化失败",
        "NOT_EXECUTED": "未执行",
        "REVIEWED": "已人工复核",
        "PRESENT": "已抽取",
    }
    node_labels = dict(NODE_DEFINITIONS)
    scenario = _scenario(db, task)
    material_names = {item["material_key"]: item["name"] for item in manifest["materials"]}
    material_defs = {item["material_key"]: item for item in manifest["materials"]}
    field_name_by_key = {item["field_key"]: item["name"] for item in manifest["fields"]}
    rule_name_by_key = {item["rule_key"]: item["name"] for item in manifest["rules"]}
    ruled_status_labels = {
        "PASSED": "通过",
        "ABNORMAL": "异常",
        "EVIDENCE_INSUFFICIENT": "证据不足",
        "NOT_APPLICABLE": "不适用",
        "NOT_EXECUTED": "未执行",
        "PENDING_REVIEW": "待复核",
        MATERIAL_TYPE_REVIEW: "材料类型待复核",
        MATERIAL_TYPE_REJECTED: "材料类型不符",
        LEGACY_TYPE_MISMATCH: "材料类型待复核",
        "RUNNING": "执行中",
    }
    status_labels = {**field_status_labels, **ruled_status_labels}
    document_status_labels = {
        "WAITING_UPLOAD": "等待上传",
        "UPLOADED": "已上传，尚未处理",
        "PREPARED": "文件处理完成",
        MATERIAL_TYPE_REVIEW: "材料类型待复核",
        MATERIAL_TYPE_REJECTED: "材料类型不符（人工已确认）",
        LEGACY_TYPE_MISMATCH: "材料类型待复核",
    }
    risk_status_labels = {
        "ABNORMAL": "已触发风险疑点",
        "PASSED": "未触发风险疑点",
        "EVIDENCE_INSUFFICIENT": "尚不能形成结论",
        "NOT_EXECUTED": "未执行",
        "NOT_APPLICABLE": "不适用",
        "PENDING_REVIEW": "待复核",
    }
    decision_labels = {
        "CONFIRM_ABNORMAL": "确认异常",
        "MARK_NORMAL": "判定正常",
        "EVIDENCE_INSUFFICIENT": "证据不足",
        "CONFIRM_FIELD": "确认字段",
        "MODIFY_FIELD": "修改字段",
        "MARK_NOT_PRESENT": "确认字段不存在",
    }
    unfinished_rule_statuses = {"EVIDENCE_INSUFFICIENT", "NOT_EXECUTED", "PENDING_REVIEW"}
    pairs = _current_uploads(db, task)
    documents_by_id = {document.id: document for document, _ in pairs}
    documents_by_material = {document.material_key: document for document, _ in pairs}
    uploads_by_material = {document.material_key: upload for document, upload in pairs}
    uploaded_keys = {key for key, upload in uploads_by_material.items() if upload is not None}
    out_of_scope_field_keys: set[tuple[str, str]] = set()
    unfinished_field_names: list[str] = []
    confirmed_missing_field_ids = {
        review.field_value_id
        for review in db.scalars(
            select(ManualReview).where(
                ManualReview.audit_task_id == task.id,
                ManualReview.status == "RESOLVED",
                ManualReview.review_type == "FIELD_CONFIRMATION",
                ManualReview.decision == "MARK_NOT_PRESENT",
                ManualReview.field_value_id.is_not(None),
            )
        ).all()
        if review.field_value_id is not None
    }
    for field in task.task_schema_json["fields"]:
        key = (field["field_key"], field["material_key"])
        fact = fields.get(key)
        if fact is not None and field_statuses.get(key) in {"PRESENT", "REVIEWED"}:
            continue
        if fact is not None and field_statuses.get(key) == "NOT_PRESENT" and fact.id in confirmed_missing_field_ids:
            continue
        # 可选材料未上传时该字段不在本次有效执行范围内（6.3）；引用它的规则是否适用
        # 由规则自己的适用条件决定，这里既不当作通过，也不当作执行故障。
        if field["material_key"] not in uploaded_keys and not material_defs[field["material_key"]]["required"]:
            out_of_scope_field_keys.add(key)
            continue
        state = field_statuses.get(key, "NOT_EXECUTED")
        unfinished_field_names.append(
            f"{material_names.get(field['material_key'], field['material_key'])}·{field['name']}"
            f"（{field_status_labels.get(state, state)}）"
        )
    unfinished_fields = len(unfinished_field_names)
    # 规则结果只取当前有效执行批次，并排除报告输出节点自身在本次执行中尚未提交的结果。
    current_rule_results = (
        db.scalars(
            select(RuleResult)
            .where(
                RuleResult.audit_task_id == task.id,
                RuleResult.node_execution_id.in_(current_execution_ids),
            )
            .order_by(RuleResult.id)
        ).all()
        if current_execution_ids
        else []
    )
    rule_results = [item for item in current_rule_results if item.node_execution_id not in output_execution_ids]
    passed_rules = [item for item in rule_results if item.status == "PASSED"]
    abnormal_rules = [item for item in rule_results if item.status == "ABNORMAL"]
    other_rules = [item for item in rule_results if item.status not in {"PASSED", "ABNORMAL"}]
    # 只有输出字段全部落在“可选材料未上传”范围内的抽取规则才算出执行范围（6.3）：
    # 它们不是执行故障，但也绝不等于通过，会在不确定事项里单独列出。
    field_material_by_key = {item["field_key"]: item["material_key"] for item in manifest["fields"]}
    extraction_outputs = {
        rule["rule_key"]: list(rule.get("outputs", []))
        for rule in manifest["rules"]
        if rule_node_type_of(rule) == "FIELD_EXTRACTION" and rule["operator"] == "LLM_EXTRACT"
    }

    def rule_out_of_scope(result: RuleResult) -> bool:
        outputs = extraction_outputs.get(result.rule_key)
        if not outputs:
            return False
        return all((key, field_material_by_key.get(key)) in out_of_scope_field_keys for key in outputs)

    # 预期集合：任务实际启用范围里的每条规则都必须形成结果，不能只统计已有结果里的未完成项。
    # 报告输出规则由本节点自身产出，不进入“预期结果缺失”的判断。
    # 按**能力类型**判断，不按实例码：`node` 是用户配置的实例码。
    rule_node_by_key = {item["rule_key"]: rule_node_type_of(item) for item in manifest["rules"]}
    expected_rule_keys = {
        key
        for key in task.task_schema_json["enabled_rule_keys"]
        if rule_node_by_key.get(key) != "REPORT_OUTPUT"
    }
    produced_rule_keys = {item.rule_key for item in rule_results}
    missing_rule_keys = sorted(expected_rule_keys - produced_rule_keys)
    unfinished_rule_results = [
        item for item in rule_results if item.status in unfinished_rule_statuses and not rule_out_of_scope(item)
    ]
    out_of_scope_rule_names = [
        item.rule_name
        for item in rule_results
        if item.status in unfinished_rule_statuses and rule_out_of_scope(item)
    ]
    unfinished_rules = len(unfinished_rule_results) + len(missing_rule_keys)
    pending_reviews = db.scalar(
        select(func.count()).select_from(ManualReview).where(ManualReview.audit_task_id == task.id, ManualReview.status == "PENDING")
    ) or 0
    # 类型不可用的材料位（待复核或已被人工确认不符）：报告里是同一个事实
    # 「该材料位的类型尚无可用依据」，但状态本身保持两种以便如实区分来源。
    material_type_mismatches = db.scalar(
        select(func.count())
        .select_from(TaskDocument)
        .where(TaskDocument.audit_task_id == task.id, TaskDocument.status.in_(MATERIAL_TYPE_UNAVAILABLE_STATUSES))
    ) or 0
    # 必传材料缺失与启用节点的失败/阻断同样属于“已配置范围未执行完整”的真实原因。
    required_missing_names = [
        document.material_name for document, upload in pairs if document.required and upload is None
    ]
    node_failures = [
        {
            "node_code": execution.node_code,
            "node_name": node_labels.get(execution.node_code, execution.node_code),
            "status": execution.status,
            "failure_code": execution.failure_code,
            "failure_message": execution.failure_message,
        }
        for execution in current_executions.values()
        # 报告输出节点自身在本次执行中仍是 RUNNING，不能因此把报告判成永远不完整。
        # 这里按**能力类型**解析出的实例码集合判断，不按固定实例码比较。
        if execution.node_code not in report_node_codes and execution.status in {"FAILED", "BLOCKED"}
    ]
    expected_risk_keys = {item["rule_key"] for item in risk_rules}
    risk_execution_ids = {
        current_executions[code].id for code in risk_node_codes if code in current_executions
    }
    risk_results = (
        db.scalars(
            select(RuleResult)
            .where(
                RuleResult.audit_task_id == task.id,
                RuleResult.node_execution_id.in_(risk_execution_ids),
                RuleResult.rule_key.in_(expected_risk_keys),
            )
            .order_by(RuleResult.id)
        ).all()
        if risk_configured and risk_execution_ids
        else []
    )
    if risk_configured and not risk_results:
        raise BusinessError("RISK_RESULT_MISSING", "已配置风险评级规则但未形成结果，不能生成无风险结论", status_code=409)
    produced_risk_keys = {item.rule_key for item in risk_results}
    missing_risk_keys = expected_risk_keys - produced_risk_keys
    risk_inconclusive = [item for item in risk_results if item.status in unfinished_rule_statuses]
    # 来源规则不适用的风险规则没有形成风险结论，它不算执行故障，但风险也不是“已判定”。
    risk_not_applicable = [item for item in risk_results if item.status == "NOT_APPLICABLE"]
    risk_conclusion_available = (
        risk_configured
        and not risk_inconclusive
        and not risk_not_applicable
        and not missing_risk_keys
        and len(risk_results) == len(risk_rules)
    )
    risk_level_by_key = {item["rule_key"]: item.get("parameters", {}).get("risk_level") for item in risk_rules}
    if not risk_configured:
        risk_label = "不可判定（本模板未配置风险评级规则，本次不输出“无风险”结论）"
    elif not risk_conclusion_available:
        risk_label = "部分不可判定（存在尚未形成结论或不适用的风险规则）"
    else:
        risk_label = "可判定（已配置的风险规则均已形成结论）"
    # 报告是否生成、已配置审核范围是否执行完整、风险是否可判定必须分开表达：
    # 未配置风险评级不是执行故障，COMPLETE 也不代表全部检查通过。
    # 完整性按“预期集合”核对：启用字段、启用规则、必传材料与启用节点都必须有有效结果。
    scope_execution_complete = not (
        unfinished_fields
        or unfinished_rules
        or pending_reviews
        or material_type_mismatches
        or required_missing_names
        or node_failures
    )
    status = "COMPLETE" if scope_execution_complete else "INCOMPLETE"
    # 人工复核只改变"有效结果"；系统原判断必须从保存的复核记录取回，不能只展示最终状态。
    resolved_reviews = db.scalars(
        select(ManualReview)
        .where(ManualReview.audit_task_id == task.id, ManualReview.status == "RESOLVED")
        .order_by(ManualReview.id)
    ).all()
    reviews_by_rule = {item.rule_result_id: item for item in resolved_reviews if item.rule_result_id is not None}
    reviews_by_field = {item.field_value_id: item for item in resolved_reviews if item.field_value_id is not None}
    node_executions = db.scalars(
        select(NodeExecution)
        .where(NodeExecution.audit_task_id == task.id, NodeExecution.status != "INVALIDATED")
        .order_by(NodeExecution.id)
    ).all()
    latest_by_node = {item.node_code: item for item in node_executions}
    uploads_by_id = {
        item.id: item
        for item in db.scalars(
            # document_upload 与 task_document 之间有两条外键，必须显式给出连接条件。
            select(DocumentUpload)
            .join(TaskDocument, TaskDocument.id == DocumentUpload.task_document_id)
            .where(TaskDocument.audit_task_id == task.id)
        ).all()
    }
    evidence_by_id = {
        item.id: item for item in db.scalars(select(Evidence).where(Evidence.audit_task_id == task.id)).all()
    }

    def effective_status_of(review: ManualReview) -> str | None:
        """该复核落到的业务对象当前状态：规则结果 / 字段事实 / 材料位。"""
        if review.rule_result_id is not None:
            result = db.get(RuleResult, review.rule_result_id)
            return result.status if result else None
        if review.field_value_id is not None:
            fact = db.get(TaskFieldValue, review.field_value_id)
            # 已完成复核指向的是当次裁决生成的不可变字段版本；报告展示当次
            # “复核后状态”，而不是用今天的当前事实改写历史记录。
            return fact.status if fact else None
        if review.document_upload_id is not None:
            upload = db.get(DocumentUpload, review.document_upload_id)
            document = db.get(TaskDocument, upload.task_document_id) if upload else None
            return document.status if document else None
        return None

    def review_detail(review: ManualReview) -> list[str]:
        """统一三部分：系统原判断（不可变快照）、人工结论、复核后有效状态。"""
        judgment = review.system_judgment_json if isinstance(review.system_judgment_json, dict) else {}
        original_status = judgment.get("status")
        original_message = judgment.get("message")
        original_value = judgment.get("raw_value")
        original = [str(status_labels.get(original_status, original_status or "未记录"))]
        if isinstance(original_message, str) and original_message:
            original.append(original_message)
        if original_value not in (None, ""):
            original.append(f"原值 {original_value}")
        evidence_refs = judgment.get("evidences")
        if not isinstance(evidence_refs, list) or not evidence_refs:
            evidence_ref = judgment.get("evidence")
            evidence_refs = [evidence_ref] if isinstance(evidence_ref, dict) else []
        for evidence_ref in evidence_refs:
            if not isinstance(evidence_ref, dict):
                continue
            locator = LOCATOR_STATUS_LABELS.get(evidence_ref.get("locator_status"), "状态未知")
            # 内容依据与位置分开说明：人工要看的是「这段原文还在不在材料里」，
            # 位置对不上不改变内容是否存在这件事。
            quote_check = evidence_ref.get("quote_check_label") or QUOTE_CHECK_LABELS.get(
                evidence_ref.get("quote_check"), "内容依据状态未知"
            )
            field_path = f"；字段属性：{evidence_ref['field_path']}" if evidence_ref.get("field_path") else ""
            original.append(
                f"证据 {evidence_ref.get('evidence_no') or '未记录'}"
                f"（内容依据：{quote_check}；位置：{locator}{field_path}）"
            )
        conclusion = [
            f"人工结论：{decision_labels.get(review.decision, review.decision or '未记录')}",
            f"依据类型：{BASIS_TYPE_LABELS.get(review.basis_type, review.basis_type or '未记录')}",
        ]
        if review.before_value not in (None, "") or review.after_value not in (None, ""):
            before = review.before_value if review.before_value not in (None, "") else "（空）"
            after = review.after_value if review.after_value not in (None, "") else "（空）"
            conclusion.append(f"改动：{before} → {after}")
        conclusion.append(f"业务说明：{review.business_note or '未填写'}")
        effective = effective_status_of(review)
        return [
            f"  - 系统原判断：{' · '.join(original)}",
            f"  - {' · '.join(conclusion)}",
            f"  - 复核后有效状态：{status_labels.get(effective, effective or '未记录')}",
        ]

    material_summary = (
        "、".join(f"{item['name']}{'' if item['required'] else '（按需）'}" for item in manifest["materials"]) or "无"
    )
    lines = [
        f"# {task.task_name}",
        "",
        "## 一、任务与模板版本",
        "",
        f"- 审核模板：{manifest.get('scene_name')}（模板标识 {scenario.scenario_code}）",
        f"- 模板版本：V{scenario.version_no}",
        # 配置来源必须如实标明：页面表单配置没有 Excel 工作簿，不显示一个不存在的格式版本。
        f"- 配置来源：{'页面表单配置' if manifest.get('source_kind') == 'FORM' else 'Excel 规则库导入'}",
        f"- Excel 格式版本：{manifest.get('profile_version') or '不适用（页面表单配置）'}",
        f"- 审核任务：{task.task_name}（任务编号 {task.task_no}）",
        f"- 报告生成时间：{datetime.now(UTC).isoformat(timespec='seconds')}",
        "- 报告生成状态：已生成。报告文件生成成功只表示报告已产出，不代表审核通过。",
        "",
        "## 二、材料清单",
        "",
    ]
    if not manifest["materials"]:
        lines.append("- 本模板没有定义材料位。")
    for material in manifest["materials"]:
        upload = uploads_by_material.get(material["material_key"])
        document = documents_by_material.get(material["material_key"])
        scope_label = "必传" if material["required"] else "按需"
        state_label = f"；材料位状态：{document_status_labels.get(document.status, document.status)}" if document else ""
        if upload is None:
            lines.append(f"- {material['name']}（{scope_label}）：未上传{state_label}")
        else:
            lines.append(
                f"- {material['name']}（{scope_label}）：已上传 {upload.original_filename}"
                f"（{upload.size_bytes} 字节）{state_label}"
            )
    lines.extend(["", "## 三、执行范围", ""])
    enabled_rule_keys = set(task.task_schema_json["enabled_rule_keys"])
    # 执行范围一节按**能力类型**归并：实例码是用户配置的，同一类型可能有多个实例，
    # 按实例码分行会让报告出现「同一类检查列两遍」而用户看不出它们是一类。
    rule_node_counts: dict[str, int] = {}
    for rule in manifest["rules"]:
        if rule["rule_key"] in enabled_rule_keys:
            node_type = rule_node_type_of(rule)
            rule_node_counts[node_type] = rule_node_counts.get(node_type, 0) + 1
    lines.append(f"- 材料范围：{material_summary}")
    lines.append(f"- 已启用规则：{len(enabled_rule_keys)} 条")
    for code, label in NODE_DEFINITIONS:
        if code in rule_node_counts:
            lines.append(f"  - {label}：{rule_node_counts[code]} 条")
    lines.append(f"- 任务字段：{len(task.task_schema_json['fields'])} 个")
    lines.extend(["", "节点执行情况：", ""])
    # 节点执行记录按**实例码**存储，报告按**能力类型**分行：先解析该类型的实例码集合。
    # 直接拿能力类型当实例码查会让报告永远显示「没有执行记录」。
    node_codes_by_type = {
        node_type: _node_codes_of_type(manifest, node_type) for node_type, _ in NODE_DEFINITIONS
    }
    for code, label in NODE_DEFINITIONS:
        execution = next(
            (latest_by_node[node_code] for node_code in node_codes_by_type[code] if node_code in latest_by_node),
            None,
        )
        if execution is None:
            continue
        if execution.status == "SKIPPED":
            reason = (execution.result_json or {}).get("skip_reason") if isinstance(execution.result_json, dict) else None
            lines.append(f"- {label}：未执行（{reason or '本模板未配置此类检查'}）")
        elif execution.status == "SUCCEEDED":
            lines.append(f"- {label}：已执行完成")
        elif execution.status == "FAILED":
            lines.append(f"- {label}：执行失败（{execution.failure_code or '未记录错误码'}：{execution.failure_message or '未记录原因'}）")
        elif execution.status == "BLOCKED":
            lines.append(f"- {label}：已阻断（{execution.failure_code or '未记录错误码'}：{execution.failure_message or '未记录原因'}）")
        else:
            lines.append(f"- {label}：{ruled_status_labels.get(execution.status, execution.status)}")
    # 报告说明按**能力类型**汇总（「本模板未配置这类检查」），不按实例码列节点。
    unconfigured = [
        code for code in manifest.get("unconfigured_check_node_types", []) if code in node_labels
    ]
    if unconfigured:
        lines.append(f"- 本模板未配置此类检查：{'、'.join(node_labels[code] for code in unconfigured)}")
        lines.append("- 未配置的检查类别不产生规则结果，本报告不会把它当成已通过。")
    lines.append("")
    lines.extend(["## 四、字段结果", ""])
    for field in task.task_schema_json["fields"]:
        key = (field["field_key"], field["material_key"])
        fact = fields.get(key)
        label = f"{material_names.get(field['material_key'], '未知材料')} · {field['name']}"
        if key in out_of_scope_field_keys:
            lines.append(f"- {label}：材料未上传，本次不在有效执行范围内（未抽取，也不代表字段为空）")
            continue
        if fact is not None and field_statuses.get(key) in {"PRESENT", "REVIEWED"}:
            lines.append(f"- {label}：{fact.raw_value}{_fact_evidence_suffix(evidence_by_id, fact)}")
        else:
            state = field_statuses.get(key, "NOT_EXECUTED")
            lines.append(f"- {label}：未形成有效结果（{field_status_labels.get(state, '状态未知')}）")
        review = reviews_by_field.get(fact.id) if fact else None
        if review is not None:
            lines.extend(review_detail(review))
    lines.append("")

    def rule_lines(result: RuleResult) -> list[str]:
        entry = f"- {result.rule_name}：{ruled_status_labels.get(result.status, result.status)}"
        if result.message:
            entry += f"（{result.message}）"
        output = [entry]
        payload = result.result_json if isinstance(result.result_json, dict) else {}
        standard = payload.get("standard") if isinstance(payload.get("standard"), dict) else None
        if standard:
            # 审核标准先于结论出现：只给结论看不到这条是照什么标准判的。
            standard_text = standard.get("requirement") or standard.get("missing")
            if standard_text:
                output.append(f"  - 审核标准：{standard_text}")
        trace = _input_trace(result.result_json, status_labels)
        if trace:
            output.append(f"  - 使用的内容：{trace}")
        comparison = payload.get("comparison") if isinstance(payload.get("comparison"), dict) else None
        if comparison:
            output.append(f"  - 实际比较：{comparison.get('expression')}")
            left = "，".join(
                str(value)
                for value in (comparison.get("left_label"), comparison.get("left_value"))
                if value not in (None, "")
            )
            right = "，".join(
                str(value)
                for value in (comparison.get("right_label"), comparison.get("right_value"))
                if value not in (None, "")
            )
            if left or right:
                output.append(f"  - 对比双方：{left} ／ {right}")
        notice = payload.get("material_type_notice")
        if notice:
            output.append(f"  - 待确认事项：{notice}")
        review = reviews_by_rule.get(result.id)
        if review is not None:
            # 系统原判断只从复核记录保存的真实初始快照读取，不把人工覆盖后的内容当成系统原话。
            output.extend(review_detail(review))
        return output

    lines.extend(["## 五、规则结果", ""])
    lines.append(
        f"已配置并形成结果的规则共 {len(rule_results)} 条：通过 {len(passed_rules)} 条，异常 {len(abnormal_rules)} 条，"
        f"其他状态（不适用、未执行、证据不足、待复核）{len(other_rules)} 条。"
    )
    lines.extend(["", "### 已完成并通过的检查", ""])
    if passed_rules:
        for result in passed_rules:
            lines.extend(rule_lines(result))
    else:
        lines.append("- 本次没有已完成并通过的检查。")
    lines.extend(["", "### 已完成但发现异常的检查", ""])
    if abnormal_rules:
        lines.append("- 以下检查技术执行已完成，但业务结论为异常：")
        for result in abnormal_rules:
            lines.extend(rule_lines(result))
    else:
        lines.append("- 本次没有发现异常的检查。")
    lines.extend(["", "### 未配置、不适用、未执行、证据不足与待复核", ""])
    if other_rules:
        for result in other_rules:
            lines.extend(rule_lines(result))
    else:
        lines.append("- 本次没有其他状态的规则。")
    lines.append("")
    lines.extend(["", "## 六、人工复核", ""])
    lines.append(f"- 待处理复核项：{pending_reviews} 项")

    def review_target(review: ManualReview) -> str:
        if review.rule_result_id is not None:
            result = db.get(RuleResult, review.rule_result_id)
            return f"规则“{result.rule_name if result else '未知规则'}”"
        if review.field_value_id is not None:
            fact = db.get(TaskFieldValue, review.field_value_id)
            if fact is None:
                return "字段“未知字段”"
            material = material_names.get(fact.material_key, fact.material_key)
            return f"字段“{material}·{field_name_by_key.get(fact.field_key, fact.field_key)}”"
        if review.document_upload_id is not None:
            upload = db.get(DocumentUpload, review.document_upload_id)
            document = documents_by_id.get(upload.task_document_id) if upload else None
            return f"材料“{document.material_name if document else '未知材料'}”"
        return "未关联业务对象"

    all_reviews = db.scalars(
        select(ManualReview).where(ManualReview.audit_task_id == task.id).order_by(ManualReview.id)
    ).all()
    if not all_reviews:
        lines.append("- 本次审核没有产生人工复核项。")
    for review in all_reviews:
        target = review_target(review)
        if review.status == "PENDING":
            judgment = review.system_judgment_json if isinstance(review.system_judgment_json, dict) else {}
            lines.append(f"- {target}：待人工复核（系统判断：{judgment.get('message') or '未记录'}）")
            continue
        if review.status == "INVALIDATED":
            lines.append(f"- {target}：复核记录已失效（对应结果已被重算或被替换材料取代，不作为本次结论）")
            continue
        lines.append(f"- {target}：")
        lines.extend(review_detail(review))
    lines.extend(["", "## 七、风险及不确定事项", "", "### 未配置风险评级", ""])
    if risk_configured:
        lines.append("- 本模板已配置风险评级规则。")
    else:
        lines.append("- 本模板未配置风险评级规则，本次审核未形成风险结论，不能据此认定无风险。")
    lines.extend(["", "### 已配置风险规则的实际结果", ""])
    if not risk_configured:
        lines.append("- 无已配置的风险规则，因此无法判定风险。")
    else:
        risk_abnormal = [item for item in risk_results if item.status == "ABNORMAL"]
        for item in risk_results:
            level = risk_level_by_key.get(item.rule_key)
            level_label = f"（风险等级 {level}）" if level else ""
            lines.append(
                f"- {item.rule_name}{level_label}：{risk_status_labels.get(item.status, item.status)}；{item.message}"
            )
        if missing_risk_keys:
            lines.append("- 以下风险规则尚未形成结果：" + "、".join(
                rule_name_by_key.get(key, key) for key in sorted(missing_risk_keys)
            ))
        if risk_conclusion_available and not risk_abnormal:
            lines.append("- 已配置的风险规则均未触发风险疑点。")
    lines.extend(["", "### 不确定事项与未完成项", ""])
    uncertain: list[str] = []
    if unfinished_field_names:
        uncertain.append("以下字段尚未形成有效结果：" + "、".join(unfinished_field_names))
    if unfinished_rules:
        uncertain.append(
            f"以下 {unfinished_rules} 条规则未执行、证据不足或待复核："
            + "、".join(
                f"{item.rule_name}（{ruled_status_labels.get(item.status, item.status)}）"
                for item in unfinished_rule_results
            )
            + (
                "；尚未形成任何结果的规则："
                + "、".join(rule_name_by_key.get(key, key) for key in missing_rule_keys)
                if missing_rule_keys
                else ""
            )
        )
    elif missing_rule_keys:
        uncertain.append(
            "以下已启用规则尚未形成任何结果："
            + "、".join(rule_name_by_key.get(key, key) for key in missing_rule_keys)
        )
    if required_missing_names:
        uncertain.append("以下必传材料尚未上传：" + "、".join(required_missing_names))
    if node_failures:
        uncertain.append(
            "以下审核节点执行失败或被阻断，相关范围的结论不完整："
            + "、".join(
                f"{item['node_name']}（{item['failure_code'] or item['status']}：{item['failure_message'] or '未记录原因'}）"
                for item in node_failures
            )
        )
    configured_gaps = [
        item for item in capability_gap_items if item.get("rule_key") in set(task.task_schema_json["enabled_rule_keys"])
    ]
    if configured_gaps:
        uncertain.append(
            "以下已配置检查缺少供应商提供的质量结论，本次不可判定（不默认通过）："
            + "、".join(f"{item.get('rule_name')}（{item.get('gap')}）" for item in configured_gaps)
        )
    if out_of_scope_rule_names:
        uncertain.append(
            "以下规则因可选材料未上传而不在本次执行范围内（未执行不代表通过）：" + "、".join(out_of_scope_rule_names)
        )
    if out_of_scope_field_keys:
        uncertain.append(
            "以下字段因可选材料未上传而不在本次执行范围内（不适用不代表通过）："
            + "、".join(
                f"{material_names.get(material_key, material_key)}·{field_name_by_key.get(field_key, field_key)}"
                for field_key, material_key in sorted(out_of_scope_field_keys)
            )
        )
    if material_type_mismatches:
        # 两种状态对下游的后果不同，报告里必须分开说：待复核不阻断普通审核，已确认不符才阻断。
        pending_names = [
            document.material_name
            for document, _ in pairs
            if read_material_type_status(document.status) == MATERIAL_TYPE_REVIEW
        ]
        rejected_names = [
            document.material_name
            for document, _ in pairs
            if read_material_type_status(document.status) == MATERIAL_TYPE_REJECTED
        ]
        if pending_names:
            uncertain.append(
                "以下材料位的类型待人工确认（不影响已按真实原文给出的审核结论）："
                + "、".join(pending_names)
            )
        if rejected_names:
            uncertain.append(
                "以下材料位已被人工确认与材料位不符，依赖它们的检查缺少有效依据："
                + "、".join(rejected_names)
            )
    if not risk_configured:
        uncertain.append("本模板未配置风险评级规则，本次风险不可判定。")
    elif risk_inconclusive:
        uncertain.append(
            "以下风险规则尚未形成结论，不能据此认定无风险："
            + "、".join(item.rule_name for item in risk_inconclusive)
        )
    if risk_not_applicable:
        uncertain.append(
            "以下风险规则因来源规则不适用而未形成风险结论："
            + "、".join(item.rule_name for item in risk_not_applicable)
        )
    if not scope_execution_complete:
        uncertain.append("本次已配置的审核范围未全部执行完成，报告不能作为无风险结论。")
    if uncertain:
        lines.extend(f"- {item}" for item in uncertain)
    else:
        lines.append("- 无。")
    lines.extend(["", "## 八、证据引用", ""])
    lines.append(
        "说明：字段取值为系统生成的结果摘要；“引用原文”一栏才是文档引用原文。"
        "内容依据与位置分开核对：位置（页码、坐标）只是辅助信息，缺失或对不上不影响"
        "原文本身可用于审核；只有引文在所属材料里确实找不到时，字段内容才按缺少原文依据处理。"
    )
    lines.append("")
    fact_by_id = {item.id: item for item in fields.values()}
    rule_result_by_id = {item.id: item for item in rule_results}
    evidence_lines: list[str] = []
    missing_quote_count = 0
    for evidence in evidence_by_id.values():
        if evidence.document_upload_id is not None and evidence.document_upload_id not in uploads_by_id:
            # 旧上传版本的证据不再进入当前报告。
            continue
        if evidence.rule_result_id is not None:
            result = rule_result_by_id.get(evidence.rule_result_id)
            if result is None:
                continue
            purpose = f"规则“{result.rule_name}”"
        elif evidence.field_value_id is not None:
            fact = fact_by_id.get(evidence.field_value_id)
            if fact is None or not fact.is_current:
                continue
            field_path = f"的属性“{evidence.field_path}”" if evidence.field_path else ""
            purpose = f"字段“{field_name_by_key.get(fact.field_key, fact.field_key)}”{field_path}"
        else:
            continue
        upload = uploads_by_id.get(evidence.document_upload_id) if evidence.document_upload_id else None
        document = documents_by_id.get(upload.task_document_id) if upload else None
        # 内容依据在前、位置在后，并明确标注位置是辅助信息：工作人员先要能核对“这段话在不在”。
        quote_check = _quote_check_of(db, evidence)
        quote_check_label = QUOTE_CHECK_LABELS.get(quote_check, "内容依据状态未知")
        page_label = f"第 {evidence.page_no} 页" if evidence.page_no else "未标注页码"
        bbox_label = f"坐标 {evidence.bbox_json}" if evidence.bbox_json else "未提供坐标"
        locator_label = LOCATOR_STATUS_LABELS.get(evidence.locator_status, "状态未知")
        locator_note = f"（{evidence.locator_note}）" if evidence.locator_note else ""
        quote = (evidence.quote_text or "").strip()
        if not quote:
            missing_quote_count += 1
            quote = "未记录文档引用原文"
        evidence_lines.append(
            f"- {evidence.evidence_no} ｜ 材料：{document.material_name if document else '未关联材料'}"
            f" ｜ 文件：{upload.original_filename if upload else '未关联文件'}"
            f" ｜ 内容依据核对：{quote_check_label}"
            f" ｜ 位置（辅助）：{page_label}、{bbox_label}、{locator_label}{locator_note}"
            f" ｜ 用于：{purpose} ｜ 引用原文：{quote}"
        )
    if evidence_lines:
        lines.extend(sorted(evidence_lines))
    else:
        lines.append("- 本次没有可引用的文档证据记录。")
    if missing_quote_count:
        lines.append(f"- 其中 {missing_quote_count} 条证据缺少引用原文，已明确标注，未进行推断。")
    lines.extend(["", "## 九、附加展示章节（来自报告模板）", ""])
    report_template = manifest.get("report_template") or []
    report_groups = manifest.get("report_groups") or {}
    grouped_reports = report_groups.get("reports") if isinstance(report_groups, dict) else None
    # 配置声明但**当前没有文件生成实现**的输出格式：必须在报告正文里写清楚，
    # 否则「模板要求 PDF、系统只产 Markdown」这件事只在配置 JSON 里躺着，页面与报告都看不见。
    format_support: list[dict[str, Any]] = []
    for report in grouped_reports or []:
        for entry in report.get("format_support") or []:
            format_support.append(
                {
                    "format": entry.get("format"),
                    "supported": bool(entry.get("supported")),
                    "message": entry.get("message") or "",
                    "report_code": report.get("report_code"),
                    "report_name": report.get("report_name"),
                }
            )
    if isinstance(grouped_reports, list) and report_groups.get("source_kind") == "REPORT_CODES":
        # 节点版按报告编号分组的报告配置：逐份报告、逐章节渲染。
        # 全部章节停用的报告不输出；非停用章节待确认时该报告标为未完成配置。
        lines.append(
            "以下报告由审核模板的报告配置指定：报告编号、报告名称、材料范围、输出格式、"
            "结果排序与章节顺序都来自已发布配置。"
        )
        lines.append("")
        lines.extend(_render_report_groups(db, task, manifest, grouped_reports))
    elif report_template:
        lines.append("以下章节由审核模板的报告配置指定，属于附加展示内容。")
        lines.append("")
        field_defs = {item["field_key"]: item for item in manifest["fields"]}
        for section in report_template:
            lines.extend([f"### {section['section_name']}", ""])
            if section.get("content_instruction"):
                # 内容要求只是展示说明，不能被当作已经生成的正文。
                lines.append(f"- 本节内容要求（展示说明，不代表已生成正文）：{section['content_instruction']}")
            bound_field_keys = list(section.get("field_keys", []))
            if not bound_field_keys:
                lines.append("- 本节未形成内容（本模板未为本节绑定字段，系统不代为生成正文）。")
            for field_key in bound_field_keys:
                field_definition = field_defs[field_key]
                key = (field_key, field_definition["material_key"])
                fact = fields.get(key)
                label = f"{material_names.get(field_definition['material_key'], '未知材料')} · {field_definition['name']}"
                if key in out_of_scope_field_keys:
                    lines.append(f"- {label}：材料未上传，本次不在有效执行范围内")
                elif fact is not None and field_statuses.get(key) in {"PRESENT", "REVIEWED"}:
                    lines.append(f"- {label}：{fact.raw_value}{_fact_evidence_suffix(evidence_by_id, fact)}")
                elif fact is not None and field_statuses.get(key) == "NOT_PRESENT" and fact.id in confirmed_missing_field_ids:
                    lines.append(f"- {label}：人工确认文档中不存在该字段")
                else:
                    state = field_statuses.get(key, "NOT_EXECUTED")
                    lines.append(f"- {label}：未形成有效结果（{field_status_labels.get(state, '状态未知')}）")
            lines.append("")
    else:
        lines.append("- 本模板未配置附加报告章节，以上即为系统固定事实章节。")
        lines.append("")
    lines.extend(["## 十、报告结论", ""])
    lines.append("- 报告是否生成：已生成。报告生成成功不代表审核通过。")
    incompleteness_reasons = []
    if unfinished_fields or missing_rule_keys or unfinished_rule_results:
        incompleteness_reasons.append("存在未形成有效结果的字段或规则")
    if pending_reviews:
        incompleteness_reasons.append("存在待复核项")
    if material_type_mismatches:
        incompleteness_reasons.append("存在材料类型尚未定案或已确认与材料位不符的材料位")
    if required_missing_names:
        incompleteness_reasons.append("存在未上传的必传材料")
    if node_failures:
        incompleteness_reasons.append("存在执行失败或被阻断的节点")
    lines.append(
        "- 已配置审核范围是否执行完整："
        + (
            "已完整执行。"
            if scope_execution_complete
            else "未完整执行（" + "、".join(incompleteness_reasons or ["存在未完成项"]) + "）。"
        )
    )
    lines.append(f"- 风险是否可判定：{risk_label}")
    # 未实现的输出格式与能力缺口必须在正文里也能读到，不能只在配置 JSON 里。
    unimplemented_formats = [item for item in format_support if not item["supported"]]
    if unimplemented_formats:
        lines.append(
            "- 报告输出格式：本节报告声明了尚未实现的输出格式（"
            + "、".join(
                f"{item['report_name'] or item['report_code'] or '本报告'} 要求 {item['format']}"
                for item in unimplemented_formats
            )
            + "）；系统当前只生成 Markdown 正文，不会按这些格式产出文件，也不会改扩展名冒充。"
        )
    configured_gaps = [
        item for item in capability_gap_items if item.get("rule_key") in set(task.task_schema_json["enabled_rule_keys"])
    ]
    if configured_gaps:
        lines.append(
            "- 能力未实现：以下已启用检查引用的能力当前没有判定实现，本次不给结论"
            "（它们不是「证据不足」，也不是通过）："
            + "、".join(
                f"{item.get('rule_name')}（{item.get('capability_id') or item.get('gap')}）" for item in configured_gaps
            )
        )
    # 报告范围内的真实快照：写入报告规则结果，供 report_view 与任务当前输入修订对账。
    snapshot = {
        "task_input_revision": task.input_revision,
        "upload_ids": sorted(upload.id for _, upload in pairs if upload is not None),
        "field_value_ids": sorted(item.id for item in fields.values()),
        "review_ids": sorted(item.id for item in all_reviews),
        "rule_result_ids": sorted(item.id for item in rule_results),
    }
    # 报告正文只落 audit_report.content_md——它是正文的唯一存放处，报告页与管理端都从这里读。
    # 执行结果（node_execution.result_json，同时作为操作结果回传）只带结论与信号：
    # 正文有几百行，塞进执行结果后会被页面当成「结果摘要」逐项铺开成一条无换行的长文本。
    report_body = "\n".join(lines)
    report_result = {
        "report_status": status,
        "content_type": "text/markdown",
        # 报告是否生成、已配置审核范围是否执行完整、风险是否可判定分别表达。
        "report_generated": True,
        "scope_execution_complete": scope_execution_complete,
        "risk_conclusion_available": risk_conclusion_available,
        # 报告只对应当前有效事实与执行结果；input_revision 用于判断报告是否已过期。
        "results_scope": "CURRENT_RUN",
        "input_revision": task.input_revision,
        "snapshot": snapshot,
        # 结论同时以**结构化键**给出：报告正文里的中文字面量是给人读的，
        # 页面不应靠匹配「## 十、」这类行首文案来提取结论——正文任何一次措辞调整
        # 都会让页面静默退化成「没有结论」，且不报错。
        "conclusion": {
            "report_generated": True,
            "scope_execution_complete": scope_execution_complete,
            "scope_execution_note": (
                "已完整执行。"
                if scope_execution_complete
                else "未完整执行（" + "、".join(incompleteness_reasons or ["存在未完成项"]) + "）。"
            ),
            "risk_conclusion_available": risk_conclusion_available,
            "risk_note": risk_label,
            "unimplemented_formats": [
                {
                    "format": item["format"],
                    "report_code": item["report_code"],
                    "report_name": item["report_name"],
                    "message": item["message"],
                }
                for item in unimplemented_formats
            ],
            "capability_gaps": [
                {
                    "rule_name": item.get("rule_name"),
                    "capability_id": item.get("capability_id"),
                    "gap": item.get("gap"),
                    "message": item.get("message"),
                }
                for item in configured_gaps
            ],
        },
        # 本节报告声明的输出格式与真实支持状态：随报告一起保存，页面据此显示格式缺口。
        "format_support": format_support,
        # 系统固定事实章节之外的业务附加章节：零业务章节不等于零报告，也不等于范围完整。
        "business_section_count": (
            len(report_template)
            if not (isinstance(grouped_reports, list) and report_groups.get("source_kind") == "REPORT_CODES")
            else sum(len(item.get("sections") or []) for item in grouped_reports)
        ),
        "reasons": {
            "unfinished_fields": unfinished_fields,
            "unfinished_rules": unfinished_rules,
            "pending_reviews": pending_reviews,
            "material_type_mismatches": material_type_mismatches,
            # 未配置、不可判定、节点失败、缺必传材料与预期结果缺失必须分开表达。
            "risk_not_configured": not risk_configured,
            "risk_inconclusive": bool(risk_inconclusive or risk_not_applicable),
            "missing_rule_keys": missing_rule_keys,
            "missing_required_materials": required_missing_names,
            "failed_nodes": [item["node_code"] for item in node_failures],
        },
    }
    for rule in report_rules:
        db.add(
            RuleResult(
                audit_task_id=task.id,
                node_execution_id=execution.id,
                rule_key=rule["rule_key"],
                rule_name=rule["name"],
                # 报告文件生成成功只表示报告已产出，不代表审核通过。
                status="PASSED",
                message="报告已生成" + ("" if scope_execution_complete else "；本次已配置的审核范围未全部执行完成"),
                result_json={
                    "report_generated": True,
                    "report_status": status,
                    "scope_execution_complete": scope_execution_complete,
                    "risk_conclusion_available": risk_conclusion_available,
                    "input_revision": task.input_revision,
                    # 报告生成时刻的真实范围快照：随报告规则结果一起保留，作为这条「报告已生成」
                    # 结论的生成依据。report_view 的对账读执行结果里同一份快照。
                    "snapshot": snapshot,
                },
            )
        )
    # 报告快照落库：报告列表/详情/归档基于真实生成的快照记录。
    # 正文与三信号、范围快照、结构化结论都来自本次执行结果；重新生成是新快照，历史快照保留。
    # 正文只写在这里：执行结果不回传它，否则页面会把整篇正文当成执行摘要铺开。
    db.add(
        AuditReport(
            report_no=public_no("RPT"),
            audit_task_id=task.id,
            node_execution_id=execution.id,
            report_name=f"{task.task_name} 审核报告",
            status="GENERATED",
            content_type="text/markdown",
            content_md=report_body,
            report_generated=True,
            scope_execution_complete=scope_execution_complete,
            risk_conclusion_available=risk_conclusion_available,
            reasons_json=report_result["reasons"],
            snapshot_json=snapshot,
            input_revision=task.input_revision,
            origin="NODE",
            generated_at=datetime.now(UTC),
        )
    )
    return report_result

def _configured_format_support(task: AuditTask) -> list[dict[str, Any]]:
    """任务绑定版本里声明的报告输出格式与各自的真实支持状态。

    声明（业务期望）与实现（系统能否真的产出该格式文件）必须分开表达：
    当前只生成 Markdown 正文，声明 PDF / Word / Excel 的报告在这里如实标为未实现。
    """
    manifest = task.manifest_json
    groups = manifest.get("report_groups") or {}
    return [
        entry
        for report in (groups.get("reports") or [])
        for entry in (report.get("format_support") or [])
    ]
