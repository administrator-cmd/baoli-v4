"""字段和规则的原文依据随业务取值保存于 CSV，不另建证据记录。"""
from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.common.exceptions import BusinessError
from app.constants.execution import ORIGIN_HUMAN_CORRECTION
from app.modules.audit.artifact.crud import AuditArtifactCRUD
from app.modules.audit.artifact.model import StoredArtifact
from app.modules.audit.execution.model import NodeExecution
from app.modules.audit.field.crud import AuditFieldCRUD
from app.modules.audit.field.model import TaskFieldValue
from app.modules.audit.field.service import value_store as task_value_service
from app.modules.audit.rule.crud import AuditRuleCRUD
from app.modules.audit.rule.model import RuleResult
from app.modules.audit.shared import _locatable_text
from app.modules.audit.task.model import AuditTask
from app.modules.material.document.crud import MaterialDocumentCRUD
from app.modules.material.document.model import DocumentUpload, TaskDocument


class AuditFieldEvidenceService:
    """校验、保存并展开字段与规则的原文来源。"""

    def __init__(self, db: Session | None = None) -> None:
        self.db = db

    def result_content_view(self, value: dict[str, Any]) -> dict[str, Any]:
        return dict(value)


    def _check_evidence_content(self, texts: list[str], quote_text: str) -> str:
        """核对引文是否在实际读取内容中出现，不证明字段或结论正确。"""
        return self._check_evidence_contents(texts, [quote_text])[0]


    def _check_evidence_contents(self, texts: list[str], quote_texts: list[str]) -> list[str]:
        """一次规范化材料正文并检查多个独立引文，避免每个片段重扫整份 OCR。"""
        normalized_texts = tuple(_locatable_text(text) for text in texts)
        results = []
        for quote_text in quote_texts:
            quoted = _locatable_text(quote_text)
            if not quoted:
                results.append("NOT_CHECKABLE")
            else:
                results.append("FOUND" if any(quoted in text for text in normalized_texts) else "NOT_FOUND")
        return results


    def artifact_content_texts(self, content: Any) -> list[str]:
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
                    for text in self.artifact_content_texts(value)
                ]
                return ([joined] if joined else []) + rest
            return [text for value in content.values() for text in self.artifact_content_texts(value)]
        if isinstance(content, list):
            return [text for value in content for text in self.artifact_content_texts(value)]
        return []


    def _evidence_error(self, message: str, **details: Any) -> BusinessError:
        return BusinessError("TASK_EVIDENCE_INVALID", message, status_code=500, details=details or None)


    def _evidence_item(self, item: Any) -> dict[str, Any]:
        if not isinstance(item, dict):
            raise self._evidence_error("原文依据必须是对象")
        allowed = {"document_upload_id", "artifact_id", "quote_text", "quote_label", "quote_header",
                   "quote_period", "quote_statement_scope", "quote_entity_scope", "quote_unit", "artifact_page", "field_path",
                   "source_field_value_id", "source_rule_result_id"}
        if set(item) - allowed:
            raise self._evidence_error("原文依据包含未声明字段", fields=sorted(set(item) - allowed))
        source_keys = [key for key in ("source_field_value_id", "source_rule_result_id") if key in item]
        if source_keys:
            if len(source_keys) != 1 or len(item) != 1:
                raise self._evidence_error("原文依据引用只能声明一个来源，不重复携带正文")
            key = source_keys[0]
            value = item[key]
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise self._evidence_error("原文依据来源编号必须是正整数", field=key)
            return {key: value}
        upload_id, artifact_id, quote = item.get("document_upload_id"), item.get("artifact_id"), item.get("quote_text")
        if isinstance(upload_id, bool) or not isinstance(upload_id, int) or upload_id <= 0:
            raise self._evidence_error("原文依据必须包含有效的 document_upload_id")
        if artifact_id is not None and (isinstance(artifact_id, bool) or not isinstance(artifact_id, int) or artifact_id <= 0):
            raise self._evidence_error("artifact_id 必须是正整数或空值")
        if not isinstance(quote, str):
            raise self._evidence_error("quote_text 必须是字符串")
        result = {"document_upload_id": upload_id, "artifact_id": artifact_id, "quote_text": quote}
        for key in ("quote_label", "quote_header", "quote_period", "quote_statement_scope", "quote_entity_scope", "quote_unit"):
            if key in item:
                value = item[key]
                if value is not None and not isinstance(value, str):
                    raise self._evidence_error(f"{key} 必须是字符串或空值")
                result[key] = value
        if "artifact_page" in item:
            page = item["artifact_page"]
            if page is not None and (isinstance(page, bool) or not isinstance(page, int) or page < 0):
                raise self._evidence_error("artifact_page 必须是非负整数或空值")
            result["artifact_page"] = page
        if "field_path" in item:
            path = item["field_path"]
            if path is not None and not isinstance(path, str):
                raise self._evidence_error("field_path 必须是字符串或空值")
            result["field_path"] = path
        return result


    def _payload_items(self, task: AuditTask, record_type: str, record_id: int) -> list[dict[str, Any]]:
        value = task_value_service.AuditFieldValueStoreService(self.db).value_of(task, record_type, record_id)
        evidence = value.get("evidence", [])
        if not isinstance(evidence, list):
            raise self._evidence_error("CSV 中的原文依据必须是数组", record_id=record_id)
        return [self._evidence_item(item) for item in evidence]


    def _dedupe(self, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        result, seen = [], set()
        for item in items:
            identity = tuple(sorted(item.items()))
            if identity not in seen:
                seen.add(identity)
                result.append(item)
        return result


    def _resolve_items(self, task: AuditTask, items: list[dict[str, Any]],
                       seen: frozenset[tuple[str, int]] = frozenset(),
                       memo: dict[tuple[str, int], list[dict[str, Any]]] | None = None) -> list[dict[str, Any]]:
        result = []
        for item in items:
            source_key = next((key for key in ("source_field_value_id", "source_rule_result_id") if key in item), None)
            if source_key is not None:
                record_id = item[source_key]
                identity = (source_key, record_id)
                if identity in seen:
                    raise self._evidence_error("原文依据引用形成循环", record_id=record_id)
                cached = memo.get(identity) if memo is not None else None
                if cached is not None:
                    result.extend(cached)
                    continue
                entity = TaskFieldValue if source_key == "source_field_value_id" else RuleResult
                record_type = (task_value_service.RECORD_TASK_FIELD_VALUE if entity is TaskFieldValue
                               else task_value_service.RECORD_RULE_RESULT)
                source = AuditFieldCRUD(self.db).get_of(entity, record_id)
                if source is None or source.audit_task_id != task.id:
                    raise self._evidence_error("原文依据引用的来源记录不存在或不属于当前任务", record_id=record_id)
                resolved = self._resolve_items( task, self._payload_items( task, record_type, record_id),
                                          seen | {identity}, memo)
                if memo is not None:
                    memo[identity] = resolved
                result.extend(resolved)
                continue
            upload = MaterialDocumentCRUD(self.db).get_of(DocumentUpload, item["document_upload_id"])
            if upload is None or upload.audit_task_id != task.id:
                raise self._evidence_error("原文依据引用的上传文件不属于当前任务", document_upload_id=item["document_upload_id"])
            if item.get("artifact_id") is not None:
                artifact = AuditArtifactCRUD(self.db).get_of(StoredArtifact, item["artifact_id"])
                if artifact is None or artifact.audit_task_id != task.id or artifact.document_upload_id != upload.id:
                    raise self._evidence_error("原文依据的产物与来源文件不一致", artifact_id=item["artifact_id"])
            result.append(item)
        return self._dedupe(result)


    def evidence_memo(self) -> dict[tuple[str, int], list[dict[str, Any]]]:
        """同一请求内复用已展开的证据链。

        多个结果引用同一条上游时不重复递归读取；不做跨请求缓存，避免复用历史原文。
        """
        return {}


    def field_evidence(self, task: AuditTask, field_value_id: int,
                       memo: dict[tuple[str, int], list[dict[str, Any]]] | None = None) -> list[dict[str, Any]]:
        row = AuditFieldCRUD(self.db).get_of(TaskFieldValue, field_value_id)
        if row is None or row.audit_task_id != task.id:
            raise self._evidence_error("字段取值不属于当前任务", record_id=field_value_id)
        return self._resolve_items( task, self._payload_items( task, task_value_service.RECORD_TASK_FIELD_VALUE, row.id),
                              frozenset({("source_field_value_id", row.id)}), memo)


    def rule_evidence(self, task: AuditTask, rule_result_id: int,
                      memo: dict[tuple[str, int], list[dict[str, Any]]] | None = None) -> list[dict[str, Any]]:
        row = AuditRuleCRUD(self.db).get_of(RuleResult, rule_result_id)
        if row is None or row.audit_task_id != task.id:
            raise self._evidence_error("规则结果不属于当前任务", record_id=rule_result_id)
        return self._resolve_items( task, self._payload_items( task, task_value_service.RECORD_RULE_RESULT, row.id),
                              frozenset({("source_rule_result_id", row.id)}), memo)


    def evidence_source_views(self, task: AuditTask, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """为已解析的原文依据补充来源文件名和材料名，不生成证据编号或定位状态。"""
        result = []
        for raw in items:
            item = self._evidence_item(raw)
            if "document_upload_id" not in item:
                raise self._evidence_error("展示原文依据前必须先解析来源引用")
            upload = MaterialDocumentCRUD(self.db).get_of(DocumentUpload, item["document_upload_id"])
            if upload is None or upload.audit_task_id != task.id:
                raise self._evidence_error("原文依据的上传文件不属于当前任务", document_upload_id=item["document_upload_id"])
            document = MaterialDocumentCRUD(self.db).get_of(TaskDocument, upload.task_document_id)
            if document is None or document.audit_task_id != task.id:
                raise self._evidence_error("原文依据的材料不属于当前任务", document_upload_id=upload.id)
            result.append({**item, "document_name": upload.original_filename,
                           "material_name": document.material_name})
        return result


    def _attach_field_source(
        self, task: AuditTask, execution: NodeExecution, output_code: str | None,
        source: dict[str, Any], field_outputs: dict[str, tuple[TaskFieldValue, dict[str, Any]]],
    ) -> dict[str, Any]:
        """字段引用写入新字段版本，规则再引用该版本。"""
        if output_code is None:
            return source
        output = field_outputs.get(output_code)
        if output is not None:
            field, payload = output
            if field.audit_task_id != task.id or field.extraction_execution_id != execution.id:
                raise BusinessError("RULE_EVIDENCE_SOURCE_INVALID", "字段证据与本次执行不一致")
            if source.get("source_field_value_id") == field.id:
                raise BusinessError("RULE_EVIDENCE_SOURCE_INVALID", "字段不能引用自身作为取值来源")
            payload["evidence"] = self._dedupe([*(payload.get("evidence") or []), source])
            return {"source_field_value_id": field.id}
        # 人工值没有本次抽取版本，新增引文仅属于规则结论。
        if output_code in AuditFieldCRUD(self.db).human_keys_for(task.id, {output_code}, ORIGIN_HUMAN_CORRECTION):
            return source
        raise BusinessError("RULE_EVIDENCE_SOURCE_INVALID", "字段证据没有对应的本次产出版本")

    def _result_field_source(self, task: AuditTask, result_id: int, definition: dict[str, Any]) -> dict[str, int]:
        """上游字段取值只继承该字段的证据，不继承整条规则的其他材料。"""
        candidates = set()
        for item in self._payload_items(task, task_value_service.RECORD_RULE_RESULT, result_id):
            source_id = item.get("source_field_value_id")
            if source_id is None:
                continue
            field = AuditFieldCRUD(self.db).get(source_id)
            if (field is not None and field.audit_task_id == task.id
                    and field.field_key == definition["field_key"]
                    and field.material_key == definition["material_key"]):
                candidates.add(field.id)
        if len(candidates) != 1:
            raise BusinessError("RULE_EVIDENCE_SOURCE_INVALID", "上游结果没有唯一对应的字段证据版本")
        return {"source_field_value_id": candidates.pop()}

    def save_artifact_references(self, task: AuditTask, execution: NodeExecution, rule: dict[str, Any],
                                 verdict: dict[str, Any] | None,
                                 output_values: list[tuple[str, int, dict[str, Any]]]) -> tuple[list[tuple[str, int, dict[str, Any]]], list[dict[str, Any]]]:
        """抽取原文写字段 CSV；规则只引用字段版本，不复制字段或上游规则的原文。"""
        entries = {(kind, record_id): dict(payload) for kind, record_id, payload in output_values}
        field_outputs = {}
        for (kind, record_id), payload in entries.items():
            if kind == task_value_service.RECORD_TASK_FIELD_VALUE:
                field = AuditFieldCRUD(self.db).get(record_id)
                if field is None:
                    raise BusinessError("RULE_EVIDENCE_SOURCE_INVALID", "本次字段产出索引不存在")
                field_outputs[field.field_key] = (field, payload)
        definitions = {field["field_key"]: field for field in rule.get("output_definitions") or []}
        rule_items = []
        for reference in (verdict or {}).get("evidence") or []:
            if reference.get("source_field_value_id") is not None:
                self.field_evidence(task, reference["source_field_value_id"])
                rule_items.append(self._attach_field_source(
                    task, execution, reference.get("output_code"),
                    {"source_field_value_id": reference["source_field_value_id"]}, field_outputs,
                ))
                continue
            source_result_id = reference.get("source_rule_result_id", reference.get("rule_result_id"))
            if source_result_id is not None:
                self.rule_evidence(task, source_result_id)
                output_code = reference.get("output_code")
                source = (
                    self._result_field_source(task, source_result_id, definitions[output_code])
                    if output_code is not None else {"source_rule_result_id": source_result_id}
                )
                rule_items.append(self._attach_field_source(
                    task, execution, output_code, source, field_outputs,
                ))
                continue
            artifact_id = reference.get("artifact_id")
            if artifact_id is None:
                if reference.get("tool_call_id"):
                    # 计算/比对工具的业务依据已经保存在 basis，没有材料原文可另存。
                    continue
                raise self._evidence_error("原文依据缺少已保存的字段、规则或工具产物来源")
            artifact = AuditArtifactCRUD(self.db).get_of(StoredArtifact, artifact_id)
            if artifact is None or artifact.audit_task_id != task.id or artifact.status != "READY":
                raise BusinessError("RULE_EVIDENCE_ARTIFACT_INVALID", "引用的产物不属于当前任务的成功产物", status_code=500)
            item = {"document_upload_id": artifact.document_upload_id, "artifact_id": artifact.id,
                    "quote_text": reference.get("quote_text") or ""}
            for key in ("quote_label", "quote_header", "quote_period", "quote_statement_scope", "quote_entity_scope", "quote_unit", "artifact_page"):
                if reference.get(key) is not None:
                    item[key] = reference[key]
            code = reference.get("output_code")
            if code is None:
                rule_items.append(item)
                continue
            definition = definitions[code]
            if reference.get("material_key") != definition["material_key"]:
                raise BusinessError("RULE_EVIDENCE_SOURCE_INVALID", "字段原文来源与声明材料不一致")
            output = field_outputs.get(code)
            if output is not None and output[0].document_upload_id != artifact.document_upload_id:
                raise BusinessError("RULE_EVIDENCE_SOURCE_INVALID", "字段产出与证据上传版本不一致")
            rule_items.append(self._attach_field_source(
                task, execution, code, self._evidence_item(item), field_outputs,
            ))
        return [(kind, record_id, payload) for (kind, record_id), payload in entries.items()], self._dedupe(rule_items)
