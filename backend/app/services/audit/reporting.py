"""报告生成：按场景业务章节生成正文，并保留系统执行摘要。

报告必须同时、分开表达三件事，不能用一句「成功」代替：报告是否生成；已配置的审核范围是否
执行完整（按预期集合核对启用字段、启用规则、必传材料、启用节点都必须有有效结果）；风险是否
可判定（未配置风险评级不是执行故障，但也不能据此认定无风险）。

结论同时以结构化键写入执行结果，页面不靠匹配正文行首文案提取结论——正文任何一次措辞调整
都会让页面静默退化成「没有结论」。

正文只落 `audit_report.content_md`：执行结果会作为操作结果回传，把几百行正文塞进去会被页面
当成「结果摘要」铺开成一条无换行的长文本。"""

from .. import task_csv_service
from ...domain import fact_snapshot, workflow_dag
from ...domain.audit_vocab import MATERIAL_TYPE_UNAVAILABLE_STATUSES
from ...domain.identity import public_no
from ...domain.manifest import enabled_extraction_field_keys, rule_node_type_of
from .state import _node_codes_of_type
from ...common.exceptions import BusinessError
from ...entity import AuditReport, AuditTask, ManualReview, NodeExecution, RuleResult, TaskDocument
from .facts import _current_field_fact_pairs
from .shared import _current_uploads, _material_names
from .state import _current_node_executions, _risk_rule_refs, _scope_results, _result_material_keys
from ..llm_report import generate_report_sections
from datetime import UTC, datetime
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from typing import Any


def _sort_report_results(results: list[Any], group: dict[str, Any]) -> list[Any]:
    """按已配置的材料顺序和等级顺序排列，不按等级文字猜高低。"""
    mode = group.get("result_sort")
    if not mode:
        return results
    materials = {key: index for index, key in enumerate(group.get("material_keys") or [])}
    levels = {value: index for index, value in enumerate(group.get("risk_level_order") or [])}
    if mode in {"RISK_LEVEL_DESC", "MATERIAL_ORDER_RISK_LEVEL_DESC"}:
        if not levels:
            raise BusinessError("REPORT_LEVEL_ORDER_REQUIRED", "报告按风险等级排序，但场景缺少等级顺序")
        unknown = sorted({str(item.result_json["risk_level"]) for item in results
                          if item.result_json.get("risk_level") and item.result_json["risk_level"] not in levels})
        if unknown:
            raise BusinessError("REPORT_LEVEL_ORDER_INCOMPLETE", "报告等级顺序未包含实际结果中的等级", details={"levels": unknown})
    def key(item: Any) -> tuple[int, ...]:
        material = min((materials[value] for value in _result_material_keys(item) if value in materials), default=len(materials))
        level = levels.get(item.result_json.get("risk_level"), len(levels))
        if mode == "RISK_LEVEL_DESC":
            return (level,)
        if mode == "MATERIAL_ORDER":
            return (material,)
        if mode == "MATERIAL_ORDER_RISK_LEVEL_DESC":
            return (material, level)
        raise BusinessError("REPORT_SORT_INVALID", "报告排序方式不受支持", details={"result_sort": mode})
    return sorted(results, key=key)


def _render_report_groups(
    db: Session, task: AuditTask, manifest: dict[str, Any], groups: list[dict[str, Any]],
    task_execution: dict[str, Any],
) -> list[str]:
    """按场景章节生成报告正文，真实数据先筛选、排序，再交给共用模型工具。"""
    material_names = _material_names(manifest)
    field_defs = {item["field_key"]: item for item in manifest["fields"]}
    pairs = _current_field_fact_pairs(db, task)
    executions = _current_node_executions(db, task)
    execution_ids = {item.id for item in executions.values()}
    node_by_id = {item.id: item.node_code for item in executions.values()}
    risk_refs = _risk_rule_refs(manifest)
    results = list(db.scalars(select(RuleResult).where(
        RuleResult.audit_task_id == task.id, RuleResult.node_execution_id.in_(execution_ids),
    ).order_by(RuleResult.id)).all()) if execution_ids else []
    results = task_csv_service.from_index(db, task, RuleResult, results)
    report_nodes = set(_node_codes_of_type(manifest, "REPORT_OUTPUT"))
    results = [item for item in results if node_by_id[item.node_execution_id] not in report_nodes]
    reviews = list(db.scalars(select(ManualReview).where(
        ManualReview.audit_task_id == task.id, ManualReview.status != "INVALIDATED",
    ).order_by(ManualReview.id)).all())

    def review_payloads(*, field_value_id: int | None = None, rule_result_id: int | None = None) -> list[dict[str, Any]]:
        return [
            {"status": item.status, "decision": item.decision,
             "system_judgment": item.system_judgment_json, "basis_type": item.basis_type,
             "before_value": item.before_value, "after_value": item.after_value,
             "business_note": item.business_note}
            for item in reviews
            if ((field_value_id is not None and item.field_value_id == field_value_id)
                or (rule_result_id is not None and item.rule_result_id == rule_result_id))
        ]
    lines: list[str] = []
    for group in groups:
        sections = [item for item in group.get("sections", []) if item.get("config_status") != "DISABLED"]
        if not sections:
            continue
        scope = list(group.get("material_keys") or [])
        # 没有分组材料范围的章节使用其显式字段与任务结果；不代填某类材料。
        section_payloads = []
        for index, section in enumerate(sections):
            section_scope = list(section.get("material_keys", scope))
            sorting = {**group, "result_sort": section.get("result_sort", group.get("result_sort")),
                       "risk_level_order": section.get("risk_level_order", group.get("risk_level_order")),
                       "material_keys": section_scope}
            sources: list[dict[str, Any]] = []
            unattributed = 0
            content_type = section.get("content_type")
            if content_type is None or content_type in {"FIELD_DETAIL", "FIELD_AND_RESULT"}:
                for field_key in section.get("field_keys") or []:
                    definition = field_defs[field_key]
                    pair = pairs.get((field_key, definition["material_key"]))
                    sources.append({
                        "source_ref": "field:" + field_key, "kind": "FIELD",
                        "name": definition["name"], "material_name": material_names[definition["material_key"]],
                        "status": pair[1].status if pair else "NOT_EXECUTED",
                        "raw_value": pair[0].raw_value if pair else None,
                        "normalized_value": fact_snapshot.task_fact_value(pair[1], field_definition=definition)
                        if pair and pair[1].status in {"PRESENT", "REVIEWED"} else None,
                        "unit": definition.get("unit"), "currency": definition.get("currency"),
                        "manual_reviews": review_payloads(field_value_id=pair[0].id) if pair else [],
                    })
            if content_type is None or content_type in {"AUDIT_RESULT", "FIELD_AND_RESULT", "RISK_LIST", "CONCLUSION"}:
                candidates = [item for item in results if
                    ((node_by_id[item.node_execution_id], item.rule_key) in risk_refs)
                    == (content_type == "RISK_LIST") or content_type in {None, "CONCLUSION"}]
                if section_scope:
                    inside, related, unattributed = _scope_results(candidates, section_scope, material_names)
                    selected_ids = {item.id for item in inside} | {item.id for item, _ in related}
                    candidates = [item for item in candidates if item.id in selected_ids]
                    outside = {item.id: names for item, names in related}
                else:
                    unattributed = 0
                    outside = {}
                for item in _sort_report_results(candidates, sorting):
                    sources.append({
                        "source_ref": f"result:{item.id}", "kind": "RESULT", "name": item.rule_name,
                        "status": item.status, "message": item.message, "result": item.result_json,
                        "related_materials_outside_scope": outside.get(item.id, []),
                        "manual_reviews": review_payloads(rule_result_id=item.id),
                    })
            section_payloads.append({
                "section_index": index, "section_name": section["section_name"],
                "content_type": content_type, "content_instruction": section.get("content_instruction") or "",
                "config_status": section.get("config_status"), "sources": sources,
                "unattributed_result_count": unattributed,
                "material_names": [material_names[key] for key in section_scope],
                "result_sort": sorting.get("result_sort"),
                "risk_level_order": sorting.get("risk_level_order"),
            })
        generated = generate_report_sections.invoke({
            "report": {"report_name": group.get("report_name"),
                       "material_names": [material_names[key] for key in scope],
                       "result_sort": group.get("result_sort"), "risk_level_order": group.get("risk_level_order"),
                       "task_execution": task_execution},
            "sections": section_payloads,
        })
        lines.extend([f"## {group['report_name']}", ""])
        for configured, payload, section in zip(sections, section_payloads, generated["sections"], strict=True):
            lines.extend([f"### {configured['section_name']}", ""])
            if section["introduction_md"]:
                lines.extend([section["introduction_md"], ""])
            sources_by_ref = {source["source_ref"]: source for source in payload["sources"]}
            for entry in section["entries"]:
                source = sources_by_ref[entry["source_ref"]]
                # 标签及状态来自真实结果，模型只组织正文，不能改写审核结论。
                lines.extend([f"- {source['name']}：{_report_status_label(source['status'])}", entry["content_md"], ""])
                result = source.get("result") or {}
                if result.get("risk_level"):
                    lines.append(f"  风险等级：{result['risk_level']}")
                if result.get("action"):
                    lines.append(f"  处理建议：{result['action']}")
            if section["conclusion_md"]:
                lines.extend([section["conclusion_md"], ""])
    return lines


def _report_status_label(status: str) -> str:
    return {"PASSED": "合格", "ABNORMAL": "异常", "COMPUTED": "已计算",
            "EVIDENCE_INSUFFICIENT": "依据不足", "PENDING_REVIEW": "待复核",
            "NOT_APPLICABLE": "不适用", "NOT_EXECUTED": "未执行",
            "PRESENT": "已抽取", "REVIEWED": "已确认", "NOT_PRESENT": "未找到"}.get(status, status)


def _report_output(db: Session, task: AuditTask, manifest: dict[str, Any], execution: NodeExecution) -> dict[str, Any]:
    enabled_keys = set(task.task_schema_json["enabled_rule_keys"])
    extraction_field_keys = enabled_extraction_field_keys(
        manifest, task.task_schema_json["enabled_rule_keys"],
    )
    report_rules = [
        item
        for item in manifest["rules"]
        if rule_node_type_of(item) == "REPORT_OUTPUT" and item["operator"] == "REPORT_RENDER" and item["rule_key"] in enabled_keys
    ]
    if not report_rules:
        # 本任务没有选入报告生成规则：显式跳过，不报错。
        #
        # 方案是用户自己的范围选择——勾了什么就跑什么。没勾报告生成就没有报告，
        # 这正是这份方案表达的意思，不该让任务在最后一个节点整个失败。
        # 跳过必须写明原因：「没有报告」不等于「审核通过」，也不等于
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
    # 报告只取当前有效执行批次：每个节点取最新一条未失效执行。
    # 节点执行由 DAG 依赖决定，sequence_no 只用于展示排序，不能作为结果范围边界。
    current_executions = _current_node_executions(db, task)
    node_plans = workflow_dag.plans_of(manifest)
    node_names = {plan.node_instance_code: plan.label for plan in node_plans}
    current_execution_ids = {item.id for item in current_executions.values()}
    execution_node_by_id = {item.id: item.node_code for item in current_executions.values()}
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
    # 报告输出节点按能力类型解析实例码：`node_code` 是用户配置的实例码，
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
    material_names = {item["material_key"]: item["name"] for item in manifest["materials"]}
    material_defs = {item["material_key"]: item for item in manifest["materials"]}
    unfinished_rule_statuses = {"EVIDENCE_INSUFFICIENT", "NOT_EXECUTED", "PENDING_REVIEW"}
    pairs = _current_uploads(db, task)
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
        reason = (
            "本方案未启用自动抽取，当前没有有效值"
            if fact is None and field["field_key"] not in extraction_field_keys
            else field_status_labels.get(state, state)
        )
        unfinished_field_names.append(
            f"{material_names.get(field['material_key'], field['material_key'])}·{field['name']}"
            f"（{reason}）"
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
    current_rule_results = task_csv_service.from_index(db, task, RuleResult, current_rule_results)
    rule_results = [item for item in current_rule_results if item.node_execution_id not in output_execution_ids]
    # 只有输出字段全部落在“可选材料未上传”范围内的抽取规则才算出执行范围（6.3）：
    # 它们不是执行故障，但也绝不等于通过，会在不确定事项里单独列出。
    field_material_by_key = {item["field_key"]: item["material_key"] for item in manifest["fields"]}
    extraction_outputs = {
        (rule["node"], rule["rule_key"]): list(rule.get("outputs", []))
        for rule in manifest["rules"]
        if rule_node_type_of(rule) == "FIELD_EXTRACTION" and rule["operator"] == "LLM_EXTRACT"
    }

    def rule_out_of_scope(result: RuleResult) -> bool:
        outputs = extraction_outputs.get((execution_node_by_id[result.node_execution_id], result.rule_key))
        if not outputs:
            return False
        return all((key, field_material_by_key.get(key)) in out_of_scope_field_keys for key in outputs)

    # 预期集合：任务实际启用范围里的每条规则都必须形成结果，不能只统计已有结果里的未完成项。
    # 报告输出规则由本节点自身产出，不进入“预期结果缺失”的判断。
    # 按能力类型判断，不按实例码：`node` 是用户配置的实例码。
    expected_rule_refs = {
        (item["node"], item["rule_key"])
        for item in manifest["rules"]
        if item["rule_key"] in enabled_keys and rule_node_type_of(item) != "REPORT_OUTPUT"
    }
    enabled_node_codes = {
        node["node_code"] for node in manifest["nodes"] if node.get("enabled", True)
    }
    calculation_scope = [
        item for item in manifest.get("calculations", [])
        if item.get("status") != "DISABLED" and item["node_instance_code"] in enabled_node_codes
    ]
    expected_rule_refs.update((item["node_instance_code"], item["rule_code"]) for item in calculation_scope)
    rule_name_by_ref = {
        (item["node"], item["rule_key"]): item["name"] for item in manifest["rules"]
    }
    rule_name_by_ref.update({
        (item["node_instance_code"], item["rule_code"]): item["rule_name"]
        for item in manifest.get("calculations", [])
    })
    produced_rule_refs = {
        (execution_node_by_id[item.node_execution_id], item.rule_key) for item in rule_results
    }
    missing_rule_refs = sorted(expected_rule_refs - produced_rule_refs)
    missing_rule_keys = [key for _, key in missing_rule_refs]
    unfinished_rule_results = [
        item for item in rule_results if item.status in unfinished_rule_statuses and not rule_out_of_scope(item)
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
            "node_name": node_names[execution.node_code],
            "status": execution.status,
            "failure_code": execution.failure_code,
            "failure_message": execution.failure_message,
        }
        for execution in current_executions.values()
        # 报告输出节点自身在本次执行中仍是 RUNNING，不能因此把报告判成永远不完整。
        # 这里按能力类型解析出的实例码集合判断，不按固定实例码比较。
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
    risk_results = task_csv_service.from_index(db, task, RuleResult, risk_results)
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
    if not risk_configured:
        risk_label = "不可判定（当前场景未配置风险评级规则，本次不输出“无风险”结论）"
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
    all_reviews = db.scalars(select(ManualReview).where(ManualReview.audit_task_id == task.id)).all()
    report_template = manifest.get("report_template") or []
    report_groups = manifest.get("report_groups") or {}
    grouped_reports = report_groups.get("reports") or []
    format_support = [
        {"format": entry.get("format"), "supported": bool(entry.get("supported")),
         "message": entry.get("message") or "", "report_code": report.get("report_code"),
         "report_name": report.get("report_name")}
        for report in grouped_reports for entry in report.get("format_support") or []
    ]
    lines = [f"# {task.task_name} 审核报告", ""]
    if grouped_reports:
        lines.extend(_render_report_groups(db, task, manifest, grouped_reports, {
            "scope_execution_complete": scope_execution_complete,
            "unfinished_fields": unfinished_field_names,
            "missing_results": [rule_name_by_ref[ref] for ref in missing_rule_refs],
            "unfinished_results": [{"name": item.rule_name, "status": item.status, "message": item.message}
                                   for item in unfinished_rule_results],
            "pending_review_count": pending_reviews,
            "unavailable_material_type_count": material_type_mismatches,
            "required_missing_materials": required_missing_names,
            "node_failures": node_failures,
            "risk_configured": risk_configured,
            "risk_conclusion_available": risk_conclusion_available,
        }))
    else:
        lines.extend(["本场景未配置业务报告章节。", ""])
    lines.extend(["## 系统执行摘要", ""])
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
        # 结论同时以结构化键给出：报告正文里的中文字面量是给人读的，
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
        # 业务章节数量与执行完整性分别记录。
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
