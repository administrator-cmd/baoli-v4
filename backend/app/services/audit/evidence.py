"""字段和规则的原文依据随业务取值保存于 CSV，不另建证据记录。"""
from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ...common.exceptions import BusinessError
from ...constants.execution import ORIGIN_HUMAN_CORRECTION
from ...entity.tasks import AuditTask, DocumentUpload, RuleResult, StoredArtifact, TaskDocument, TaskFieldValue
from . import value_store as task_value_service
from .shared import _locatable_text


def result_content_view(value: dict[str, Any]) -> dict[str, Any]:
    return dict(value)


def _check_evidence_content(texts: list[str], quote_text: str) -> str:
    """核对引文是否在实际读取内容中出现，不证明字段或结论正确。"""
    quoted = _locatable_text(quote_text)
    if not quoted:
        return "NOT_CHECKABLE"
    return "FOUND" if any(quoted in _locatable_text(text) for text in texts) else "NOT_FOUND"


def artifact_content_texts(content: Any) -> list[str]:
    if isinstance(content, str):
        return [content]
    if isinstance(content, dict):
        pages = content.get("pages")
        if isinstance(pages, list) and pages:
            # OCR 逐页正文；跨页引文要按整篇匹配，不能只在单页里找。
            joined = "\n\n".join(
                str(page.get("text") or "") for page in pages if isinstance(page, dict)
            )
            rest = [
                text for key, value in content.items() if key != "pages"
                for text in artifact_content_texts(value)
            ]
            return ([joined] if joined else []) + rest
        return [text for value in content.values() for text in artifact_content_texts(value)]
    if isinstance(content, list):
        return [text for value in content for text in artifact_content_texts(value)]
    return []


def _evidence_error(message: str, **details: Any) -> BusinessError:
    return BusinessError("TASK_EVIDENCE_INVALID", message, status_code=500, details=details or None)


def _evidence_item(item: Any) -> dict[str, Any]:
    if not isinstance(item, dict):
        raise _evidence_error("原文依据必须是对象")
    allowed = {"document_upload_id", "artifact_id", "quote_text", "field_path",
               "source_field_value_id", "source_rule_result_id"}
    if set(item) - allowed:
        raise _evidence_error("原文依据包含未声明字段", fields=sorted(set(item) - allowed))
    source_keys = [key for key in ("source_field_value_id", "source_rule_result_id") if key in item]
    if source_keys:
        if len(source_keys) != 1 or len(item) != 1:
            raise _evidence_error("原文依据引用只能声明一个来源，不重复携带正文")
        key = source_keys[0]
        value = item[key]
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise _evidence_error("原文依据来源编号必须是正整数", field=key)
        return {key: value}
    upload_id, artifact_id, quote = item.get("document_upload_id"), item.get("artifact_id"), item.get("quote_text")
    if isinstance(upload_id, bool) or not isinstance(upload_id, int) or upload_id <= 0:
        raise _evidence_error("原文依据必须包含有效的 document_upload_id")
    if artifact_id is not None and (isinstance(artifact_id, bool) or not isinstance(artifact_id, int) or artifact_id <= 0):
        raise _evidence_error("artifact_id 必须是正整数或空值")
    if not isinstance(quote, str):
        raise _evidence_error("quote_text 必须是字符串")
    result = {"document_upload_id": upload_id, "artifact_id": artifact_id, "quote_text": quote}
    if "field_path" in item:
        path = item["field_path"]
        if path is not None and not isinstance(path, str):
            raise _evidence_error("field_path 必须是字符串或空值")
        result["field_path"] = path
    return result


def _payload_items(db: Session, task: AuditTask, record_type: str, record_id: int) -> list[dict[str, Any]]:
    value = task_value_service.value_of(db, task, record_type, record_id)
    evidence = value.get("evidence", [])
    if not isinstance(evidence, list):
        raise _evidence_error("CSV 中的原文依据必须是数组", record_id=record_id)
    return [_evidence_item(item) for item in evidence]


def _dedupe(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result, seen = [], set()
    for item in items:
        identity = tuple(sorted(item.items()))
        if identity not in seen:
            seen.add(identity)
            result.append(item)
    return result


def _resolve_items(db: Session, task: AuditTask, items: list[dict[str, Any]],
                   seen: frozenset[tuple[str, int]] = frozenset()) -> list[dict[str, Any]]:
    result = []
    for item in items:
        source_key = next((key for key in ("source_field_value_id", "source_rule_result_id") if key in item), None)
        if source_key is not None:
            record_id = item[source_key]
            identity = (source_key, record_id)
            if identity in seen:
                raise _evidence_error("原文依据引用形成循环", record_id=record_id)
            entity = TaskFieldValue if source_key == "source_field_value_id" else RuleResult
            record_type = (task_value_service.RECORD_TASK_FIELD_VALUE if entity is TaskFieldValue
                           else task_value_service.RECORD_RULE_RESULT)
            source = db.get(entity, record_id)
            if source is None or source.audit_task_id != task.id:
                raise _evidence_error("原文依据引用的来源记录不存在或不属于当前任务", record_id=record_id)
            result.extend(_resolve_items(db, task, _payload_items(db, task, record_type, record_id), seen | {identity}))
            continue
        upload = db.get(DocumentUpload, item["document_upload_id"])
        if upload is None or upload.audit_task_id != task.id:
            raise _evidence_error("原文依据引用的上传文件不属于当前任务", document_upload_id=item["document_upload_id"])
        if item.get("artifact_id") is not None:
            artifact = db.get(StoredArtifact, item["artifact_id"])
            if artifact is None or artifact.audit_task_id != task.id or artifact.document_upload_id != upload.id:
                raise _evidence_error("原文依据的产物与来源文件不一致", artifact_id=item["artifact_id"])
        result.append(item)
    return _dedupe(result)


def field_evidence(db: Session, task: AuditTask, field_value_id: int) -> list[dict[str, Any]]:
    row = db.get(TaskFieldValue, field_value_id)
    if row is None or row.audit_task_id != task.id:
        raise _evidence_error("字段取值不属于当前任务", record_id=field_value_id)
    return _resolve_items(db, task, _payload_items(db, task, task_value_service.RECORD_TASK_FIELD_VALUE, row.id),
                          frozenset({("source_field_value_id", row.id)}))


def rule_evidence(db: Session, task: AuditTask, rule_result_id: int) -> list[dict[str, Any]]:
    row = db.get(RuleResult, rule_result_id)
    if row is None or row.audit_task_id != task.id:
        raise _evidence_error("规则结果不属于当前任务", record_id=rule_result_id)
    return _resolve_items(db, task, _payload_items(db, task, task_value_service.RECORD_RULE_RESULT, row.id),
                          frozenset({("source_rule_result_id", row.id)}))


def evidence_source_views(db: Session, task: AuditTask, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """为已解析的原文依据补充来源文件名和材料名，不生成证据编号或定位状态。"""
    result = []
    for raw in items:
        item = _evidence_item(raw)
        if "document_upload_id" not in item:
            raise _evidence_error("展示原文依据前必须先解析来源引用")
        upload = db.get(DocumentUpload, item["document_upload_id"])
        if upload is None or upload.audit_task_id != task.id:
            raise _evidence_error("原文依据的上传文件不属于当前任务", document_upload_id=item["document_upload_id"])
        document = db.get(TaskDocument, upload.task_document_id)
        if document is None or document.audit_task_id != task.id:
            raise _evidence_error("原文依据的材料不属于当前任务", document_upload_id=upload.id)
        result.append({**item, "document_name": upload.original_filename,
                       "material_name": document.material_name})
    return result


def save_artifact_references(db: Session, task: AuditTask, execution: Any, rule: dict[str, Any],
                             verdict: dict[str, Any] | None,
                             output_values: list[tuple[str, int, dict[str, Any]]]) -> tuple[list[tuple[str, int, dict[str, Any]]], list[dict[str, Any]]]:
    """抽取原文写字段 CSV；规则只引用字段版本，不复制字段或上游规则的原文。"""
    entries = {(kind, record_id): dict(payload) for kind, record_id, payload in output_values}
    definitions = {field["field_key"]: field for field in rule.get("output_definitions") or []}
    rule_items = []
    for reference in (verdict or {}).get("evidence") or []:
        if reference.get("source_field_value_id") is not None:
            field_evidence(db, task, reference["source_field_value_id"])
            rule_items.append({"source_field_value_id": reference["source_field_value_id"]})
            continue
        source_result_id = reference.get("source_rule_result_id", reference.get("rule_result_id"))
        if source_result_id is not None:
            rule_evidence(db, task, source_result_id)
            rule_items.append({"source_rule_result_id": source_result_id})
            continue
        artifact_id = reference.get("artifact_id")
        if artifact_id is None:
            if reference.get("tool_call_id"):
                # 计算/比对工具的业务依据已经保存在 basis，没有材料原文可另存。
                continue
            raise _evidence_error("原文依据缺少已保存的字段、规则或工具产物来源")
        artifact = db.get(StoredArtifact, artifact_id)
        if artifact is None or artifact.audit_task_id != task.id or artifact.status != "READY":
            raise BusinessError("RULE_EVIDENCE_ARTIFACT_INVALID", "引用的产物不属于当前任务的成功产物", status_code=500)
        item = {"document_upload_id": artifact.document_upload_id, "artifact_id": artifact.id,
                "quote_text": reference.get("quote_text") or ""}
        code = reference.get("output_code")
        if code is None:
            rule_items.append(item)
            continue
        definition = definitions[code]
        if reference.get("material_key") != definition["material_key"]:
            raise BusinessError("RULE_EVIDENCE_SOURCE_INVALID", "字段原文来源与声明材料不一致")
        source = db.scalar(select(TaskFieldValue).where(
            TaskFieldValue.audit_task_id == task.id, TaskFieldValue.field_key == code,
            TaskFieldValue.document_upload_id == artifact.document_upload_id,
            TaskFieldValue.extraction_execution_id == execution.id, TaskFieldValue.is_current.is_(True)))
        if source is None:
            manual = db.scalar(select(TaskFieldValue).where(
                TaskFieldValue.audit_task_id == task.id, TaskFieldValue.field_key == code,
                TaskFieldValue.material_key == definition["material_key"],
                TaskFieldValue.is_current.is_(True), TaskFieldValue.origin == ORIGIN_HUMAN_CORRECTION))
            if manual is None:
                raise BusinessError("RULE_EVIDENCE_SOURCE_INVALID", "抽取原文没有对应的本次字段取值版本")
            # 人工值保持自己的来源；本次材料引用只属于规则结论。
            rule_items.append(item)
            continue
        key = (task_value_service.RECORD_TASK_FIELD_VALUE, source.id)
        if key not in entries:
            entries[key] = dict(task_value_service.value_of(db, task, key[0], key[1]))
        payload = entries[key]
        payload["evidence"] = _dedupe([*(payload.get("evidence") or []), _evidence_item(item)])
        rule_items.append({"source_field_value_id": source.id})
    return [(kind, record_id, payload) for (kind, record_id), payload in entries.items()], _dedupe(rule_items)
