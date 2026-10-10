from typing import Any

from sqlalchemy.orm import Session

from ..repositories import task_repository
from .task_lookup import task_or_404
from .audit.evidence import review_content_view


def task_reviews_view(db: Session, task_no: str) -> dict[str, Any]:
    """组装任务的人工复核视图；API 不感知查询表和关联方式。"""
    task = task_or_404(db, task_no)
    rows = task_repository.review_rows(db, task.id)
    rule_names = {item.id: item.rule_name for item in rows.rules}
    fact_keys = {item.id: (item.field_key, item.material_key) for item in rows.fields}
    field_names = {item["field_key"]: item["name"] for item in task.task_schema_json.get("fields", [])}
    documents = {item.material_key: item.material_name for item in rows.documents}

    items: list[dict[str, Any]] = []
    for review in rows.reviews:
        judgment = review_content_view(review.system_judgment_json if isinstance(review.system_judgment_json, dict) else {})
        if review.rule_result_id is not None:
            object_name = f"规则：{rule_names.get(review.rule_result_id, '未知规则')}"
            object_kind = "RULE"
        elif review.field_value_id is not None:
            field_key, material_key = fact_keys.get(review.field_value_id, (None, None))
            material_name = documents.get(material_key, material_key or "未知材料")
            object_name = f"字段：{material_name}·{field_names.get(field_key, field_key or '未知字段')}"
            object_kind = "FIELD"
        elif review.document_upload_id is not None:
            object_name = "材料文件"
            object_kind = "MATERIAL"
        else:
            object_name = "未关联业务对象"
            object_kind = "UNKNOWN"
        items.append(
            {
                "review_no": review.review_no,
                "review_type": review.review_type,
                "object_name": object_name,
                "object_kind": object_kind,
                "status": review.status,
                "system_judgment": {
                    "status": judgment.get("status"),
                    "message": judgment.get("message"),
                    "raw_value": judgment.get("raw_value"),
                    "normalized_value": judgment.get("normalized_value"),
                    "evidence": judgment.get("evidence"),
                    "evidences": judgment.get("evidences") if isinstance(judgment.get("evidences"), list) else [],
                    "retrieval": judgment.get("retrieval"),
                    "seal_records": judgment.get("seal_records"),
                    "subject_query": judgment.get("subject_query"),
                    "service_code": judgment.get("service_code"),
                    "service_result": judgment.get("service_result"),
                },
                "decision": review.decision,
                "business_note": review.business_note,
                "before_value": review.before_value,
                "after_value": review.after_value,
                "basis_type": review.basis_type,
                "decision_command_no": review.decision_command_no,
                "created_at": review.created_at,
                "updated_at": review.updated_at,
            }
        )
    return {"task_no": task.task_no, "items": items}

