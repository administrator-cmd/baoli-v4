"""通用规则循环的真实输入与产出持久化，不按业务节点类型分发。"""
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, or_, select

from ...common.exceptions import BusinessError
from ...constants.execution import (
    ENTITY_SCOPE_SELF,
    FACT_KIND_DERIVED,
    FACT_KIND_EXTRACTED,
    ORIGIN_DOCUMENT_EXTRACTION,
    ORIGIN_HUMAN_CORRECTION,
)
from ...constants.status import (
    COMPLETED_RESULT_STATUSES,
    CONCLUSIVE_RESULT_STATUSES,
    FIELD_STATUS_NOT_PRESENT,
    FIELD_STATUS_PRESENT,
    FIELD_STATUS_REVIEWED,
    MODEL_FIELD_STATUSES,
    PRODUCED_RESULT_STATUSES,
)
from ...domain import fact_projection
from ...domain.identity import public_no
from ...domain.report_data import report_data_refs, report_format_support
from ...domain.rule_library import is_risk_rule
from ...entity.reports import AuditReport
from ...entity.tasks import RuleResult, TaskDocument, TaskFact, TaskFieldValue
from . import value_store as task_value_service
from .agent import _upstream_result_view
from .execution_scope import latest_execution
from .shared import current_upload


def evaluation_context(db, task, rule) -> dict[str, Any]:
    from ...domain.execution_mode import MODE_MATERIAL_CHECK, MODE_UNKNOWN, SOURCE_ONLY_MODES
    from .fact_context import extracted_identity_filter, fact_owner_field_keys, project_task_facts
    mode = str(rule.get("execution_mode") or MODE_UNKNOWN)
    source_only = mode in SOURCE_ONLY_MODES or mode == MODE_MATERIAL_CHECK
    codes = set(rule.get("source_rule_codes") or [])
    calculation = rule.get("calculation_definition") or {}
    codes.update(calculation.get("depends_on_result_codes") or [])
    codes.update(p["source_code"] for p in calculation.get("parameters") or [] if p.get("source_type") == "CALC_RESULT" and p.get("source_code"))
    # 引用声明来自完整冻结配置；未选中/停用字段仍可说明身份，不能当成整批程序异常。
    fields = {f["field_key"]: f for f in task.manifest_json["fields"]}
    field_keys = set() if source_only else {item["field_key"] for item in rule.get("inputs") or []}
    if not source_only:
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
    current_execution = latest_execution(db, task.id)
    report = rule.get("kind") == "REPORT"
    fact_query = select(TaskFact).where(TaskFact.audit_task_id == task.id, TaskFact.is_current.is_(True))
    result_query = select(RuleResult).where(RuleResult.audit_task_id == task.id,
                                         RuleResult.rule_key.in_(selected),
                                         RuleResult.node_execution_id == (current_execution.id if current_execution else None))
    if not report:
        # 事实身份只按会形成字段事实的定义解析：停用/未选字段不参与归属，与编译期同一口径。
        fact_query = fact_query.where(or_(extracted_identity_filter(definitions, owner_keys=fact_owner_field_keys(task)),
                                         (TaskFact.fact_kind == FACT_KIND_DERIVED) & TaskFact.fact_code.in_(codes)))
        result_query = result_query.where(or_(RuleResult.rule_key.in_(sources), RuleResult.rule_code.in_(codes)))
    facts = list(db.scalars(fact_query.order_by(TaskFact.id)))
    results = list({r.rule_key: r for r in db.scalars(result_query.order_by(RuleResult.id))}.values())
    result_values = {r.id: task_value_service.value_of(db, task, task_value_service.RECORD_RULE_RESULT, r.id) for r in results}
    projected = project_task_facts(db, task, facts, definitions=fields)
    context = {"upstream_facts": [
        {**fact, "value": task_value_service.value_of(db, task, task_value_service.RECORD_TASK_FACT, fact["fact_id"]).get("value")}
        for fact in projected
    ], "upstream_results": [
        # 只把身份与业务结论传给下游：evidence、tool_findings、report、risk_context 等
        # 属于本条结果的诊断与展示，不作为另一条规则的业务输入反复传递。
        _upstream_result_view({
            "rule_result_id": r.id, "rule_key": r.rule_key, "rule_code": r.rule_code,
            "rule_name": r.rule_name, "status": r.status, "message": r.message,
            "risk_level": r.risk_level,
            "result": result_values[r.id],
        })
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
    # 事实与结果按引用键建一次索引：逐章节、逐引用扫描全量会让报告耗时随数据量平方增长。
    # 分组保持原列表顺序，章节内的引用顺序不变。
    facts_by_identity: dict[tuple[Any, ...], list[dict[str, Any]]] = {}
    facts_by_code: dict[str, list[dict[str, Any]]] = {}
    for fact in facts:
        facts_by_identity.setdefault(fact_projection.fact_identity(fact), []).append(fact)
        facts_by_code.setdefault(str(fact["fact_code"]), []).append(fact)
    results_by_key: dict[str, list[dict[str, Any]]] = {}
    for result in results:
        results_by_key.setdefault(result["rule_key"], []).append(result)
    producers_by_code: dict[str, set[str]] = {}
    for rule in rules.values():
        if rule.get("result_code"):
            producers_by_code.setdefault(str(rule["result_code"]), set()).add(rule["rule_key"])
    output = []
    for section in sections:
        sources = []
        for ref in report_data_refs(section):
            field = fields.get(ref)
            rule = rules.get(ref)
            calculation = calculations.get(ref)
            if field is not None:
                source_facts = facts_by_identity.get(fact_projection.field_fact_identity(field), [])
                source_results = []
                name = field["name"]
            elif rule is not None:
                source_results = results_by_key.get(ref, [])
                source_facts = facts_by_code.get(str(rule["result_code"]), []) if rule.get("result_code") else []
                name = rule["name"]
            else:
                source_facts = facts_by_code.get(str(ref), [])
                producer_keys = producers_by_code.get(str(ref), set())
                source_results = [
                    result for result in results
                    if result["rule_key"] in producer_keys or result["rule_code"] == ref
                ]
                name = (calculation.get("rule_name") or calculation["rule_code"]) if calculation else ref
            sources.append({
                "reference": ref, "name": name, "facts": source_facts,
                # 报告数据只带上游结果的业务结论，避免 report_data 再复制一份完整负载。
                "results": [_upstream_result_view(result) for result in source_results],
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
            if state not in MODEL_FIELD_STATUSES:
                raise BusinessError("RULE_OUTPUT_STATUS_INVALID", "字段输出必须明确标注已取到值、确认未载明或证据不足", details={"output": item["code"]})
            if (state == FIELD_STATUS_PRESENT and (item["value"] is None or item["value"] == "")) or (state != FIELD_STATUS_PRESENT and item["value"] is not None):
                raise BusinessError("RULE_OUTPUT_STATUS_INVALID", "字段输出值与抽取状态不一致", details={"output": item["code"], "status": state})
            # 抽取契约要求已取到值的字段同时给出原文写法；缺原文就无法核对取值来源，
            # 也不能从标准值反造。这里交回模型修正，而不是静默保存一个没有原文的取值。
            if state == FIELD_STATUS_PRESENT:
                raw_value = item.get("raw_value")
                if not isinstance(raw_value, str) or not raw_value.strip():
                    raise BusinessError(
                        "RULE_OUTPUT_RAW_VALUE_MISSING",
                        "字段已取到值，但没有返回原文写法（raw_value）",
                        details={"output": item["code"]},
                    )
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


def save_outputs(db, task, execution, rule, verdict, *, evaluation_id: str | None):
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
        entity = str(definition.get("entity_scope") or ENTITY_SCOPE_SELF)
        if field is not None:
            fact_code, material, period, statement, entity = fact_projection.field_fact_identity(field)
        # 只读当前事实与最大版本号：历史会在每次重跑后累积，整段载入没有上限。
        fact_filter = (TaskFact.audit_task_id == task.id, TaskFact.fact_code == fact_code,
                       TaskFact.material_key == material, TaskFact.period_scope == period,
                       TaskFact.statement_scope == statement, TaskFact.entity_scope == entity)
        current = list(db.scalars(select(TaskFact).where(*fact_filter, TaskFact.is_current.is_(True))))
        next_revision = (db.scalar(select(func.max(TaskFact.revision_no)).where(*fact_filter)) or 0) + 1
        manual = db.scalar(select(TaskFieldValue).where(
            TaskFieldValue.audit_task_id == task.id, TaskFieldValue.field_key == code,
            TaskFieldValue.material_key == material, TaskFieldValue.is_current.is_(True),
            TaskFieldValue.origin == ORIGIN_HUMAN_CORRECTION,
        )) if field is not None else None
        if manual is not None:
            reviewed = [fact for fact in current if fact.is_current and fact.source_execution_id is None and fact.status == manual.status]
            if len(reviewed) != 1 or manual.status not in {FIELD_STATUS_REVIEWED, FIELD_STATUS_NOT_PRESENT}:
                raise BusinessError("TASK_MANUAL_FACT_INCONSISTENT", "人工结论与当前字段事实不一致，无法继续保存模型产出", status_code=500,
                                    details={"output": code})
            from ...domain.fact_projection import canonical_field_value
            saved_value = task_value_service.value_of(db, task, task_value_service.RECORD_TASK_FACT, reviewed[0].id).get("value")
            expected_status = FIELD_STATUS_PRESENT if manual.status == FIELD_STATUS_REVIEWED else FIELD_STATUS_NOT_PRESENT
            if field_status != expected_status or canonical_field_value(value, field) != canonical_field_value(saved_value, field):
                raise BusinessError("RULE_OUTPUT_MANUAL_VALUE_CONFLICT", "模型产出与当前人工结论不同，人工结论未被覆盖", details={"output": code, "manual_status": manual.status})
            continue
        # 同一批次可以重跑（继续执行只重跑未完成项及其下游），重跑得到的新值必须写成新版本；
        # 不能因为「本批次已经写过当前事实」就跳过，否则规则结论更新而字段事实仍是旧的。
        revision = next_revision
        for old in current:
            old.is_current = False
        db.flush()
        if field is not None:
            document = db.scalar(select(TaskDocument).where(TaskDocument.audit_task_id == task.id, TaskDocument.material_key == material))
            upload = current_upload(db, document) if document else None
            if upload is None:
                raise BusinessError("RULE_OUTPUT_SOURCE_MISSING", "抽取产出没有对应的真实材料上传", details={"field_key": code})
            value_filter = (TaskFieldValue.audit_task_id == task.id, TaskFieldValue.field_key == code,
                            TaskFieldValue.material_key == material)
            values = list(db.scalars(select(TaskFieldValue).where(*value_filter, TaskFieldValue.is_current.is_(True))))
            value_revision = (db.scalar(select(func.max(TaskFieldValue.revision_no)).where(*value_filter)) or 0) + 1
            for old in values:
                old.is_current = False
            db.flush()
            field_value = TaskFieldValue(audit_task_id=task.id, scenario_version_id=task.scenario_version_id,
                                         field_key=code, material_key=material, document_upload_id=upload.id,
                                         revision_no=value_revision, is_current=True, status=field_status,
                                         extraction_execution_id=execution.id, origin=ORIGIN_DOCUMENT_EXTRACTION)
            db.add(field_value)
            db.flush()
            written.append((task_value_service.RECORD_TASK_FIELD_VALUE, field_value.id,
                            {"raw_value": item.get("raw_value"), "normalized_value_json": value,
                             "unit": field.get("unit"), "currency": field.get("currency")}))
        task_fact = TaskFact(audit_task_id=task.id, scenario_version_id=task.scenario_version_id,
                             fact_code=fact_code, fact_kind=FACT_KIND_EXTRACTED if field is not None else FACT_KIND_DERIVED,
                             material_key=material, entity_scope=entity, period_scope=period, statement_scope=statement,
                             value_type=definition.get("value_type") or definition.get("result_type") or "STRING",
                             source_execution_id=execution.id, revision_no=revision, is_current=True,
                             status=field_status if field is not None else "COMPUTED",
                             calculation_rule_code=rule.get("rule_code") if field is None else None)
        db.add(task_fact)
        db.flush()
        written.append((task_value_service.RECORD_TASK_FACT, task_fact.id,
                        {"value": fact_projection.canonical_fact_value(value, definition),
                         "unit": definition.get("unit"), "currency": definition.get("currency")}))
    # 先建立数据库索引；证据核对完成后，与规则结论一起写入四表。
    report = verdict.get("report")
    if rule.get("kind") == "REPORT" and report:
        scope = set(task.task_schema_json["enabled_rule_keys"])
        records = db.scalars(select(RuleResult).where(RuleResult.audit_task_id == task.id,
                            RuleResult.rule_key.in_(scope), RuleResult.node_execution_id == execution.id).order_by(RuleResult.id))
        result_by_key = {r.rule_key: r for r in records}
        report_keys = {r["rule_key"] for r in task.manifest_json["rules"] if r.get("kind") == "REPORT"}
        # 报告只依赖非报告项；报告自己上一轮的结果不是本轮依据。
        dependency_results = {key: r for key, r in result_by_key.items() if key not in report_keys}
        unresolved = [{"rule_key": r.rule_key, "status": r.status, "message": r.message} for r in dependency_results.values() if r.status not in COMPLETED_RESULT_STATUSES]
        unresolved.extend({"rule_key": key, "status": "NOT_EXECUTED", "message": "该生效项尚无本批次执行结果"}
                          for key in sorted(scope - report_keys - dependency_results.keys()))
        complete = (scope - report_keys) <= dependency_results.keys() and all(r.status != "NOT_EXECUTED" for r in dependency_results.values())
        formats = report_format_support(rule.get("report_definition", {}).get("formats") or [], report["content_type"])
        unresolved.extend({"status": "NOT_GENERATED", "message": f"报告已保存正文，但尚未生成 {item['format']} 文件"}
                          for item in formats if not item["supported"])
        existing = next((report for report in db.scalars(select(AuditReport).where(AuditReport.audit_task_id == task.id,
                        AuditReport.node_execution_id == execution.id)) if (report.snapshot_json or {}).get("rule_key") == rule["rule_key"]), None)
        risk_keys = {r["rule_key"] for r in task.manifest_json["rules"] if r["rule_key"] in scope and is_risk_rule(r)}
        risk_available = bool(risk_keys) and risk_keys <= result_by_key.keys() and all(result_by_key[k].status in CONCLUSIVE_RESULT_STATUSES for k in risk_keys)
        snapshot = {"rule_key": rule["rule_key"], "task_input_revision": task.input_revision,
                    "rule_result_ids": [r.id for r in result_by_key.values()],
                    "evaluation_id": evaluation_id}
        generated_at = datetime.now(UTC)
        if existing is None:
            db.add(AuditReport(report_no=public_no("REP"), audit_task_id=task.id, node_execution_id=execution.id,
                               report_name=rule["name"], content_type=report["content_type"], content_md=report["content"],
                               scope_execution_complete=complete, risk_conclusion_available=risk_available,
                               reasons_json={"unresolved": unresolved, "format_support": formats}, snapshot_json=snapshot,
                               input_revision=task.input_revision, generated_at=generated_at))
        else:
            # 同一批次可以重跑（继续执行只重跑未完成项及其下游）；重跑生成的正文与依据必须写回，
            # 否则 report_view 会继续把上一轮的正文当成当前报告返回。
            existing.report_name = rule["name"]
            existing.content_type = report["content_type"]
            existing.content_md = report["content"]
            existing.scope_execution_complete = complete
            existing.risk_conclusion_available = risk_available
            existing.reasons_json = {"unresolved": unresolved, "format_support": formats}
            existing.snapshot_json = snapshot
            existing.input_revision = task.input_revision
            existing.generated_at = generated_at
    return written


def revoke_outputs(db, task, execution, rule, codes: set[str] | None = None) -> None:
    """重跑未形成结论时撤销本条规则上一轮的当前产出。

    `codes` 限定要撤销的产出；未指定时撤销全部声明产出。行不删除，只退出当前位；
    人工确认与人工纠错版本不受影响。字段产出的归属由编译期保证唯一（`RULE_OUTPUT_CONFLICT`）。
    """
    identities: set[tuple[Any, ...]] = set()
    value_keys: set[tuple[str, str]] = set()
    for field in rule.get("output_definitions") or []:
        if codes is not None and field["field_key"] not in codes:
            continue
        identity = fact_projection.field_fact_identity(field)
        identities.add(identity)
        value_keys.add((str(field["field_key"]), str(identity[1])))
    if identities:
        # 一次取回候选行再按完整身份过滤：逐条查会把一次撤销放大成 2F 条 SQL。
        facts = db.scalars(select(TaskFact).where(
            TaskFact.audit_task_id == task.id,
            TaskFact.fact_kind == FACT_KIND_EXTRACTED,
            TaskFact.fact_code.in_({str(identity[0]) for identity in identities}),
            TaskFact.is_current.is_(True),
            TaskFact.source_execution_id.is_not(None),
        )).all()
        for fact in facts:
            if (fact.fact_code, fact.material_key, fact.period_scope,
                    fact.statement_scope, fact.entity_scope) in identities:
                fact.is_current = False
        values = db.scalars(select(TaskFieldValue).where(
            TaskFieldValue.audit_task_id == task.id,
            TaskFieldValue.field_key.in_({key for key, _ in value_keys}),
            TaskFieldValue.is_current.is_(True),
            TaskFieldValue.origin == ORIGIN_DOCUMENT_EXTRACTION,
        )).all()
        for value in values:
            if (value.field_key, value.material_key) in value_keys:
                value.is_current = False
    if rule.get("result_code") and (codes is None or rule["result_code"] in codes):
        for fact in db.scalars(select(TaskFact).where(
            TaskFact.audit_task_id == task.id,
            TaskFact.fact_code == rule["result_code"],
            TaskFact.fact_kind == FACT_KIND_DERIVED,
            TaskFact.calculation_rule_code == rule.get("rule_code"),
            TaskFact.source_execution_id == execution.id,
            TaskFact.is_current.is_(True),
        )):
            fact.is_current = False
    db.flush()


def tool_runtime(db, task, rule, tool_name):
    """按本次实际调用的工具注入真实数据，固定算子仅作为可选工具。"""
    if tool_name == "MATERIAL_EXISTS":
        return {"applicability": rule.get("applicability"), "requirement": rule.get("requirement"), "check_level": rule.get("check_level")}, {}
    if tool_name == "RISK_TRIGGER":
        context = evaluation_context(db, task, rule)
        sources = context["upstream_results"]
        return {"source_results": sources, "requirement": rule.get("requirement"), "risk_condition": rule.get("risk_condition")}, {}
    return None, {}
