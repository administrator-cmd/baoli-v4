"""按完整字段口径查询事实，并统一模型输入、取值和证据的投影。"""
from sqlalchemy import false, tuple_, select

from ...domain import fact_projection
from ...entity import TaskFact, TaskFieldValue
from .. import task_value_service


def extracted_identity_filter(definitions):
    identities = list(fact_projection.field_identity_index(definitions))
    if not identities:
        return false()
    return (TaskFact.fact_kind == "EXTRACTED") & tuple_(
        TaskFact.fact_code, TaskFact.material_key, TaskFact.period_scope,
        TaskFact.statement_scope, TaskFact.entity_scope,
    ).in_(identities)


def project_task_facts(db, task, rows, *, definitions=None, material_states=None):
    from .evidence import fact_evidence_ids
    definitions = definitions if definitions is not None else {f["field_key"]: f for f in task.task_schema_json["fields"]}
    values = {row.id: task_value_service.value_of(db, task, task_value_service.RECORD_TASK_FACT, row.id) for row in rows}
    facts = fact_projection.facts_of_task_fact_rows(
        rows, values=values, field_definitions=definitions, material_states=material_states,
    )
    keys = {fact["field_key"] for fact in facts if fact["fact_kind"] == "EXTRACTED"}
    human_keys = set(db.scalars(select(TaskFieldValue.field_key).where(
        TaskFieldValue.audit_task_id == task.id, TaskFieldValue.is_current.is_(True),
        TaskFieldValue.field_key.in_(keys), TaskFieldValue.origin == "HUMAN_CORRECTION",
    ))) if keys else set()
    for row, fact in zip(rows, facts):
        fact["evidence_ids"] = fact_evidence_ids(db, task, row)
        if row.fact_kind == "EXTRACTED":
            fact["origin"] = "HUMAN_CORRECTION" if fact["field_key"] in human_keys else "DOCUMENT_EXTRACTION"
    return facts
