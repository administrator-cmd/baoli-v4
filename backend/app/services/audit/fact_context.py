"""按完整字段口径查询事实，并统一模型输入、取值和证据的投影。"""
from sqlalchemy import false, select, tuple_

from ...common.exceptions import BusinessError
from ...constants.execution import (
    FACT_KIND_EXTRACTED,
    ORIGIN_DOCUMENT_EXTRACTION,
    ORIGIN_HUMAN_CORRECTION,
)
from ...domain import fact_projection
from ...domain.manifest import enabled_extraction_field_keys
from ...entity.tasks import RuleResult, TaskFact, TaskFieldValue
from . import value_store as task_value_service


def fact_owner_field_keys(db, task) -> set[str]:
    """会形成字段事实的字段：启用规则的产出字段，加上已有当前人工事实的字段。

    编译期只把启用规则的产出登记为事实身份；人工核对允许给没有自动抽取的字段补值，
    这类字段一旦存在当前人工事实，就必须同样进入身份索引，否则普通规则读不到，
    报告规则还会因为事实缺少身份而失败。
    """
    keys = enabled_extraction_field_keys(
        task.manifest_json, task.task_schema_json["enabled_rule_keys"],
    )
    keys |= set(db.scalars(select(TaskFieldValue.field_key).where(
        TaskFieldValue.audit_task_id == task.id,
        TaskFieldValue.is_current.is_(True),
        TaskFieldValue.origin == ORIGIN_HUMAN_CORRECTION,
    )))
    return keys


def extracted_identity_filter(definitions, *, owner_keys=None):
    """字段事实的身份过滤；给出 owner_keys 时只保留会形成字段事实的定义。"""
    if owner_keys is not None:
        definitions = {key: field for key, field in definitions.items() if key in owner_keys}
    identities = list(fact_projection.field_identity_index(definitions))
    if not identities:
        return false()
    return (TaskFact.fact_kind == FACT_KIND_EXTRACTED) & tuple_(
        TaskFact.fact_code, TaskFact.material_key, TaskFact.period_scope,
        TaskFact.statement_scope, TaskFact.entity_scope,
    ).in_(identities)


def project_task_facts(db, task, rows, *, definitions=None, material_states=None):
    from .evidence import evidence_memo, field_evidence, rule_evidence
    definitions = definitions if definitions is not None else {f["field_key"]: f for f in
                                                               task.task_schema_json["fields"]}
    values = {row.id: task_value_service.value_of(db, task, task_value_service.RECORD_TASK_FACT, row.id) for row in
              rows}
    # 只有启用规则的产出字段和已有人工事实的字段会形成字段事实；身份索引按这一组建立。
    identity_definitions = {
        key: field for key, field in definitions.items() if key in fact_owner_field_keys(db, task)
    }
    field_values = list(db.scalars(select(TaskFieldValue).where(
        TaskFieldValue.audit_task_id == task.id, TaskFieldValue.is_current.is_(True),
        TaskFieldValue.field_key.in_(definitions),
    )))
    by_source = {(item.field_key, item.material_key, item.extraction_execution_id): item for item in field_values}
    identities = fact_projection.field_identity_index(identity_definitions)
    raw_values = {}
    source_values = {}
    for row in rows:
        if row.fact_kind != FACT_KIND_EXTRACTED:
            continue
        field_key = identities.get(fact_projection.fact_identity(row))
        source = by_source.get((field_key, row.material_key, row.source_execution_id))
        if source is None:
            raise BusinessError("TASK_FIELD_FACT_INCOMPLETE", "当前字段事实没有对应来源版本，无法读取真实原文",
                                status_code=500,
                                details={"fact_id": row.id, "field_key": field_key,
                                         "material_key": row.material_key,
                                         "source_execution_id": row.source_execution_id})
        raw_values[row.id] = task_value_service.value_of(
            db, task, task_value_service.RECORD_TASK_FIELD_VALUE, source.id,
        ).get("raw_value")
        source_values[row.id] = source.id
    facts = fact_projection.facts_of_task_fact_rows(
        rows, values=values, field_definitions=identity_definitions, material_states=material_states,
        raw_values=raw_values,
        # 材料中文名只来自冻结配置，用于比较双方的来源展示，不从字段名或材料键推断。
        material_names={
            str(item["material_key"]): str(item.get("name") or item["material_key"])
            for item in task.manifest_json.get("materials") or []
            if item.get("material_key")
        },
    )
    keys = {fact["field_key"] for fact in facts if fact["fact_kind"] == FACT_KIND_EXTRACTED}
    human_keys = set(db.scalars(select(TaskFieldValue.field_key).where(
        TaskFieldValue.audit_task_id == task.id, TaskFieldValue.is_current.is_(True),
        TaskFieldValue.field_key.in_(keys), TaskFieldValue.origin == ORIGIN_HUMAN_CORRECTION,
    ))) if keys else set()
    # 计算事实的来源规则一次取齐：逐条回查会让读取时间随事实条数线性增长。
    source_results: dict[tuple[int | None, str | None], list[int]] = {}
    derived = [row for row in rows if row.fact_kind != FACT_KIND_EXTRACTED]
    if derived:
        pairs = {(row.source_execution_id, row.calculation_rule_code) for row in derived}
        query = select(RuleResult.node_execution_id, RuleResult.rule_code, RuleResult.id).where(
            RuleResult.audit_task_id == task.id,
            tuple_(RuleResult.node_execution_id, RuleResult.rule_code).in_(pairs),
        )
        for execution_id, rule_code, result_id in db.execute(query).all():
            source_results.setdefault((execution_id, rule_code), []).append(result_id)
    # 同一请求内复用已展开的证据链：多个事实引用同一上游时不重复递归读取。
    memo = evidence_memo()
    for row, fact in zip(rows, facts):
        if row.fact_kind == FACT_KIND_EXTRACTED:
            fact["source_field_value_id"] = source_values[row.id]
            fact["evidence"] = field_evidence(db, task, source_values[row.id], memo)
            fact["origin"] = ORIGIN_HUMAN_CORRECTION if fact["field_key"] in human_keys else ORIGIN_DOCUMENT_EXTRACTION
        else:
            sources = source_results.get((row.source_execution_id, row.calculation_rule_code), [])
            if len(sources) != 1:
                raise BusinessError("TASK_FACT_SOURCE_INVALID", "计算事实没有唯一对应的规则结果", status_code=500,
                                    details={"fact_id": row.id})
            fact["source_rule_result_id"] = sources[0]
            fact["evidence"] = rule_evidence(db, task, sources[0], memo)
    return facts
