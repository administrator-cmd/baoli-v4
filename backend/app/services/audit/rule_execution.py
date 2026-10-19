"""通用规则循环的真实输入与产出持久化，不按业务节点类型分发。"""
from datetime import UTC, datetime
import json
from typing import Any

from sqlalchemy import or_, select

from ...common.exceptions import BusinessError
from ...domain.audit_vocab import COMPLETED_RESULT_STATUSES, CONCLUSIVE_RESULT_STATUSES, PRODUCED_RESULT_STATUSES
from ...domain.identity import public_no
from ...domain import fact_projection
from ...domain.report_data import report_data_refs, report_format_support
from ...domain.rule_library import is_risk_rule
from ...entity import AuditReport, DocumentUpload, NodeExecution, TaskDocument, TaskFact, TaskFieldValue, RuleResult
from . import value_store as task_value_service


def evaluation_context(db, task, rule) -> dict[str, Any]:
    from .fact_context import extracted_identity_filter, project_task_facts
    codes = set(rule.get("source_rule_codes") or [])
    calculation = rule.get("calculation_definition") or {}
    codes.update(calculation.get("depends_on_result_codes") or [])
    codes.update(p["source_code"] for p in calculation.get("parameters") or [] if p.get("source_type") == "CALC_RESULT" and p.get("source_code"))
    # 引用声明来自完整冻结配置；未选中/停用字段仍可说明身份，不能当成整批程序异常。
    fields = {f["field_key"]: f for f in task.manifest_json["fields"]}
    field_keys = {item["field_key"] for item in rule.get("inputs") or []}
    field_keys.update(f["field_key"] for f in rule.get("output_definitions") or [])
    # 计算 FIELD 参数已由编译器解析到 inputs，不把原始业务编号再次当成内部字段键。
    if field_keys - fields.keys():
        raise BusinessError("RULE_INPUT_FIELD_UNDECLARED", "规则引用的字段不属于任务冻结的字段身份", status_code=500,
                            details={"field_keys": sorted(field_keys - fields.keys())})
    definitions = {key: fields[key] for key in field_keys}
    sources = set(rule.get("source_rule_keys") or [])
    if rule.get("source_rule_key"):
        sources.add(rule["source_rule_key"])
    selected = set(task.task_schema_json["enabled_rule_keys"])
    # 同一技术容器内以执行批次+规则身份定位，当前调用看到真实已保存的上游结果。
    current_execution = db.scalar(select(NodeExecution.id).where(NodeExecution.audit_task_id == task.id, NodeExecution.status != "INVALIDATED").order_by(NodeExecution.id.desc()).limit(1))
    report = rule.get("kind") == "REPORT"
    fact_query = select(TaskFact).where(TaskFact.audit_task_id == task.id, TaskFact.is_current.is_(True))
    result_query = select(RuleResult).where(RuleResult.audit_task_id == task.id,
                                         RuleResult.rule_key.in_(selected), RuleResult.node_execution_id == current_execution)
    if not report:
        fact_query = fact_query.where(or_(extracted_identity_filter(definitions),
                                         (TaskFact.fact_kind == "DERIVED") & TaskFact.fact_code.in_(codes)))
        result_query = result_query.where(or_(RuleResult.rule_key.in_(sources), RuleResult.rule_code.in_(codes)))
    facts = list(db.scalars(fact_query.order_by(TaskFact.id)))
    results = list({r.rule_key: r for r in db.scalars(result_query.order_by(RuleResult.id))}.values())
    result_values = {r.id: task_value_service.value_of(db, task, task_value_service.RECORD_RULE_RESULT, r.id) for r in results}
    projected = project_task_facts(db, task, facts, definitions=fields)
    context = {"upstream_facts": [
        {**fact, "value": task_value_service.value_of(db, task, task_value_service.RECORD_TASK_FACT, fact["fact_id"]).get("value")}
        for fact in projected
    ], "upstream_results": [
        {"rule_result_id": r.id, "node_code": "RULE_EXECUTION", "rule_key": r.rule_key, "rule_code": r.rule_code, "rule_name": r.rule_name, "status": r.status, "message": r.message,
         # 模型原始响应和格式修正日志属于本条诊断，不作为另一条规则的业务输入反复传递。
         "result": result_values[r.id]}
        for r in results
    ], "available_materials": task.manifest_json.get("materials") or []}
    if report:
        context["report_data"] = report_section_data(
            rule["report_definition"]["sections"], task.manifest_json,
            context["upstream_facts"], context["upstream_results"],
        )
    return context


def report_section_data(
    sections: list[dict[str, Any]],
    manifest: dict[str, Any],
    facts: list[dict[str, Any]],
    results: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """按章节引用关联真实数据；找不到的原引用明确保留，不拒绝整份报告。"""
    fields = {field["field_key"]: field for field in manifest["fields"]}
    rules = {rule["rule_key"]: rule for rule in manifest["rules"]}
    calculations = {item["result_code"]: item for item in manifest["calculations"]}
    output = []
    for section in sections:
        sources = []
        for ref in report_data_refs(section):
            field = fields.get(ref)
            rule = rules.get(ref)
            calculation = calculations.get(ref)
            if field is not None:
                source_facts = [fact for fact in facts
                                if fact_projection.fact_identity(fact) == fact_projection.field_fact_identity(field)]
                source_results = []
                name = field["name"]
            elif rule is not None:
                source_results = [result for result in results if result["rule_key"] == ref]
                source_facts = [fact for fact in facts if rule.get("result_code")
                                and fact["fact_code"] == rule["result_code"]]
                name = rule["name"]
            else:
                source_facts = [fact for fact in facts if fact["fact_code"] == ref]
                producer_keys = {item["rule_key"] for item in rules.values() if item.get("result_code") == ref}
                source_results = [result for result in results
                                  if result["rule_key"] in producer_keys or result["rule_code"] == ref]
                name = (calculation.get("rule_name") or calculation["rule_code"]) if calculation else ref
            sources.append({
                "reference": ref, "name": name, "facts": source_facts, "results": source_results,
                "missing_data": not source_facts and not source_results,
                "note": "没有对应的当前字段事实或本批次生效规则结果" if not source_facts and not source_results else "",
            })
        output.append({"section_key": section["section_key"], "section_name": section["section_name"],
                       "data": sources})
    return output


def validate_outputs(rule, verdict):
    outputs = verdict.get("outputs", [])
    if not isinstance(outputs, list) or any(not isinstance(item, dict) or "code" not in item or "value" not in item for item in outputs):
        raise BusinessError("RULE_OUTPUT_STRUCTURE_INVALID", "outputs必须为包含code和value的对象数组")
    if any(not isinstance(item["code"], str) or not item["code"] for item in outputs):
        raise BusinessError("RULE_OUTPUT_STRUCTURE_INVALID", "outputs的code必须为非空字符串")
    definitions = {f["field_key"]: f for f in rule.get("output_definitions") or []}
    if rule.get("result_code"):
        definitions[rule["result_code"]] = rule["calculation_definition"]
    codes = [item["code"] for item in outputs]
    if len(codes) != len(set(codes)) or set(codes) - definitions.keys():
        raise BusinessError("RULE_OUTPUT_IDENTITY_INVALID", "产出身份重复或不属于本条规则声明的输出")
    if verdict["status"] in PRODUCED_RESULT_STATUSES and definitions.keys() - set(codes):
        raise BusinessError("RULE_OUTPUT_MISSING", "模型声明已执行但未返回要求的实际产出", details={"missing": sorted(definitions.keys() - set(codes))})
    for item in outputs:
        field_output = item["code"] in {f["field_key"] for f in rule.get("output_definitions") or []}
        if field_output:
            state = item.get("field_status")
            if state not in {"PRESENT", "NOT_PRESENT", "EVIDENCE_INSUFFICIENT"}:
                raise BusinessError("RULE_OUTPUT_STATUS_INVALID", "字段输出必须明确标注已取到值、确认未载明或证据不足", details={"output": item["code"]})
            if (state == "PRESENT" and (item["value"] is None or item["value"] == "")) or (state != "PRESENT" and item["value"] is not None):
                raise BusinessError("RULE_OUTPUT_STATUS_INVALID", "字段输出值与抽取状态不一致", details={"output": item["code"], "status": state})
        if not field_output and item["value"] is None and verdict["status"] in PRODUCED_RESULT_STATUSES:
            raise BusinessError("RULE_OUTPUT_MISSING", "模型声明已执行，但实际输出值为空")
        if item["value"] is not None:
            definition = definitions[item["code"]]
            fact_projection.canonical_fact_value(item["value"], definition)
    if rule.get("kind") == "REPORT" and (verdict.get("report") is not None or verdict["status"] in PRODUCED_RESULT_STATUSES):
        report = verdict.get("report")
        if not isinstance(report, dict) or not isinstance(report.get("content"), str) or not report["content"].strip():
            raise BusinessError("RULE_REPORT_CONTENT_MISSING", "报告执行项未返回实际正文")
        if report.get("content_type") not in {"text/markdown", "text/plain", "text/html"}:
            raise BusinessError("RULE_REPORT_FORMAT_UNSUPPORTED", "当前报告保存接口不支持模型返回的正文格式")


def save_outputs(db, task, execution, rule, verdict):
    if verdict is None:
        return []
    definitions = {f["field_key"]: f for f in rule.get("output_definitions") or []}
    written: list[tuple[str, int, dict[str, Any]]] = []
    for item in verdict.get("outputs") or []:
        code, value = item["code"], item["value"]
        field = definitions.get(code)
        if field is None and (value is None or verdict["status"] not in PRODUCED_RESULT_STATUSES):
            continue
        field_status = item["field_status"] if field is not None else None
        definition = field if field is not None else rule["calculation_definition"]
        material = field["material_key"] if field is not None else None
        fact_code = str(field.get("source_ref") or code) if field is not None else code
        period = definition.get("period_scope") or definition.get("period_policy") or "NOT_APPLICABLE"
        statement = definition.get("statement_scope") or "NOT_APPLICABLE"
        entity = str(definition.get("entity_scope") or "SELF")
        if field is not None:
            fact_code, material, period, statement, entity = fact_projection.field_fact_identity(field)
        current = list(db.scalars(select(TaskFact).where(TaskFact.audit_task_id == task.id, TaskFact.fact_code == fact_code,
                            TaskFact.material_key == material, TaskFact.period_scope == period, TaskFact.statement_scope == statement,
                            TaskFact.entity_scope == entity).order_by(TaskFact.revision_no)))
        manual = db.scalar(select(TaskFieldValue).where(
            TaskFieldValue.audit_task_id == task.id, TaskFieldValue.field_key == code,
            TaskFieldValue.material_key == material, TaskFieldValue.is_current.is_(True),
            TaskFieldValue.origin == "HUMAN_CORRECTION",
        )) if field is not None else None
        if manual is not None:
            reviewed = [fact for fact in current if fact.is_current and fact.source_execution_id is None and fact.status == manual.status]
            if len(reviewed) != 1 or manual.status not in {"REVIEWED", "NOT_PRESENT"}:
                raise BusinessError("TASK_MANUAL_FACT_INCONSISTENT", "人工结论与当前字段事实不一致，无法继续保存模型产出", status_code=500,
                                    details={"output": code})
            from ...domain.fact_projection import canonical_field_value
            saved_value = task_value_service.value_of(db, task, task_value_service.RECORD_TASK_FACT, reviewed[0].id).get("value")
            expected_status = "PRESENT" if manual.status == "REVIEWED" else "NOT_PRESENT"
            if field_status != expected_status or canonical_field_value(value, field) != canonical_field_value(saved_value, field):
                raise BusinessError("RULE_OUTPUT_MANUAL_VALUE_CONFLICT", "模型产出与当前人工结论不同，人工结论未被覆盖", details={"output": code, "manual_status": manual.status})
            continue
        if any(f.source_execution_id == execution.id and f.is_current for f in current):
            continue
        revision = max((f.revision_no for f in current), default=0) + 1
        for old in current:
            old.is_current = False
        db.flush()
        if field is not None:
            document = db.scalar(select(TaskDocument).where(TaskDocument.audit_task_id == task.id, TaskDocument.material_key == material))
            upload = db.scalar(select(DocumentUpload).where(DocumentUpload.task_document_id == document.id, DocumentUpload.is_current.is_(True))) if document else None
            if upload is None:
                raise BusinessError("RULE_OUTPUT_SOURCE_MISSING", "抽取产出没有对应的真实材料上传", details={"field_key": code})
            values = list(db.scalars(select(TaskFieldValue).where(TaskFieldValue.audit_task_id == task.id, TaskFieldValue.field_key == code, TaskFieldValue.material_key == material)))
            value_revision = max((f.revision_no for f in values), default=0) + 1
            for old in values:
                old.is_current = False
            db.flush()
            field_value = TaskFieldValue(audit_task_id=task.id, scenario_version_id=task.scenario_version_id,
                                         field_key=code, material_key=material, document_upload_id=upload.id,
                                         revision_no=value_revision, is_current=True, status=field_status,
                                         extraction_execution_id=execution.id, origin="DOCUMENT_EXTRACTION")
            db.add(field_value)
            db.flush()
            written.append((task_value_service.RECORD_TASK_FIELD_VALUE, field_value.id,
                            {"raw_value": item.get("raw_value"), "normalized_value_json": value,
                             "unit": field.get("unit"), "currency": field.get("currency")}))
        task_fact = TaskFact(audit_task_id=task.id, scenario_version_id=task.scenario_version_id,
                             fact_code=fact_code, fact_kind="EXTRACTED" if field is not None else "DERIVED",
                             material_key=material, entity_scope=entity, period_scope=period, statement_scope=statement,
                             value_type=definition.get("value_type") or definition.get("result_type") or "STRING",
                             source_execution_id=execution.id, revision_no=revision, is_current=True,
                             status=field_status if field is not None else "COMPUTED", calculation_status="COMPUTED" if field is None else None,
                             calculation_rule_code=rule.get("rule_code") if field is None else None)
        db.add(task_fact)
        db.flush()
        written.append((task_value_service.RECORD_TASK_FACT, task_fact.id,
                        {"value": fact_projection.canonical_fact_value(value, definition),
                         "unit": definition.get("unit"), "currency": definition.get("currency")}))
    # 先建立数据库索引；证据核对完成后，与规则结论一起写入六表。
    report = verdict.get("report")
    if rule.get("kind") == "REPORT" and report:
        scope = set(task.task_schema_json["enabled_rule_keys"])
        records = db.scalars(select(RuleResult).where(RuleResult.audit_task_id == task.id,
                            RuleResult.rule_key.in_(scope), RuleResult.node_execution_id == execution.id).order_by(RuleResult.id))
        result_by_key = {r.rule_key: r for r in records}
        unresolved = [{"rule_key": r.rule_key, "status": r.status, "message": r.message} for r in result_by_key.values() if r.status not in COMPLETED_RESULT_STATUSES]
        report_keys = {r["rule_key"] for r in task.manifest_json["rules"] if r.get("kind") == "REPORT"}
        unresolved.extend({"rule_key": key, "status": "NOT_EXECUTED", "message": "该生效项尚无本批次执行结果"}
                          for key in sorted(scope - report_keys - result_by_key.keys()))
        complete = (scope - report_keys) <= result_by_key.keys() and all(r.status != "NOT_EXECUTED" for r in result_by_key.values())
        formats = report_format_support(rule.get("report_definition", {}).get("formats") or [], report["content_type"])
        unresolved.extend({"status": "NOT_GENERATED", "message": f"报告已保存正文，但尚未生成 {item['format']} 文件"}
                          for item in formats if not item["supported"])
        existing = next((report for report in db.scalars(select(AuditReport).where(AuditReport.audit_task_id == task.id,
                        AuditReport.node_execution_id == execution.id)) if (report.snapshot_json or {}).get("rule_key") == rule["rule_key"]), None)
        if existing is None:
            risk_keys = {r["rule_key"] for r in task.manifest_json["rules"] if r["rule_key"] in scope and is_risk_rule(r)}
            risk_available = bool(risk_keys) and risk_keys <= result_by_key.keys() and all(result_by_key[k].status in CONCLUSIVE_RESULT_STATUSES for k in risk_keys)
            db.add(AuditReport(report_no=public_no("REP"), audit_task_id=task.id, node_execution_id=execution.id,
                               report_name=rule["name"], status="GENERATED", content_type=report["content_type"], content_md=report["content"],
                               report_generated=True, scope_execution_complete=complete, risk_conclusion_available=risk_available,
                               reasons_json={"unresolved": unresolved, "format_support": formats}, snapshot_json={"rule_key": rule["rule_key"], "task_input_revision": task.input_revision, "rule_result_ids": [r.id for r in result_by_key.values()]},
                               input_revision=task.input_revision, generated_at=datetime.now(UTC)))
    return written


def tool_runtime(db, task, rule, tool_name):
    """按本次实际调用的工具注入真实数据，固定算子仅作为可选工具。"""
    if tool_name == "MATERIAL_EXISTS":
        return {"applicability": rule.get("applicability"), "requirement": rule.get("requirement"), "check_level": rule.get("check_level")}, {}
    if tool_name == "RISK_TRIGGER":
        context = evaluation_context(db, task, rule)
        sources = context["upstream_results"]
        return {"source_results": sources, "requirement": rule.get("requirement"), "risk_condition": rule.get("risk_condition")}, {}
    return None, {}
