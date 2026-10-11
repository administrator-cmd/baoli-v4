"""通用规则循环的真实输入与产出持久化，不按业务节点类型分发。"""
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
import json
from typing import Any

from sqlalchemy import select

from ...common.exceptions import BusinessError
from ...domain.identity import public_no
from ...entity import AuditReport, DocumentUpload, NodeExecution, TaskDocument, TaskFact, TaskFieldValue, RuleResult
from .. import task_csv_service


def evaluation_context(db, task, rule) -> dict[str, Any]:
    facts = task_csv_service.records(db, task, TaskFact)
    codes = set(rule.get("source_rule_codes") or [])
    calculation = rule.get("calculation_definition") or {}
    codes.update(calculation.get("depends_on_result_codes") or [])
    codes.update(p["source_code"] for p in calculation.get("parameters") or [] if p.get("source_type") in {"CALC_RESULT", "FIELD"} and p.get("source_code"))
    field_codes = {str(f.get("source_ref") or f["field_key"]) for f in rule.get("output_definitions") or []}
    sources = set(rule.get("source_rule_keys") or [])
    if rule.get("source_rule_key"):
        sources.add(rule["source_rule_key"])
    results = task_csv_service.records(db, task, RuleResult)
    selected = set(task.task_schema_json["enabled_rule_keys"])
    # 同一技术容器内以执行批次+规则身份定位，当前调用看到真实已保存的上游结果。
    current_execution = db.scalar(select(NodeExecution.id).where(NodeExecution.audit_task_id == task.id, NodeExecution.status != "INVALIDATED").order_by(NodeExecution.id.desc()))
    executions = {r.rule_key: r for r in results if r.rule_key in selected and r.node_execution_id == current_execution}
    report = rule.get("kind") == "REPORT"
    return {"upstream_facts": [
        {"fact_id": f.id, "fact_code": f.fact_code, "material_key": f.material_key,
         "revision_no": f.revision_no, "status": f.status,
         "value": str(f.decimal_value) if f.decimal_value is not None else f.date_value.isoformat() if f.date_value is not None else f.boolean_value if f.boolean_value is not None else f.text_value,
         "judgement": f.judgement_json}
        for f in facts if f.is_current and (report or f.fact_code in codes | field_codes)
    ], "upstream_results": [
        {"rule_result_id": r.id, "node_code": "RULE_EXECUTION", "rule_key": r.rule_key, "rule_code": r.rule_code, "status": r.status, "message": r.message, "result": r.result_json}
        for key, r in executions.items() if report or key in sources or r.rule_code in codes
    ], "available_materials": task.manifest_json.get("materials") or []}


def _value_columns(value, definition):
    kind = definition.get("value_type") or definition.get("result_type") or "STRING"
    columns = {"decimal_value": None, "text_value": None, "date_value": None, "boolean_value": None}
    try:
        if definition.get("extraction_strategy") == "STRUCTURED":
            columns["text_value"] = json.dumps(value, ensure_ascii=False, allow_nan=False)
        elif kind in {"DECIMAL", "AMOUNT", "PERCENT", "INTEGER"}:
            decimal = Decimal(str(value))
            if not decimal.is_finite():
                raise ValueError("数值必须有限")
            if kind == "INTEGER" and decimal != decimal.to_integral_value():
                raise ValueError("INTEGER产出必须为整数")
            columns["decimal_value"] = decimal
        elif kind == "DATE":
            columns["date_value"] = date.fromisoformat(str(value))
        elif kind == "BOOLEAN":
            if not isinstance(value, bool):
                raise ValueError("BOOLEAN产出必须为JSON布尔值")
            columns["boolean_value"] = value
        else:
            columns["text_value"] = json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else str(value)
    except (ValueError, InvalidOperation) as exc:
        raise BusinessError("RULE_OUTPUT_VALUE_INVALID", f"规则产出不能写入声明的数据类型：{exc}") from exc
    return columns


def validate_outputs(rule, verdict):
    if not isinstance(verdict, dict) or "status" not in verdict:
        raise BusinessError("RULE_OUTPUT_STRUCTURE_INVALID", "模型执行结果必须包含status")
    outputs = verdict.get("outputs", [])
    if not isinstance(outputs, list) or any(not isinstance(item, dict) or "code" not in item or "value" not in item for item in outputs):
        raise BusinessError("RULE_OUTPUT_STRUCTURE_INVALID", "outputs必须为包含code和value的对象数组")
    definitions = {f["field_key"]: f for f in rule.get("output_definitions") or []}
    if rule.get("result_code"):
        definitions[rule["result_code"]] = rule["calculation_definition"]
    codes = [item["code"] for item in outputs]
    if len(codes) != len(set(codes)) or set(codes) - definitions.keys():
        raise BusinessError("RULE_OUTPUT_IDENTITY_INVALID", "产出身份重复或不属于本条规则声明的输出")
    if verdict["status"] in {"PASSED", "ABNORMAL", "COMPUTED"} and definitions.keys() - set(codes):
        raise BusinessError("RULE_OUTPUT_MISSING", "模型声明已执行但未返回要求的实际产出", details={"missing": sorted(definitions.keys() - set(codes))})
    for item in outputs:
        if item["value"] is None and verdict["status"] in {"PASSED", "ABNORMAL", "COMPUTED"}:
            raise BusinessError("RULE_OUTPUT_MISSING", "模型声明已执行，但实际输出值为空")
        if item["value"] is not None:
            definition = definitions[item["code"]]
            if definition.get("extraction_strategy") == "STRUCTURED":
                from ...domain.field_strategy import normalize_structured_value
                if not isinstance(definition.get("structure_schema"), dict):
                    raise BusinessError("RULE_OUTPUT_STRUCTURE_INVALID", "结构化字段没有可用的数据结构，无法保存该条产出")
                try:
                    item["value"] = normalize_structured_value(item["value"], definition.get("structure_schema"))
                except ValueError as exc:
                    raise BusinessError("RULE_OUTPUT_STRUCTURE_INVALID", f"实际产出的JSON不符合字段数据结构：{exc}") from exc
            _value_columns(item["value"], definition)
    if rule.get("kind") == "REPORT" and verdict["status"] in {"PASSED", "ABNORMAL"}:
        report = verdict.get("report")
        if not isinstance(report, dict) or not isinstance(report.get("content"), str) or not report["content"].strip():
            raise BusinessError("RULE_REPORT_CONTENT_MISSING", "报告执行项未返回实际正文")
        if report.get("content_type") not in {"text/markdown", "text/plain", "text/html"}:
            raise BusinessError("RULE_REPORT_FORMAT_UNSUPPORTED", "当前报告保存接口不支持模型返回的正文格式")
        formats = {str(value).strip().lower() for value in rule.get("report_definition", {}).get("formats") or []}
        aliases = {"markdown": "text/markdown", "md": "text/markdown", "text": "text/plain", "txt": "text/plain", "html": "text/html"}
        required = {aliases.get(value, value) for value in formats}
        if required - {report["content_type"]}:
            raise BusinessError("RULE_REPORT_FORMAT_UNSUPPORTED", "实际报告产出未满足规则声明的输出格式", details={"required": sorted(required), "actual": report["content_type"]})


def save_outputs(db, task, execution, rule, verdict, instance, tool_results):
    if verdict is None:
        return
    validate_outputs(rule, verdict)
    if verdict["status"] not in {"PASSED", "ABNORMAL", "COMPUTED"}:
        return
    definitions = {f["field_key"]: f for f in rule.get("output_definitions") or []}
    for item in verdict.get("outputs") or []:
        code, value = item["code"], item["value"]
        if value is None:
            continue
        field = definitions.get(code)
        definition = field if field is not None else rule["calculation_definition"]
        material = field["material_key"] if field is not None else None
        fact_code = str(field.get("source_ref") or code) if field is not None else code
        period = definition.get("period_scope") or definition.get("period_policy") or "NOT_APPLICABLE"
        statement = definition.get("statement_scope") or "NOT_APPLICABLE"
        current = list(db.scalars(select(TaskFact).where(TaskFact.audit_task_id == task.id, TaskFact.fact_code == fact_code,
                            TaskFact.material_key == material, TaskFact.period_scope == period, TaskFact.statement_scope == statement,
                            TaskFact.entity_scope == "SELF").order_by(TaskFact.revision_no)))
        reviewed = [fact for fact in current if fact.is_current and fact.status == "REVIEWED"]
        if reviewed:
            from ...domain.fact_snapshot import canonical_field_value, task_fact_value
            saved = task_csv_service.from_index(db, task, TaskFact, reviewed)[0]
            if field is None or canonical_field_value(value, field) != task_fact_value(saved, field_definition=field):
                raise BusinessError("RULE_OUTPUT_MANUAL_VALUE_CONFLICT", "模型产出与当前人工确认值不同，人工值未被覆盖", details={"output": code})
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
            db.add(TaskFieldValue(audit_task_id=task.id, scenario_version_id=task.scenario_version_id,
                                 field_key=code, material_key=material, document_upload_id=upload.id,
                                 revision_no=value_revision, is_current=True, status="PRESENT",
                                 raw_value=str(item.get("raw_value") or ""), normalized_value_json=value,
                                 extraction_execution_id=execution.id, origin="DOCUMENT_EXTRACTION"))
        db.add(TaskFact(audit_task_id=task.id, scenario_version_id=task.scenario_version_id,
                       fact_code=fact_code, fact_kind="EXTRACTED" if field is not None else "DERIVED",
                       material_key=material, entity_scope="SELF", period_scope=period, statement_scope=statement,
                       value_type=definition.get("value_type") or definition.get("result_type") or "STRING",
                       **_value_columns(value, definition), unit=definition.get("unit"), currency=definition.get("currency"),
                       source_execution_id=execution.id, revision_no=revision, is_current=True,
                       status="PRESENT" if field is not None else "COMPUTED", calculation_status="COMPUTED" if field is None else None,
                       calculation_rule_code=rule.get("rule_code") if field is None else None,
                       input_snapshot_json={"inputs": instance.get("inputs") or [], "tool_results": tool_results or []}, judgement_json=verdict))
    report = verdict.get("report")
    if rule.get("kind") == "REPORT" and report and verdict["status"] in {"PASSED", "ABNORMAL"}:
        records = task_csv_service.records(db, task, RuleResult)
        scope = set(task.task_schema_json["enabled_rule_keys"])
        result_by_key = {r.rule_key: r for r in records if r.rule_key in scope and r.node_execution_id == execution.id}
        unresolved = [{"rule_key": r.rule_key, "status": r.status, "message": r.message} for r in result_by_key.values() if r.status not in {"PASSED", "ABNORMAL", "COMPUTED", "NOT_APPLICABLE"}]
        report_keys = {r["rule_key"] for r in task.manifest_json["rules"] if r.get("kind") == "REPORT"}
        unresolved.extend({"rule_key": key, "status": "NOT_EXECUTED", "message": "该生效项尚无本批次执行结果"}
                          for key in sorted(scope - report_keys - result_by_key.keys()))
        complete = (scope - report_keys) <= result_by_key.keys() and not unresolved
        existing = next((report for report in db.scalars(select(AuditReport).where(AuditReport.audit_task_id == task.id,
                        AuditReport.node_execution_id == execution.id)) if (report.snapshot_json or {}).get("rule_key") == rule["rule_key"]), None)
        if existing is None:
            risk_keys = {r["rule_key"] for r in task.manifest_json["rules"] if r["rule_key"] in scope and (r.get("risk_condition") or r.get("operator") == "RISK_TRIGGER")}
            risk_available = bool(risk_keys) and risk_keys <= result_by_key.keys() and all(result_by_key[k].status in {"PASSED", "ABNORMAL", "NOT_APPLICABLE"} for k in risk_keys)
            db.add(AuditReport(report_no=public_no("REP"), audit_task_id=task.id, node_execution_id=execution.id,
                               report_name=rule["name"], status="GENERATED", content_type=report["content_type"], content_md=report["content"],
                               report_generated=True, scope_execution_complete=complete, risk_conclusion_available=risk_available,
                               reasons_json={"unresolved": unresolved}, snapshot_json={"rule_key": rule["rule_key"], "manifest_hash": task.manifest_json["manifest_hash"], "rule_result_ids": [r.id for r in result_by_key.values()]},
                               input_revision=task.input_revision, origin="AGENT", generated_at=datetime.now(UTC)))


def tool_runtime(db, task, rule, tool_name):
    """按本次实际调用的工具注入真实数据，固定算子仅作为可选工具。"""
    if tool_name == "MATERIAL_EXISTS":
        return {"applicability": rule.get("applicability"), "requirement": rule.get("requirement"), "check_level": rule.get("check_level")}, {}
    if tool_name == "RISK_TRIGGER":
        context = evaluation_context(db, task, rule)
        sources = context["upstream_results"]
        return {"source_results": sources, "requirement": rule.get("requirement"), "risk_condition": rule.get("risk_condition")}, {}
    return None, {}
