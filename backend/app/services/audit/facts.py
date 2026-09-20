"""字段事实与字段抽取。

事实不可变：每次抽取或人工裁决都**新增一个版本**，旧行退出当前位但保留可追溯。
唯一键 (任务, 字段, 材料位) 上只有一条 `is_current`，且该范围**不含上传版本**——
旧上传版本的当前事实也必须让位，否则新事实写不进去。

原文是事实，标准化值由服务端按字段声明的类型/单位/币种对原文重新计算：
模型给出的 `normalized_value` 只作建议，与重算结果不一致时按 NORMALIZATION_FAILED
进人工确认，绝不采用模型值。"""

from ...domain.audit_vocab import SEAL_ARTIFACT_SOURCE, declared_artifact_source
from ...domain.identity import public_no
from ...domain.manifest import OPERATOR_LABELS, comparison_label
from ...errors import BusinessError
from ...models import AuditTask, DocumentUpload, Evidence, ManualReview, NodeExecution, RuleResult, StoredArtifact, TaskDocument, TaskFieldValue
from ..llm_extraction import extract_document_fields
from .artifacts import _artifact_payload
from .evidence import _evidence_snapshot, _verify_evidence_location
from .shared import _applicability_json, _applicability_state, _current_uploads, _extraction_fact_value, _material_names, _not_applicable_message, _task_field_definitions
from .state import _material_states
from sqlalchemy import select, update
from sqlalchemy.orm import Session
from typing import Any


def _material_check(db: Session, task: AuditTask, manifest: dict[str, Any], execution: NodeExecution) -> dict[str, Any]:
    pairs = _current_uploads(db, task)
    available = {document.material_key for document, upload in pairs if upload is not None}
    documents = {document.material_key: document for document, _ in pairs}
    material_names = _material_names(manifest)
    required_missing = [document.material_name for document, upload in pairs if document.required and upload is None]
    enabled_keys = set(task.task_schema_json["enabled_rule_keys"])
    rules = [
        rule
        for rule in manifest["rules"]
        if rule["node"] == "MATERIAL_CHECK" and rule["operator"] == "MATERIAL_EXISTS" and rule["rule_key"] in enabled_keys
    ]
    counts: dict[str, int] = {}
    for rule in rules:
        scoped = set(rule["materials"])
        missing = scoped - available
        state = _applicability_state(rule, available)
        missing_names = "、".join(material_names.get(key, key) for key in sorted(missing))
        uploaded_scoped = sorted(scoped - missing)
        uploaded_names = "、".join(material_names.get(key, key) for key in uploaded_scoped)
        if not state["applicable"]:
            # 已配置规则但适用条件明确不成立：记录不适用并保留原因，绝不写成通过。
            status = "NOT_APPLICABLE"
            message = _not_applicable_message(state, material_names)
        elif not missing:
            status = "PASSED"
            message = "所需材料已上传"
        elif all(not documents[key].required for key in missing):
            # 始终适用的材料检查缺少可选材料时属于缺输入，不能判定通过。
            status = "EVIDENCE_INSUFFICIENT"
            message = f"可选材料尚未上传，材料检查缺少输入，不能判定通过：{missing_names}"
        else:
            status = "ABNORMAL"
            message = f"所需材料未上传：{missing_names}"
        # 审核标准 = 这条材料检查声明覆盖哪些材料位，取自任务绑定的已确认配置，
        # 不由系统补默认范围。实传情况与实际比较放在下面，与标准分开表达。
        scope_names = "、".join(material_names.get(key, key) for key in rule["materials"])
        criterion = str(rule.get("requirement") or "").strip()
        db.add(
            RuleResult(
                audit_task_id=task.id,
                node_execution_id=execution.id,
                rule_key=rule["rule_key"],
                rule_name=rule["name"],
                status=status,
                message=message,
                result_json={
                    "applicability": _applicability_json(state),
                    "missing_material_keys": sorted(missing),
                    "missing_material_names": [material_names.get(key, key) for key in sorted(missing)],
                    "standard": {
                        "operator_label": OPERATOR_LABELS.get(rule.get("operator"), rule.get("operator")),
                        # 检查口径原文来自配置列；没有填写时如实标缺失，不代填默认条件。
                        "requirement": criterion or None,
                        # 这条检查声明的材料范围就是它的判定标准：这些材料位齐了才算通过。
                        "comparison_label": f"所声明的材料位是否都已上传：{scope_names}" if scope_names else None,
                        "check_level": rule.get("check_level"),
                        "clear": bool(scope_names),
                        **({} if scope_names else {"missing": "该检查没有声明适用材料，判定标准缺少业务依据"}),
                    },
                    # 实际比较：声明范围与实传情况逐项对照，读得出缺的是哪一份。
                    "comparison": {
                        "kind": "MATERIAL_EXISTS",
                        "left_label": "本次已上传的材料位",
                        "left_value": uploaded_names or "无",
                        "right_label": "本条检查声明的材料位",
                        "right_value": scope_names or "未声明",
                        "expression": (
                            f"已上传 {len(uploaded_scoped)} 项，声明 {len(scoped)} 项"
                            + (f"，缺少：{missing_names}" if missing else "，无缺少")
                        ),
                        "mode_label": "所声明的材料位均已上传",
                    },
                },
            )
        )
        counts[status] = counts.get(status, 0) + 1
    return {
        "uploaded": len(available),
        "required_missing": len(required_missing),
        "missing_materials": required_missing,
        "blocked": bool(required_missing),
        "material_rule_count": len(rules),
        "material_rule_status_counts": counts,
        "skipped": not rules and not required_missing,
        "skip_reason": "本模板未配置材料检查规则" if not rules and not required_missing else None,
    }

def _task_field_facts(
    db: Session,
    task: AuditTask,
    field_key: str,
    material_key: str,
) -> list[TaskFieldValue]:
    """同一 (任务, 字段, 材料位) 的全部事实版本，含历史上传版本，按版本号升序。

    current_fact_scope 唯一键不含 document_upload_id，所以“当前事实”的范围是整个
    (任务, 字段, 材料位)：清理当前位必须按这个范围做，否则旧上传版本还占着当前位时新事实写不进去。
    """
    return list(
        db.scalars(
            select(TaskFieldValue)
            .where(
                TaskFieldValue.audit_task_id == task.id,
                TaskFieldValue.scenario_version_id == task.scenario_version_id,
                TaskFieldValue.field_key == field_key,
                TaskFieldValue.material_key == material_key,
            )
            .order_by(TaskFieldValue.revision_no)
        ).all()
    )

def _next_field_revision(facts: list[TaskFieldValue], document_upload_id: int) -> int:
    """同一上传版本内的下一个版本号（唯一键含 document_upload_id + revision_no）。"""
    return max((item.revision_no for item in facts if item.document_upload_id == document_upload_id), default=0) + 1

def _write_human_field_fact(
    db: Session,
    task: AuditTask,
    *,
    field_key: str,
    material_key: str,
    document_upload_id: int,
    status: str,
    raw_value: str | None,
    normalized_value: Any | None,
) -> tuple[TaskFieldValue, int | None]:
    """人工裁决产生的字段事实新版本。

    旧当前事实先退出当前位（唯一键 (任务, current_fact_scope) 要求同一时刻只有一个当前事实，
    且该范围不含上传版本），新行不继承旧引文、不带抽取执行批次；旧行与旧复核、旧证据保持可追溯。
    返回 (新事实, 上一版版本号)。
    """
    facts = _task_field_facts(db, task, field_key, material_key)
    current = next((item for item in facts if item.is_current), None)
    previous_revision_no = current.revision_no if current is not None else None
    if facts:
        db.execute(
            update(TaskFieldValue)
            .where(TaskFieldValue.id.in_([item.id for item in facts]))
            .values(is_current=False)
        )
        db.flush()
    fact = TaskFieldValue(
        audit_task_id=task.id,
        scenario_version_id=task.scenario_version_id,
        field_key=field_key,
        material_key=material_key,
        document_upload_id=document_upload_id,
        revision_no=_next_field_revision(facts, document_upload_id),
        is_current=True,
        status=status,
        raw_value=raw_value,
        normalized_value_json=normalized_value,
        evidence_id=None,
        extraction_execution_id=None,
        origin="HUMAN_CORRECTION",
    )
    db.add(fact)
    db.flush()
    return fact, previous_revision_no

def _extraction_artifacts(
    artifacts: list[StoredArtifact],
    fields: list[dict[str, Any]],
    document: TaskDocument,
) -> list[StoredArtifact]:
    """本次抽取可用的产物。

    声明了「产物来源=印章识别」的字段只能取自印章产物：本材料位没有 READY 的印章
    产物时明确报“缺输入”，**不退回**正文产物抽取，也不把这些字段静默跳过——
    跳过会让它们留在“未抽取”状态，下游主体核对拿不到待核对主体。
    """
    seal_fields = [item["field_key"] for item in fields if declared_artifact_source(item) is not None]
    if seal_fields and not any(item.artifact_type == SEAL_ARTIFACT_SOURCE for item in artifacts):
        raise BusinessError(
            "SEAL_ARTIFACT_MISSING",
            f"材料“{document.material_name}”的字段"
            f"（{'、'.join(sorted(seal_fields))}）声明了产物来源=印章识别，"
            "但当前上传文件没有可用的印章识别产物，字段抽取缺少输入来源，本次未执行",
            details={"material": document.material_name, "material_key": document.material_key},
        )
    return artifacts

def _field_extraction(db: Session, task: AuditTask, manifest: dict[str, Any], execution: NodeExecution) -> dict[str, Any]:
    material_defs = {item["material_key"]: item for item in manifest["materials"]}
    selected_fields = task.task_schema_json["fields"]
    by_material: dict[str, list[dict[str, Any]]] = {}
    for field in selected_fields:
        by_material.setdefault(field["material_key"], []).append(field)
    extracted = 0
    for document, upload in _current_uploads(db, task):
        fields = by_material.get(document.material_key, [])
        if not fields:
            continue
        if not upload:
            continue
        artifacts = db.scalars(
            select(StoredArtifact).where(
                StoredArtifact.document_upload_id == upload.id,
                StoredArtifact.upload_sha256 == upload.sha256,
                StoredArtifact.status == "READY",
            )
        ).all()
        if not artifacts:
            raise BusinessError("DOCUMENT_ARTIFACT_MISSING", "当前上传文件没有可用处理产物", details={"material": document.material_name})
        artifacts = _extraction_artifacts(list(artifacts), fields, document)
        response = extract_document_fields(
            material=material_defs[document.material_key],
            fields=fields,
            artifacts=[{"type": item.artifact_type, "content": _artifact_payload(item).content} for item in artifacts],
        )
        artifact_by_type = {item.artifact_type: item for item in artifacts}
        field_definitions = {item["field_key"]: item for item in fields}
        for result in response.fields:
            field = field_definitions[result.field_key]
            status, raw_value, normalized_value, status_reason = _extraction_fact_value(field, result)
            locator_status: str | None = None
            locator_note: str | None = None
            page_no = result.evidence.page_no if result.evidence else None
            bbox = result.evidence.bbox if result.evidence else None
            if result.evidence is not None:
                # 定位核对与内容依据分开：页码/坐标对不上只影响高亮，不影响内容；
                # 只有引文在该产物里确实找不到原文依据时，PRESENT 才降级为证据不足。
                locator_status, locator_note, page_no, bbox, quote_check = _verify_evidence_location(
                    artifact_by_type[result.evidence.artifact_type], result.evidence
                )
                if status == "PRESENT" and quote_check == "NOT_FOUND":
                    status = "EVIDENCE_UNCLEAR"
                    status_reason = "抽取引用的原文在该处理产物中找不到，字段内容缺少原文依据"
            previous_facts = _task_field_facts(db, task, result.field_key, document.material_key)
            if previous_facts:
                # 旧事实上的待复核项依据的是旧事实，一律失效；已裁决的复核保持指向旧事实。
                # 当前位按 (任务, 字段, 材料位) 清理：旧上传版本的当前事实也必须让位，否则唯一键冲突。
                db.execute(
                    update(ManualReview)
                    .where(
                        ManualReview.field_value_id.in_([item.id for item in previous_facts]),
                        ManualReview.status == "PENDING",
                    )
                    .values(status="INVALIDATED")
                )
                db.execute(
                    update(TaskFieldValue)
                    .where(TaskFieldValue.id.in_([item.id for item in previous_facts]))
                    .values(is_current=False)
                )
                db.flush()
            # 每次抽取新增一个事实版本，绝不原地改写已有行的原文、规范值或状态。
            fact = TaskFieldValue(
                audit_task_id=task.id,
                scenario_version_id=task.scenario_version_id,
                field_key=result.field_key,
                material_key=document.material_key,
                document_upload_id=upload.id,
                revision_no=_next_field_revision(previous_facts, upload.id),
                is_current=True,
                status=status,
                raw_value=raw_value,
                normalized_value_json=normalized_value,
                origin="DOCUMENT_EXTRACTION",
                extraction_execution_id=execution.id,
            )
            db.add(fact)
            db.flush()
            if result.evidence is not None:
                evidence = Evidence(
                    evidence_no=public_no("EVD"),
                    audit_task_id=task.id,
                    document_upload_id=upload.id,
                    field_value_id=fact.id,
                    page_no=page_no,
                    quote_text=result.evidence.quote_text,
                    bbox_json=bbox,
                    artifact_id=artifact_by_type[result.evidence.artifact_type].id,
                    locator_status=locator_status or "UNVERIFIED",
                    locator_note=locator_note,
                )
                db.add(evidence)
                db.flush()
                fact.evidence_id = evidence.id
            if status in {"EVIDENCE_UNCLEAR", "NORMALIZATION_FAILED"}:
                db.add(
                    ManualReview(
                        review_no=public_no("REV"),
                        audit_task_id=task.id,
                        field_value_id=fact.id,
                        review_type="FIELD_CONFIRMATION",
                        status="PENDING",
                        system_judgment_json={
                            "status": status,
                            "message": status_reason,
                            "raw_value": raw_value,
                            "normalized_value": normalized_value,
                            "field_unit": field.get("unit"),
                            "field_currency": field.get("currency"),
                            "evidence": _evidence_snapshot(db, fact.evidence_id),
                        },
                        business_note=f"字段“{field['name']}”需要人工确认",
                        proposed_value=raw_value,
                    )
                )
            extracted += 1
    rule_summary = _save_extraction_rule_results(db, task, manifest, execution)
    return {
        "field_results": extracted,
        **rule_summary,
        "skipped": rule_summary["extraction_rule_count"] == 0,
        "skip_reason": "本模板未配置字段抽取规则" if rule_summary["extraction_rule_count"] == 0 else None,
    }

def _extraction_result_payload(
    rule: dict[str, Any],
    field_definitions: dict[str, dict[str, Any]],
    material_names: dict[str, str],
    facts: list[TaskFieldValue | None],
) -> dict[str, Any]:
    """一条字段抽取规则的结果内容：标准 + 本次实际抽到的取值。

    首次抽取与人工改值后的重算共用这里，两条路径写出同一份结构。
    重算时若只写状态和字段状态列表，页面就会在改值之后丢失「审核标准」与
    「抽取结果」——那正是工作人员核对「改完之后系统拿什么在比」的依据。
    """
    extracted = [
        {
            "field_key": field_key,
            "material_key": field_definitions[field_key]["material_key"],
            "field_name": field_definitions[field_key]["name"],
            "material_name": material_names.get(field_definitions[field_key]["material_key"], ""),
            "value_type": field_definitions[field_key].get("value_type"),
            "unit": field_definitions[field_key].get("unit"),
            "status": fact.status if fact else "NOT_EXECUTED",
            "raw_value": fact.raw_value if fact else None,
            "normalized_value": fact.normalized_value_json if fact else None,
            "has_evidence": bool(fact and fact.evidence_id),
        }
        for field_key, fact in zip(rule["outputs"], facts)
    ]
    instruction = str((rule.get("parameters") or {}).get("instruction") or "").strip()
    requirement = instruction or str(rule.get("requirement") or "").strip()
    return {
        "field_statuses": [item["status"] for item in extracted],
        # 审核标准 = 字段的抽取要求原文（业务在字段配置里填写的事实）。
        # 抽取规则的「要求」是提取什么信息，不是合格阈值，因此不设合格条件。
        "standard": {
            "operator_label": OPERATOR_LABELS.get(rule.get("operator"), rule.get("operator")),
            "requirement": requirement or None,
            "comparison_label": "按抽取要求从材料中提取该字段的取值",
            "check_level": rule.get("check_level"),
            "clear": bool(requirement),
            **({} if requirement else {"missing": "该字段没有填写抽取要求，抽取缺少业务依据"}),
        },
        # 抽取结果：本次实际从材料中取到的原文与标准化值。
        # 这是「抽取了什么」，与证据记录里保存的「材料原文」是两件事。
        "extracted_values": extracted,
    }

def _save_extraction_rule_results(
    db: Session,
    task: AuditTask,
    manifest: dict[str, Any],
    execution: NodeExecution,
) -> dict[str, Any]:
    enabled_keys = set(task.task_schema_json["enabled_rule_keys"])
    field_definitions = _task_field_definitions(task)
    material_names = _material_names(manifest)
    rules = [
        rule
        for rule in manifest["rules"]
        if rule["node"] == "FIELD_EXTRACTION" and rule["operator"] == "LLM_EXTRACT" and rule["rule_key"] in enabled_keys
    ]
    counts: dict[str, int] = {}
    for rule in rules:
        facts = []
        for field_key in rule["outputs"]:
            field = field_definitions.get(field_key)
            if not field:
                raise BusinessError("TASK_SCHEMA_FIELD_MISSING", "字段抽取规则引用的字段不在任务清单中", status_code=500)
            facts.append(
                db.scalar(
                    select(TaskFieldValue)
                    .join(DocumentUpload, DocumentUpload.id == TaskFieldValue.document_upload_id)
                    .join(TaskDocument, TaskDocument.id == DocumentUpload.task_document_id)
                    .where(
                        TaskFieldValue.audit_task_id == task.id,
                        TaskFieldValue.scenario_version_id == task.scenario_version_id,
                        # 只认当前事实版本；历史版本不参与抽取规则判定。
                        TaskFieldValue.is_current.is_(True),
                        TaskFieldValue.field_key == field_key,
                        TaskFieldValue.material_key == field["material_key"],
                        TaskDocument.audit_task_id == task.id,
                        TaskDocument.material_key == field["material_key"],
                        TaskDocument.current_upload_id == TaskFieldValue.document_upload_id,
                    )
                )
            )
        statuses = [fact.status if fact else "NOT_EXECUTED" for fact in facts]
        if all(status in {"PRESENT", "REVIEWED"} for status in statuses):
            result = {"status": "PASSED", "message": "字段抽取完成"}
        elif any(status in {"EXTRACTION_FAILED", "NOT_EXECUTED"} for status in statuses):
            result = {"status": "NOT_EXECUTED", "message": "字段抽取失败或未执行"}
        else:
            result = {"status": "EVIDENCE_INSUFFICIENT", "message": "字段抽取未形成充分证据"}
        db.add(
            RuleResult(
                audit_task_id=task.id,
                node_execution_id=execution.id,
                rule_key=rule["rule_key"],
                rule_name=rule["name"],
                status=result["status"],
                message=result["message"],
                result_json=_extraction_result_payload(rule, field_definitions, material_names, facts),
            )
        )
        counts[result["status"]] = counts.get(result["status"], 0) + 1
    return {"extraction_rule_count": len(rules), "extraction_rule_status_counts": counts}

def _field_inputs(db: Session, task: AuditTask, rule: dict[str, Any]) -> list[dict[str, Any]]:
    """按任务固定模板版本 + 规则明确 inputs + 当前上传版本的当前字段事实取数。

    只按 material_key / field_key 精确匹配；不按文件名、中文字段名相似度或全局最新记录选输入。
    同时按算子契约提供 value_type / unit / material_state：材料被否定或尚未形成处理产物时，
    规则必须由算子按证据不足处理，本服务不做字符串相等比较，也不替算子下结论。
    """
    field_definitions = _task_field_definitions(task)
    material_states = _material_states(db, task)
    inputs: list[dict[str, Any]] = []
    for item in rule.get("inputs", []):
        field = field_definitions.get(item["field_key"], {})
        fact = db.scalar(
            select(TaskFieldValue)
            .join(DocumentUpload, DocumentUpload.id == TaskFieldValue.document_upload_id)
            .join(TaskDocument, TaskDocument.id == DocumentUpload.task_document_id)
            .where(
                TaskFieldValue.audit_task_id == task.id,
                TaskFieldValue.scenario_version_id == task.scenario_version_id,
                TaskFieldValue.field_key == item["field_key"],
                TaskFieldValue.material_key == item["material_key"],
                # 只取当前事实版本：历史版本即使还挂在旧复核上也不作为有效输入。
                TaskFieldValue.is_current.is_(True),
                DocumentUpload.audit_task_id == task.id,
                DocumentUpload.is_current.is_(True),
                TaskDocument.material_key == item["material_key"],
                TaskDocument.audit_task_id == task.id,
                TaskDocument.current_upload_id == TaskFieldValue.document_upload_id,
            )
            .order_by(TaskFieldValue.id.desc())
        )
        common = {
            "value_type": field.get("value_type"),
            "unit": field.get("unit"),
            "currency": field.get("currency"),
            "material_state": material_states.get(item["material_key"]),
        }
        if not fact:
            inputs.append(
                {
                    **item,
                    **common,
                    "status": "NOT_EXECUTED",
                    "raw_value": None,
                    "normalized_value": None,
                    "evidence_id": None,
                }
            )
        else:
            inputs.append(
                {
                    **item,
                    **common,
                    "status": fact.status,
                    "raw_value": fact.raw_value,
                    "normalized_value": fact.normalized_value_json,
                    "evidence_id": fact.evidence_id,
                }
            )
    return inputs

def _input_summary(
    inputs: list[dict[str, Any]],
    field_definitions: dict[str, dict[str, Any]],
    material_names: dict[str, str],
) -> list[dict[str, Any]]:
    """规则实际使用的输入事实，供页面与报告说明“规则输入了什么”。

    原文值与标准化值分开保存：原文是事实本身（合同里真实写着的那几个字），
    标准化值是为比较确定性算出来的（例如 2024年10月11日 → 2024-10-11）。
    只给其中一个都会让工作人员无法对照：只给标准化值看不到原文，
    只给原文看不出系统拿什么参与比较。
    """
    summary: list[dict[str, Any]] = []
    for item in inputs:
        field = field_definitions.get(item["field_key"], {})
        summary.append(
            {
                "field_key": item["field_key"],
                "material_key": item["material_key"],
                "field_name": field.get("name", item["field_key"]),
                "material_name": material_names.get(item["material_key"], item["material_key"]),
                "value_type": field.get("value_type"),
                "unit": field.get("unit"),
                "currency": field.get("currency"),
                "status": item.get("status"),
                "raw_value": item.get("raw_value"),
                "normalized_value": item.get("normalized_value"),
                # 兼容既有读取方（报告输入摘要、风险材料归属）：value 取参与比较的那个值。
                "value": item.get("normalized_value") if item.get("normalized_value") is not None else item.get("raw_value"),
                "has_evidence": bool(item.get("evidence_id")),
            }
        )
    return summary

def _input_trace(result_json: dict[str, Any] | None, labels: dict[str, str]) -> str | None:
    """一条规则「使用的内容」。

    取值来源按规则类型取：比较类规则用参与比较的 `inputs`；抽取规则没有参与比较的输入，
    它使用的内容就是本次抽到的 `extracted_values`。两者都没有时才返回 None，
    由调用方照实说明「未记录取值内容」，而不是留一行空白让人以为规则没用到任何东西。
    """
    if not isinstance(result_json, dict):
        return None
    inputs = result_json.get("inputs")
    if not isinstance(inputs, list) or not inputs:
        inputs = result_json.get("extracted_values")
    if not isinstance(inputs, list) or not inputs:
        return None
    parts: list[str] = []
    for item in inputs:
        if not isinstance(item, dict):
            continue
        label = f"{item.get('material_name') or '未知材料'}·{item.get('field_name') or item.get('field_key') or '未知字段'}"
        status = labels.get(str(item.get("status")), str(item.get("status")))
        raw = item.get("raw_value")
        normalized = item.get("normalized_value")
        if normalized is None:
            normalized = item.get("value")
        if raw not in (None, "") and normalized not in (None, "") and str(raw) != str(normalized):
            # 原文与标准化值不同时两个都要给：只给一个都无法核对系统到底拿什么去比。
            shown = f"{raw}（标准化 {normalized}）"
        elif raw not in (None, ""):
            shown = str(raw)
        elif normalized not in (None, ""):
            shown = str(normalized)
        else:
            shown = ""
        # 材料级输入（文件质量检查、材料检查）没有字段取值，展示该份材料的判定。
        if not shown and item.get("kind") == "MATERIAL":
            filename = item.get("filename")
            shown = f"{filename}（{item.get('message') or '已按材料判定'}）" if filename else str(item.get("message") or "")
        parts.append(f"{label}={shown}（{status}）" if shown else f"{label}（{status}）")
    return "、".join(parts) if parts else None

def _comparison_view(
    comparison: Any,
    inputs: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """把算子给出的对比过程补齐成可直接展示的事实。

    算子只按输入下标引用左右两侧（它不认识字段中文名），这里按**同一次执行**的输入摘要
    补上业务名：字段中文名 + 来源材料中文名 + 抽取到的原文。页面与报告据此显示
    「A 材料的合同金额（原文 ¥221252131.69元）= B 材料的结算金额」，
    不必自己去猜下标对应哪个字段，也不会把两个字段的张冠李戴写进结论。
    """
    if not isinstance(comparison, dict):
        return None
    payload = dict(comparison)
    for key in ("left_index", "right_index"):
        label_key = key.replace("_index", "_label")
        index = payload.get(key)
        if not isinstance(index, int) or not (-len(inputs) <= index < len(inputs)):
            payload[label_key] = None
            continue
        item = inputs[index]
        field = item.get("field_name") or item.get("field_key") or "未知字段"
        material = item.get("material_name") or item.get("material_key") or "未知材料"
        payload[label_key] = f"{material}·{field}"
        payload[key.replace("_index", "_raw_value")] = item.get("raw_value")
    addends = payload.get("addends")
    if isinstance(addends, list) and len(inputs) > 1:
        # 加数逐一对应输入顺序（左侧待核对总额 + 加数…），同样补上业务名与原文。
        payload["addend_labels"] = [
            (f"{item.get('material_name') or item.get('material_key')}·"
             f"{item.get('field_name') or item.get('field_key')}")
            for item in inputs[1:]
        ]
    return payload

def _rule_standard_view(rule: dict[str, Any], parameters: dict[str, Any]) -> dict[str, Any]:
    """这条规则的审核标准，取自已配置、已确认的业务配置本身。

    标准只能来自配置：审核要求原文、检查方式、比较方向与允许差值都是业务填写的事实；
    标准缺失时如实给出缺的是什么，绝不由系统或大模型补一个默认条件。
    """
    requirement = str(rule.get("requirement") or "").strip()
    standard: dict[str, Any] = {
        "operator_label": OPERATOR_LABELS.get(rule["operator"], rule["operator"]),
        "requirement": requirement or None,
        "check_level": rule.get("check_level"),
        "risk_level": (rule.get("parameters") or {}).get("risk_level"),
        "comparison_mode": parameters.get("operator") if rule["operator"] in {
            "DATE_COMPARE",
            "DECIMAL_COMPARE",
            "DECIMAL_SUM_COMPARE",
        } else None,
        "comparison_label": comparison_label(rule["operator"], parameters.get("operator")),
        "threshold": parameters.get("threshold"),
        "threshold_unit": parameters.get("threshold_unit"),
    }
    configured = bool(requirement) or rule["operator"] in {
        "DECIMAL_SUM_COMPARE",
        "FIELD_NOT_EMPTY",
        "CLAUSE_ABSENT",
    }
    standard["clear"] = configured
    if not configured:
        # 标准不明确时指出具体缺什么，不代填默认条件。
        standard["missing"] = "该规则没有填写审核要求原文，判断标准缺少业务依据"
    return standard

def _refresh_extraction_rule_results(db: Session, task: AuditTask, manifest: dict[str, Any]) -> list[str]:
    """按当前字段事实重算字段抽取规则结果，返回被重算的规则键。"""
    field_definitions = _task_field_definitions(task)
    facts = {
        (item.field_key, item.material_key): item
        for item in db.scalars(
            select(TaskFieldValue)
            .join(DocumentUpload, DocumentUpload.id == TaskFieldValue.document_upload_id)
            .join(TaskDocument, TaskDocument.id == DocumentUpload.task_document_id)
            .where(
                TaskFieldValue.audit_task_id == task.id,
                TaskFieldValue.scenario_version_id == task.scenario_version_id,
                TaskFieldValue.is_current.is_(True),
                TaskDocument.audit_task_id == task.id,
                TaskDocument.material_key == TaskFieldValue.material_key,
                TaskDocument.current_upload_id == TaskFieldValue.document_upload_id,
            )
        ).all()
    }
    enabled_keys = set(task.task_schema_json["enabled_rule_keys"])
    recomputed: list[str] = []
    for rule in manifest["rules"]:
        if rule["node"] != "FIELD_EXTRACTION" or rule["operator"] != "LLM_EXTRACT" or rule["rule_key"] not in enabled_keys:
            continue
        statuses: list[str] = []
        rule_facts: list[TaskFieldValue | None] = []
        for field_key in rule["outputs"]:
            field = field_definitions.get(field_key)
            if not field:
                raise BusinessError("TASK_SCHEMA_FIELD_MISSING", "字段抽取规则引用的字段不在任务清单中", status_code=500)
            fact = facts.get((field_key, field["material_key"]))
            rule_facts.append(fact)
            statuses.append(fact.status if fact else "NOT_EXECUTED")
        if all(status in {"PRESENT", "REVIEWED"} for status in statuses):
            result = {"status": "PASSED", "message": "字段抽取完成"}
        elif any(status in {"EXTRACTION_FAILED", "NOT_EXECUTED"} for status in statuses):
            result = {"status": "NOT_EXECUTED", "message": "字段抽取失败或未执行"}
        else:
            result = {"status": "EVIDENCE_INSUFFICIENT", "message": "字段抽取未形成充分证据"}
        record = db.scalar(
            select(RuleResult)
            .join(NodeExecution, NodeExecution.id == RuleResult.node_execution_id)
            .where(
                RuleResult.audit_task_id == task.id,
                RuleResult.rule_key == rule["rule_key"],
                NodeExecution.node_code == "FIELD_EXTRACTION",
                NodeExecution.status != "INVALIDATED",
            )
            .order_by(RuleResult.id.desc())
        )
        if not record:
            raise BusinessError("EXTRACTION_RESULT_MISSING", "字段复核缺少对应的抽取规则结果", status_code=500)
        record.status = result["status"]
        record.message = result["message"]
        # 重算同样写出标准与抽取结果，不退回只写状态列表：
        # 人工改值之后，页面仍要能说明「按什么要求在抽、这次抽到了什么」。
        record.result_json = _extraction_result_payload(
            rule, field_definitions, _material_names(manifest), rule_facts
        )
        recomputed.append(rule["rule_key"])
    return recomputed
