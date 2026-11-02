"""字段核对：抽取完成后的人工核对、修正保存与确认。

核对是正常业务等待点，不是失败：抽取规则先执行并各自提交结果，人工修正只按现有
字段事实版本追加一条新版本，不失效当前执行批次、不重抽其他字段、也不另建一套字段存储。

确认记录绑定「当次执行批次 + 任务输入修订 + 字段版本摘要」：字段事实或材料版本一变，
旧确认仍保留为历史，但不再代表新输入。
"""

import hashlib
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ...common.exceptions import BusinessError
from ...constants.execution import ORIGIN_DOCUMENT_EXTRACTION, ORIGIN_HUMAN_CORRECTION
from ...constants.status import (
    EXECUTABLE_FIELD_STATUSES,
    FIELD_STATUS_NOT_PRESENT,
    FIELD_STATUS_REVIEWED,
)
from ...domain.identity import public_no
from ...entity.tasks import (
    AuditTask,
    FieldReviewConfirmation,
    ManualReview,
    NodeExecution,
    TaskFieldValue,
)
from . import value_store as task_value_service
from .facts import _current_field_fact_pairs, _write_human_field_fact
from .shared import _current_upload_map, _normalize_field_value, _task_documents, _task_field_definitions

DECISION_MODIFY = "MODIFY_FIELD"
DECISION_NOT_PRESENT = "MARK_NOT_PRESENT"

BASIS_TYPES = frozenset({"HUMAN_STATEMENT", "DOCUMENT_EVIDENCE", "UPLOAD_RECORD"})

# 未形成可执行取值的原因。页面按它说明「这项为什么是缺项」，不统一写成「未执行」。
MISSING_MATERIAL = "MATERIAL_NOT_UPLOADED"
MISSING_EXTRACTION_DISABLED = "EXTRACTION_NOT_ENABLED"
MISSING_EXTRACTION_FAILED = "EXTRACTION_NOT_COMPLETED"
MISSING_NOT_PRESENT = "NOT_PRESENT"
MISSING_INSUFFICIENT = "EVIDENCE_INSUFFICIENT"


def _field_field_key(field: dict[str, Any]) -> str:
    return str(field["field_key"])


def _missing_reason(status: str | None, *, uploaded: bool, enabled: bool) -> str | None:
    """缺项原因；能形成可执行取值时返回 None。"""
    if status in EXECUTABLE_FIELD_STATUSES:
        return None
    if not uploaded:
        return MISSING_MATERIAL
    if not enabled and status is None:
        return MISSING_EXTRACTION_DISABLED
    if status is None:
        return MISSING_EXTRACTION_FAILED
    if status == FIELD_STATUS_NOT_PRESENT:
        return MISSING_NOT_PRESENT
    return MISSING_INSUFFICIENT


def _extraction_versions(db: Session, task: AuditTask) -> dict[tuple[str, str], list[TaskFieldValue]]:
    """每个字段与材料位的模型抽取历史，按版本升序。"""
    rows = db.scalars(
        select(TaskFieldValue)
        .where(
            TaskFieldValue.audit_task_id == task.id,
            TaskFieldValue.scenario_version_id == task.scenario_version_id,
            TaskFieldValue.origin == ORIGIN_DOCUMENT_EXTRACTION,
        )
        .order_by(TaskFieldValue.revision_no, TaskFieldValue.id)
    ).all()
    versions: dict[tuple[str, str], list[TaskFieldValue]] = {}
    for row in rows:
        versions.setdefault((row.field_key, row.material_key), []).append(row)
    return versions


def _latest_reviews(db: Session, task: AuditTask) -> dict[int, ManualReview]:
    """字段版本对应的最近一条人工记录；一个版本可能有多次纠错，取最后一次。"""
    rows = db.scalars(
        select(ManualReview)
        .where(ManualReview.audit_task_id == task.id)
        .order_by(ManualReview.id)
    ).all()
    latest: dict[int, ManualReview] = {}
    for row in rows:
        latest[row.field_value_id] = row
    return latest


def _review_view(review: ManualReview) -> dict[str, Any]:
    return {
        "review_no": review.review_no,
        "decision": review.decision,
        "before_value": review.before_value,
        "after_value": review.after_value,
        "business_note": review.business_note,
        "basis_type": review.basis_type,
        "created_at": review.created_at,
    }


def field_review_supplement(
    db: Session, task: AuditTask,
    pairs: dict[tuple[str, str], tuple[TaskFieldValue, Any]] | None = None,
) -> dict[tuple[str, str], dict[str, Any]]:
    """字段核对需要的补充信息：原抽取值、人工前后值与原文依据归属。

    当前事实可能是人工修正版本；原抽取值仍按不可变历史读取，绝不被修正值覆盖。
    调用方已经算过当前字段事实时传 `pairs`，同一请求不再重复配对。
    """
    extracted = _extraction_versions(db, task)
    reviews = _latest_reviews(db, task)
    if pairs is None:
        pairs = _current_field_fact_pairs(db, task)
    supplement: dict[tuple[str, str], dict[str, Any]] = {}
    for field in task.task_schema_json["fields"]:
        key = (_field_field_key(field), str(field["material_key"]))
        current = pairs.get(key)
        origin = current[0].origin if current is not None else None
        # 原抽取值只取同一上传版本上的最后一版抽取结果：换过材料后，旧上传的抽取结果
        # 不再是本次输入的原文依据，展示它会让用户以为这次是从那份文件抽到的。
        source = next(
            (
                item for item in reversed(extracted.get(key, []))
                if current is not None and item.document_upload_id == current[0].document_upload_id
            ),
            None,
        )
        source_value = (
            task_value_service.value_of(
                db, task, task_value_service.RECORD_TASK_FIELD_VALUE, source.id,
            )
            if source is not None else {}
        )
        # 人工修正值没有自己的引文，原文依据仍属于它替代的那一版抽取结果。
        evidence_value_id = (
            current[0].id if current is not None and origin != ORIGIN_HUMAN_CORRECTION
            else source.id if source is not None else None
        )
        supplement[key] = {
            "extracted_value_id": source.id if source is not None else None,
            "extracted_status": source.status if source is not None else None,
            "extracted_raw_value": source_value.get("raw_value"),
            "extracted_normalized_value": source_value.get("normalized_value_json"),
            "evidence_value_id": evidence_value_id,
            "manual_review": (
                _review_view(reviews[current[0].id])
                if current is not None and current[0].id in reviews else None
            ),
        }
    return supplement


def missing_fields(
    db: Session, task: AuditTask,
    pairs: dict[tuple[str, str], tuple[TaskFieldValue, Any]] | None = None,
) -> list[dict[str, Any]]:
    """当前仍不能作为下游有效输入的字段：按业务名称与原因列出真实缺项。"""
    if pairs is None:
        pairs = _current_field_fact_pairs(db, task)
    uploads = _current_upload_map(db, task)
    documents = {item.material_key: item for item in _task_documents(db, task)}
    extracted_keys = {
        field["field_key"] for field in task.task_schema_json["fields"] if field.get("extraction_enabled")
    }
    items: list[dict[str, Any]] = []
    for field in task.task_schema_json["fields"]:
        key = (_field_field_key(field), str(field["material_key"]))
        pair = pairs.get(key)
        status = pair[1].status if pair is not None else None
        document = documents.get(key[1])
        uploaded = document is not None and uploads.get(document.id) is not None
        reason = _missing_reason(
            status, uploaded=uploaded, enabled=key[0] in extracted_keys,
        )
        if reason is None:
            continue
        items.append({
            "field_key": key[0],
            "material_key": key[1],
            "field_name": str(field.get("name") or key[0]),
            "material_name": document.material_name if document is not None else key[1],
            "status": status,
            "reason": reason,
            "material_uploaded": uploaded,
        })
    return items


def field_review_digest(
    db: Session, task: AuditTask,
    pairs: dict[tuple[str, str], tuple[TaskFieldValue, Any]] | None = None,
) -> str:
    """当前字段版本摘要：字段、材料、上传版本、事实版本与状态的确定性组合。

    值本身不可变，版本变化一定伴随新的 revision_no，因此不需要把业务取值读进摘要。
    """
    if pairs is None:
        pairs = _current_field_fact_pairs(db, task)
    parts = [
        "|".join((
            field_key, material_key,
            str(value.document_upload_id), str(value.revision_no), value.status,
            str(fact.revision_no), fact.status,
        ))
        for (field_key, material_key), (value, fact) in sorted(pairs.items())
    ]
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()


def latest_confirmation(
    db: Session, task: AuditTask, execution_id: int | None,
) -> FieldReviewConfirmation | None:
    """该批次最近一次确认；更早的确认保留为历史，不代表当前输入。"""
    if execution_id is None:
        return None
    return db.scalar(
        select(FieldReviewConfirmation)
        .where(
            FieldReviewConfirmation.audit_task_id == task.id,
            FieldReviewConfirmation.node_execution_id == execution_id,
        )
        .order_by(FieldReviewConfirmation.id.desc())
        .limit(1)
    )


def confirmation_is_current(
    db: Session, task: AuditTask, execution: NodeExecution | None,
    pairs: dict[tuple[str, str], tuple[TaskFieldValue, Any]] | None = None,
) -> bool:
    """当前批次是否已在当前输入与字段版本上被确认。"""
    if execution is None:
        return False
    confirmation = latest_confirmation(db, task, execution.id)
    if confirmation is None:
        return False
    if int(confirmation.input_revision) != int(task.input_revision):
        return False
    return confirmation.field_digest == field_review_digest(db, task, pairs)


REVIEW_STATE_KEY = "field_review"


def extraction_closed(execution: NodeExecution | None) -> bool:
    """该批次是否已经跑完抽取阶段并停在过字段核对等待点。

    抽取阶段结束后不再重跑抽取规则：人工补填的取值不能被模型重新抽取覆盖，
    也不该在已核对过的字段上重复计费。要重新抽取只能对整批重新执行。
    """
    if execution is None:
        return False
    state = (execution.result_json or {}).get(REVIEW_STATE_KEY)
    return isinstance(state, dict) and bool(state.get("closed_at"))


def _check_correction_shape(field: dict[str, Any], correction: Any) -> None:
    if correction.decision not in {DECISION_MODIFY, DECISION_NOT_PRESENT}:
        raise BusinessError(
            "FIELD_REVIEW_DECISION_INVALID", "字段核对只支持修改取值或标记为材料中不存在",
            status_code=400, details={"field_key": correction.field_key, "decision": correction.decision},
        )
    if correction.basis_type not in BASIS_TYPES:
        raise BusinessError(
            "FIELD_REVIEW_BASIS_INVALID", "人工依据类型不在允许范围内",
            status_code=400, details={"basis_type": correction.basis_type},
        )
    if correction.decision == DECISION_MODIFY and not (correction.field_value or "").strip():
        raise BusinessError(
            "FIELD_REVIEW_VALUE_REQUIRED",
            f"字段「{field.get('name') or correction.field_key}」修改取值时必须填写核对后的值",
            details={"field_key": correction.field_key},
        )
    if correction.decision == DECISION_NOT_PRESENT and (correction.field_value or "").strip():
        raise BusinessError(
            "FIELD_REVIEW_VALUE_UNEXPECTED",
            f"字段「{field.get('name') or correction.field_key}」标记为材料中不存在时不能填写取值",
            details={"field_key": correction.field_key},
        )


def _normalized_value(field: dict[str, Any], correction: Any) -> Any:
    if correction.decision != DECISION_MODIFY:
        return None
    try:
        return _normalize_field_value(
            str(field["value_type"]),
            str(correction.field_value).strip(),
            unit=field.get("unit"),
            currency=field.get("currency"),
            extraction_strategy=field.get("extraction_strategy"),
            structure_schema=field.get("structure_schema"),
        )
    except (ValueError, ArithmeticError) as exc:
        raise BusinessError(
            "FIELD_REVIEW_VALUE_INVALID",
            f"字段「{field.get('name') or correction.field_key}」的核对值不符合字段类型：{exc}",
            details={
                "field_key": correction.field_key,
                "value_type": field["value_type"],
                "unit": field.get("unit"),
                "currency": field.get("currency"),
            },
        ) from exc


def apply_corrections(
    db: Session, task: AuditTask, corrections: list[Any], *, operation_no: str | None = None,
) -> list[dict[str, Any]]:
    """保存核对修改：每个字段新增一版人工事实，旧版本退出当前位但保留历史。

    与「字段纠错」不同，这里不失效当前执行批次、不重算下游：后续规则尚未执行，
    它们读到的就是核对后的有效值。调用方负责在同一事务里提交，并递增输入修订。
    """
    if not corrections:
        return []
    fields = _task_field_definitions(task)
    documents = {item.material_key: item for item in _task_documents(db, task)}
    uploads = _current_upload_map(db, task)
    pairs = _current_field_fact_pairs(db, task)
    seen: set[tuple[str, str]] = set()
    applied: list[dict[str, Any]] = []
    for correction in corrections:
        field = fields.get(correction.field_key)
        if field is None or str(field["material_key"]) != correction.material_key:
            raise BusinessError(
                "FIELD_FACT_MISSING", "核对字段不在当前任务的字段清单中",
                status_code=404,
                details={"field_key": correction.field_key, "material_key": correction.material_key},
            )
        key = (correction.field_key, correction.material_key)
        if key in seen:
            raise BusinessError(
                "FIELD_REVIEW_CORRECTION_DUPLICATED", "同一字段在同一次提交里出现了多条修改",
                status_code=400, details={"field_key": correction.field_key},
            )
        seen.add(key)
        _check_correction_shape(field, correction)
        document = documents.get(correction.material_key)
        upload = uploads.get(document.id) if document is not None else None
        if upload is None:
            raise BusinessError(
                "FIELD_REVIEW_MATERIAL_MISSING",
                f"材料「{document.material_name if document is not None else correction.material_key}」"
                "尚未上传当前文件，不能补充字段值；请先上传材料",
                status_code=409,
                details={"field_key": correction.field_key, "material_key": correction.material_key},
            )
        current = pairs.get(key)
        current_revision = current[0].revision_no if current is not None else None
        if current_revision != correction.expected_revision_no:
            raise BusinessError(
                "FIELD_REVISION_CONFLICT",
                f"字段「{field.get('name') or correction.field_key}」的取值已被其他操作更新，"
                "本次修改基于过期页面；请刷新后重新核对",
                status_code=409,
                details={
                    "field_key": correction.field_key,
                    "material_key": correction.material_key,
                    "submitted": correction.expected_revision_no,
                    "current": current_revision,
                },
            )
        normalized = _normalized_value(field, correction)
        status = FIELD_STATUS_NOT_PRESENT if correction.decision == DECISION_NOT_PRESENT else FIELD_STATUS_REVIEWED
        before_value = (
            task_value_service.value_of(
                db, task, task_value_service.RECORD_TASK_FIELD_VALUE, current[0].id,
            ).get("raw_value")
            if current is not None else None
        )
        new_fact, previous_revision_no, _invalidated = _write_human_field_fact(
            db,
            task,
            field_key=correction.field_key,
            material_key=correction.material_key,
            document_upload_id=upload.id,
            status=status,
            raw_value=None if status == FIELD_STATUS_NOT_PRESENT else str(correction.field_value).strip(),
            normalized_value=normalized,
        )
        review = ManualReview(
            review_no=public_no("REV"),
            audit_task_id=task.id,
            field_value_id=new_fact.id,
            status="RESOLVED",
            system_judgment_json={
                "status": current[1].status if current is not None else None,
                "raw_value": before_value,
                "source_field_value_id": current[0].id if current is not None else None,
            },
            decision=correction.decision,
            business_note=correction.business_note,
            before_value=before_value,
            after_value=None if status == FIELD_STATUS_NOT_PRESENT else str(correction.field_value).strip(),
            basis_type=correction.basis_type,
            decision_command_no=operation_no,
        )
        db.add(review)
        db.flush()
        applied.append({
            "field_key": correction.field_key,
            "material_key": correction.material_key,
            "review_no": review.review_no,
            "fact_revision_no": new_fact.revision_no,
            "previous_revision_no": previous_revision_no,
            "before_value": before_value,
            "after_value": review.after_value,
            "status": status,
        })
    task.input_revision = int(task.input_revision or 0) + 1
    db.flush()
    return applied


def record_confirmation(
    db: Session, task: AuditTask, execution: NodeExecution, *,
    note: str | None, operation_no: str | None,
) -> FieldReviewConfirmation:
    """在当前字段版本上记录一次核对确认；缺项数量与字段一起写入。"""
    missing = missing_fields(db, task)
    confirmation = FieldReviewConfirmation(
        confirmation_no=public_no("FRV"),
        audit_task_id=task.id,
        node_execution_id=execution.id,
        input_revision=int(task.input_revision),
        field_digest=field_review_digest(db, task),
        missing_field_count=len(missing),
        missing_field_keys=[item["field_key"] for item in missing],
        note=note,
        operation_no=operation_no,
    )
    db.add(confirmation)
    db.flush()
    return confirmation


def confirmation_view(
    db: Session, task: AuditTask, execution: NodeExecution | None, *,
    required: bool, after_review_rule_count: int,
    pairs: dict[tuple[str, str], tuple[TaskFieldValue, Any]] | None = None,
) -> dict[str, Any]:
    """任务详情展示的核对状态；页面只据此显示入口与缺项，不自行推导范围。"""
    if pairs is None:
        pairs = _current_field_fact_pairs(db, task)
    confirmation = latest_confirmation(db, task, execution.id if execution is not None else None)
    current = (
        confirmation is not None
        and int(confirmation.input_revision) == int(task.input_revision)
        and confirmation.field_digest == field_review_digest(db, task, pairs)
    )
    missing = missing_fields(db, task, pairs) if required else []
    reason = None
    if confirmation is not None and not current:
        reason = "字段版本或材料输入已变化，上一次核对确认不再代表当前输入，需要重新核对"
    stale_required = required and execution is not None and execution.status == "WAITING_FIELD_REVIEW" and not current
    return {
        "required": required,
        "confirmed": bool(current),
        "confirmation_no": confirmation.confirmation_no if confirmation is not None else None,
        "confirmed_at": confirmation.created_at if confirmation is not None else None,
        "confirmed_input_revision": int(confirmation.input_revision) if confirmation is not None else None,
        "input_revision": int(task.input_revision),
        "missing_field_count": len(missing),
        "missing_fields": missing,
        "after_review_rule_count": int(after_review_rule_count),
        "reconfirm_reason": reason,
        "waiting": bool(stale_required),
    }
