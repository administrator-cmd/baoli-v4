"""证据管理列表与关联对象查询。"""

from typing import Any
from sqlalchemy import and_, func, select
from sqlalchemy.orm import Session
from ...common.pagination import PageResult, page_result
from ...entity import AuditTask, DocumentUpload, Evidence, NodeExecution, RuleResult, TaskDocument, TaskFieldValue


def list_evidence_page(
    db: Session,
    *,
    task_no: str | None,
    reference_type: str | None,
    keyword: str | None,
    page: int,
    page_size: int,
) -> PageResult[dict[str, Any]]:
    """证据记录分页列表（跨任务）。"""
    conditions = []
    if task_no:
        conditions.append(AuditTask.task_no == task_no)
    # 分类口径与报告「证据引用」章节一致：带 rule_result_id 的证据属于规则判定证据。
    # 规则执行时会把字段证据复制给规则结果（两个外键同时存在），只按 field_value_id 判类会
    # 把它误显示成“字段事实”，并与筛选条件互相矛盾。
    if reference_type == "FIELD_FACT":
        conditions.append(and_(Evidence.field_value_id.is_not(None), Evidence.rule_result_id.is_(None)))
    elif reference_type == "RULE_RESULT":
        conditions.append(Evidence.rule_result_id.is_not(None))
    elif reference_type == "ARTIFACT":
        conditions.append(and_(Evidence.field_value_id.is_(None), Evidence.rule_result_id.is_(None)))
    if keyword:
        conditions.append(Evidence.quote_text.like(f"%{keyword.strip()}%"))

    count_statement = (
        select(func.count())
        .select_from(Evidence)
        .join(AuditTask, AuditTask.id == Evidence.audit_task_id)
        .where(*conditions)
    )
    total = db.scalar(count_statement) or 0
    rows = db.execute(
        select(Evidence, AuditTask, DocumentUpload, TaskDocument, RuleResult, TaskFieldValue)
        .join(AuditTask, AuditTask.id == Evidence.audit_task_id)
        .outerjoin(DocumentUpload, DocumentUpload.id == Evidence.document_upload_id)
        .outerjoin(TaskDocument, TaskDocument.id == DocumentUpload.task_document_id)
        .outerjoin(RuleResult, RuleResult.id == Evidence.rule_result_id)
        .outerjoin(TaskFieldValue, TaskFieldValue.id == Evidence.field_value_id)
        .where(*conditions)
        .order_by(Evidence.created_at.desc(), Evidence.id.desc())
        .limit(page_size)
        .offset((page - 1) * page_size)
    ).all()

    # 字段名来自任务自己的执行快照（field_key → name），与当时版本一致。
    task_ids = {task.id for _, task, _, _, _, _ in rows}
    field_names: dict[tuple[int, str], str] = {}
    if task_ids:
        for task in db.scalars(select(AuditTask).where(AuditTask.id.in_(task_ids))).all():
            for field in task.task_schema_json.get("fields", []):
                field_names[(task.id, field["field_key"])] = field["name"]

    items = []
    for evidence, task, upload, document, rule_result, fact in rows:
        # 规则证据优先：与报告口径一致（见上方筛选注释）。
        if rule_result is not None:
            reference_type_value = "RULE_RESULT"
            object_name = rule_result.rule_name
            material_name = None
        elif fact is not None:
            reference_type_value = "FIELD_FACT"
            object_name = field_names.get((task.id, fact.field_key), fact.field_key)
            material_name = document.material_name if document else None
        else:
            reference_type_value = "ARTIFACT"
            object_name = None
            material_name = document.material_name if document else None
        is_current_upload = bool(document and upload and document.current_upload_id == upload.id)
        is_current_target = False
        if rule_result is not None:
            is_current_target = (
                db.scalar(
                    select(NodeExecution.id).where(
                        NodeExecution.id == rule_result.node_execution_id,
                        NodeExecution.status != "INVALIDATED",
                    )
                )
                is not None
            )
        elif fact is not None:
            # 结构化字段的一条事实可以有多条属性证据，不能只把摘要证据标成当前。
            is_current_target = bool(fact.is_current)
        historical = not (is_current_upload and is_current_target)
        items.append(
            {
                "evidence_no": evidence.evidence_no,
                "task_no": task.task_no,
                "task_name": task.task_name,
                "reference_type": reference_type_value,
                "object_name": object_name,
                "material_name": material_name,
                "filename": upload.original_filename if upload else None,
                "document_upload_no": upload.upload_no if upload else None,
                "field_path": evidence.field_path,
                "quote_text": evidence.quote_text,
                "artifact_type": None,
                "historical": historical,
                "created_at": evidence.created_at,
            }
        )
    return page_result(items, total, page, page_size)
