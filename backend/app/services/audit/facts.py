"""字段事实与字段抽取。

事实不可变：每次抽取或人工裁决都**新增一个版本**，旧行退出当前位但保留可追溯。
唯一键 (任务, 字段, 材料位) 上只有一条 `is_current`，且该范围**不含上传版本**——
旧上传版本的当前事实也必须让位，否则新事实写不进去。

原文是事实。文本类字段的标准化值是服务端清理后的原文；数值、日期、布尔字段
由服务端按字段声明的类型/单位/币种对原文重新计算，模型给出的
`normalized_value` 不反向改变服务端结果。结构化字段由后端按字段 Schema 校验
业务 JSON，并为每个顶层属性分别核验证据。"""

import json
from ...domain.audit_vocab import SEAL_ARTIFACT_SOURCE, declared_artifact_source
from ...domain import fact_snapshot, field_strategy, quality_judgement
from ...domain.identity import public_no
from ...domain.manifest import OPERATOR_LABELS, comparison_label, required_field_scope, rule_node_type_of
from ...errors import BusinessError
from ...models import AuditTask, DocumentUpload, Evidence, ManualReview, NodeExecution, RuleResult, StoredArtifact, TaskDocument, TaskFact, TaskFieldValue
from ..llm_extraction import extract_document_fields
from .artifacts import _artifact_payload
from .evidence import _evidence_snapshot, _evidence_snapshots, _verify_evidence_location
from .shared import _applicability_json, _applicability_state, _current_uploads, _extraction_fact_value, _material_names, _not_applicable_message, _task_field_definitions
from .state import _material_states
from datetime import date
from decimal import Decimal, InvalidOperation
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
        if rule_node_type_of(rule) == "MATERIAL_CHECK" and rule["operator"] == "MATERIAL_EXISTS" and rule["rule_key"] in enabled_keys
    ]
    counts: dict[str, int] = {}
    for rule in rules:
        scoped = set(rule["materials"])
        state = _applicability_state(rule, available)
        if not state["applicable"]:
            # 已配置规则但适用条件明确不成立：记录不适用并保留原因，绝不写成通过。
            status = "NOT_APPLICABLE"
            message = _not_applicable_message(state, material_names)
        else:
            status = None
            message = None
        # 判定与结果结构走共享领域服务 `material_exists_result`：
        # Agent 工具调的是同一个函数，节点与工具不可能得出不同结论，
        # 也不会出现「目录声明的输出键在某一侧永远为空」。
        payload = quality_judgement.material_exists_result(
            required_keys=sorted(scoped),
            available_keys=available,
            required_by_key={key: bool(document.required) for key, document in documents.items()},
            material_names=material_names,
            applicability=_applicability_json(state),
            requirement=str(rule.get("requirement") or "").strip(),
            operator_label=OPERATOR_LABELS.get(rule.get("operator"), rule.get("operator")),
            check_level=rule.get("check_level"),
            status=status,
            message=message,
        )
        db.add(
            RuleResult(
                audit_task_id=task.id,
                node_execution_id=execution.id,
                rule_key=rule["rule_key"],
                rule_name=rule["name"],
                status=payload["status"],
                message=payload["message"],
                result_json=payload,
            )
        )
        counts[payload["status"]] = counts.get(payload["status"], 0) + 1
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


def _current_field_fact_pairs(
    db: Session,
    task: AuditTask,
) -> dict[tuple[str, str], tuple[TaskFieldValue, TaskFact]]:
    """读取当前字段候选与统一事实，并核对两者是否仍表示同一份业务事实。

    `TaskFieldValue` 只负责原文、候选值和证据；`TaskFact` 决定有效状态与可执行值。
    页面和报告需要同时展示两类信息，因此必须在这里按任务冻结字段定义配对，不能让
    调用方单独读取候选表后把它的状态或规范值当成正式事实。
    """
    definitions = _task_field_definitions(task)
    candidate_rows = list(
        db.scalars(
            select(TaskFieldValue)
            .join(DocumentUpload, DocumentUpload.id == TaskFieldValue.document_upload_id)
            .join(TaskDocument, TaskDocument.id == DocumentUpload.task_document_id)
            .where(
                TaskFieldValue.audit_task_id == task.id,
                TaskFieldValue.scenario_version_id == task.scenario_version_id,
                TaskFieldValue.is_current.is_(True),
                DocumentUpload.audit_task_id == task.id,
                DocumentUpload.is_current.is_(True),
                TaskDocument.audit_task_id == task.id,
                TaskDocument.material_key == TaskFieldValue.material_key,
                TaskDocument.current_upload_id == TaskFieldValue.document_upload_id,
            )
            .order_by(TaskFieldValue.id)
        ).all()
    )
    candidates: dict[tuple[str, str], TaskFieldValue] = {}
    for row in candidate_rows:
        key = (row.field_key, row.material_key)
        if key in candidates:
            raise BusinessError(
                "TASK_FIELD_CURRENT_DUPLICATE",
                "同一字段与材料位存在多条当前字段版本，无法确定展示或执行依据",
                status_code=500,
                details={"field_key": row.field_key, "material_key": row.material_key},
            )
        candidates[key] = row

    task_fact_rows = list(
        db.scalars(
            select(TaskFact).where(
                TaskFact.audit_task_id == task.id,
                TaskFact.scenario_version_id == task.scenario_version_id,
                TaskFact.fact_kind == "EXTRACTED",
                TaskFact.is_current.is_(True),
            )
        ).all()
    )
    task_facts: dict[tuple[str, str, str, str, str], TaskFact] = {}
    for row in task_fact_rows:
        identity = (
            row.fact_code,
            row.material_key or "",
            row.period_scope,
            row.statement_scope,
            row.entity_scope,
        )
        if identity in task_facts:
            raise BusinessError(
                "TASK_FACT_IDENTITY_DUPLICATE",
                "同一字段业务身份存在多条当前统一事实，无法确定有效值",
                status_code=500,
                details={
                    "fact_code": row.fact_code,
                    "material_key": row.material_key,
                    "period_scope": row.period_scope,
                    "statement_scope": row.statement_scope,
                    "entity_scope": row.entity_scope,
                },
            )
        task_facts[identity] = row

    expected_identities: set[tuple[str, str, str, str, str]] = set()
    pairs: dict[tuple[str, str], tuple[TaskFieldValue, TaskFact]] = {}
    for field_key, field in definitions.items():
        material_key = str(field["material_key"])
        key = (field_key, material_key)
        identity = (
            str(field.get("source_ref") or field_key),
            material_key,
            required_field_scope(
                field, "period_scope", code="TASK_SCHEMA_FIELD_SCOPE_MISSING", status_code=500
            ),
            required_field_scope(
                field, "statement_scope", code="TASK_SCHEMA_FIELD_SCOPE_MISSING", status_code=500
            ),
            str(field.get("entity_scope") or "SELF"),
        )
        expected_identities.add(identity)
        candidate = candidates.get(key)
        canonical = task_facts.get(identity)
        if candidate is None and canonical is None:
            continue
        if candidate is None or canonical is None:
            raise BusinessError(
                "TASK_FIELD_FACT_INCOMPLETE",
                "当前字段版本与统一事实没有成对生成，不能展示或生成报告",
                status_code=500,
                details={
                    "field_key": field_key,
                    "material_key": material_key,
                    "field_value_exists": candidate is not None,
                    "task_fact_exists": canonical is not None,
                },
            )
        if candidate.status != canonical.status:
            raise BusinessError(
                "TASK_FACT_FIELD_STATUS_MISMATCH",
                "当前字段版本与统一事实的状态不一致，不能展示或生成报告",
                status_code=500,
                details={
                    "field_key": field_key,
                    "material_key": material_key,
                    "field_status": candidate.status,
                    "fact_status": canonical.status,
                },
            )
        expected_contract = {
            "value_type": field.get("value_type"),
            "unit": field.get("unit"),
            "currency": field.get("currency"),
        }
        actual_contract = {
            "value_type": canonical.value_type,
            "unit": canonical.unit,
            "currency": canonical.currency,
        }
        if actual_contract != expected_contract:
            raise BusinessError(
                "TASK_FACT_FIELD_CONTRACT_MISMATCH",
                "统一事实的类型、单位或币种与任务字段契约不一致",
                status_code=500,
                details={
                    "field_key": field_key,
                    "material_key": material_key,
                    "expected": expected_contract,
                    "actual": actual_contract,
                },
            )
        if canonical.status in {"PRESENT", "REVIEWED"}:
            canonical_value = fact_snapshot.task_fact_value(canonical, field_definition=field)
            if canonical_value is None:
                raise BusinessError(
                    "TASK_FACT_VALUE_MISSING",
                    "统一事实已标记为有效，但没有可展示或执行的值",
                    status_code=500,
                    details={"field_key": field_key, "material_key": material_key},
                )
            candidate_value = fact_snapshot.canonical_field_value(candidate.normalized_value_json, field)
            if candidate_value != fact_snapshot.canonical_field_value(canonical_value, field):
                raise BusinessError(
                    "TASK_FACT_FIELD_VALUE_MISMATCH",
                    "当前字段候选值与统一事实的有效值不一致，不能继续展示或生成报告",
                    status_code=500,
                    details={"field_key": field_key, "material_key": material_key},
                )
        pairs[key] = (candidate, canonical)

    unexpected_candidates = sorted(set(candidates) - set((key, str(item["material_key"])) for key, item in definitions.items()))
    if unexpected_candidates:
        raise BusinessError(
            "TASK_FIELD_OUT_OF_SCOPE",
            "当前字段版本不在任务冻结的字段范围内",
            status_code=500,
            details={"field_keys": [key for key, _ in unexpected_candidates]},
        )
    unexpected_facts = sorted(set(task_facts) - expected_identities)
    if unexpected_facts:
        raise BusinessError(
            "TASK_FACT_OUT_OF_SCOPE",
            "当前统一事实不在任务冻结的字段范围内",
            status_code=500,
            details={"fact_codes": [identity[0] for identity in unexpected_facts]},
        )
    return pairs


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
    preserved_review_ids: set[int] | None = None,
    inherit_evidence_from_id: int | None = None,
) -> tuple[TaskFieldValue, int | None, int]:
    """人工裁决产生的字段事实新版本。

    旧当前事实先退出当前位（唯一键 (任务, current_fact_scope) 要求同一时刻只有一个当前事实，
    且该范围不含上传版本），新行不带抽取执行批次。确认原值或判为证据不足时可复制
    原证据到新版本；人工改写取值时不自动沿用旧引文。旧行、旧复核与旧证据保持可追溯。
    返回 (新事实, 上一版版本号, 本次失效的旧待办数量)。正在本事务里裁决的待办
    通过 preserved_review_ids 排除；Session 关闭了 autoflush，不能依赖内存里已改成
    RESOLVED 的状态让 SQL 自动避开它。
    """
    facts = _task_field_facts(db, task, field_key, material_key)
    current = next((item for item in facts if item.is_current), None)
    previous_revision_no = current.revision_no if current is not None else None
    invalidated_reviews = 0
    if facts:
        fact_ids = [item.id for item in facts]
        # 新事实一旦占据当前位，依据旧版本创建的待办就不能继续提交。把失效与
        # 事实换版放在同一事务里，避免任务仍展示旧待办、提交时才发现它已过期。
        review_conditions = [
            ManualReview.field_value_id.in_(fact_ids),
            ManualReview.status == "PENDING",
        ]
        if preserved_review_ids:
            review_conditions.append(ManualReview.id.not_in(sorted(preserved_review_ids)))
        invalidated_reviews = db.execute(
            update(ManualReview)
            .where(*review_conditions)
            .values(status="INVALIDATED")
        ).rowcount or 0
        db.execute(
            update(TaskFieldValue)
            .where(TaskFieldValue.id.in_(fact_ids))
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
    if inherit_evidence_from_id is not None:
        inherited_evidence_id: int | None = None
        first_inherited_evidence_id: int | None = None
        source_primary_evidence_id = next(
            (item.evidence_id for item in facts if item.id == inherit_evidence_from_id),
            None,
        )
        source_evidences = db.scalars(
            select(Evidence)
            .where(
                Evidence.audit_task_id == task.id,
                Evidence.field_value_id == inherit_evidence_from_id,
                Evidence.rule_result_id.is_(None),
            )
            .order_by(Evidence.id)
        ).all()
        for source in source_evidences:
            inherited = Evidence(
                evidence_no=public_no("EVD"),
                audit_task_id=task.id,
                document_upload_id=source.document_upload_id,
                field_value_id=fact.id,
                field_path=source.field_path,
                page_no=source.page_no,
                quote_text=source.quote_text,
                bbox_json=source.bbox_json,
                artifact_id=source.artifact_id,
                locator_status=source.locator_status,
                locator_note=source.locator_note,
            )
            db.add(inherited)
            db.flush()
            if first_inherited_evidence_id is None:
                first_inherited_evidence_id = inherited.id
            if source.id == source_primary_evidence_id:
                inherited_evidence_id = inherited.id
        fact.evidence_id = inherited_evidence_id or first_inherited_evidence_id
    field = _task_field_definitions(task).get(field_key)
    if field is None or field["material_key"] != material_key:
        raise BusinessError(
            "TASK_FIELD_DEFINITION_MISSING",
            "人工纠错的字段不在任务冻结的字段定义中",
            status_code=500,
            details={"field_key": field_key, "material_key": material_key},
        )
    _write_field_task_fact(
        db,
        task,
        source_execution_id=None,
        field=field,
        material_key=material_key,
        status=status,
        normalized_value=normalized_value if status == "REVIEWED" else None,
    )
    return fact, previous_revision_no, int(invalidated_reviews)

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


def _extraction_scope(
    task: AuditTask,
    manifest: dict[str, Any],
    execution: NodeExecution,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Resolve this extraction instance's task fields and enabled rules."""
    selected_fields = task.task_schema_json["fields"]
    selected_field_keys = {field["field_key"] for field in selected_fields}
    enabled_rule_keys = set(task.task_schema_json["enabled_rule_keys"])
    extraction_rules = [
        rule for rule in manifest["rules"]
        if rule_node_type_of(rule) == "FIELD_EXTRACTION" and rule["operator"] == "LLM_EXTRACT"
    ]
    extraction_nodes = [
        node for node in manifest["nodes"]
        if (node.get("node_type") or node["node_code"]) == "FIELD_EXTRACTION"
    ]
    nodes_by_code = {node["node_code"]: node for node in extraction_nodes}
    if execution.node_code not in nodes_by_code:
        raise BusinessError(
            "EXTRACTION_NODE_MISSING", "当前字段抽取节点不在任务执行计划中", status_code=500,
            details={"node_code": execution.node_code},
        )

    has_contract = "execution_contract_version" in manifest or any(
        "execution_contract" in node for node in manifest["nodes"]
    )
    if has_contract:
        owners: dict[str, str] = {}
        for node_code, node in nodes_by_code.items():
            contract = node.get("execution_contract")
            if not isinstance(contract, dict) or not all(
                isinstance(contract.get(key), list)
                and all(isinstance(value, str) and value for value in contract[key])
                for key in ("field_keys", "rule_keys")
            ):
                raise BusinessError(
                    "EXTRACTION_CONTRACT_INVALID", "字段抽取节点缺少有效的字段和规则执行契约", status_code=500,
                    details={"node_code": node_code},
                )
            node_rules = [rule for rule in extraction_rules if rule["node"] == node_code]
            declared_rule_keys = set(contract["rule_keys"])
            actual_rule_keys = {rule["rule_key"] for rule in node_rules}
            if declared_rule_keys != actual_rule_keys:
                raise BusinessError(
                    "EXTRACTION_CONTRACT_RULE_MISMATCH", "字段抽取节点的规则契约与发布规则不一致", status_code=500,
                    details={
                        "node_code": node_code,
                        "missing_rule_keys": sorted(actual_rule_keys - declared_rule_keys),
                        "unexpected_rule_keys": sorted(declared_rule_keys - actual_rule_keys),
                    },
                )
            actual_output_fields = {
                field_key for rule in node_rules for field_key in rule["outputs"]
            }
            declared_output_fields = set(contract["field_keys"])
            if declared_output_fields != actual_output_fields:
                raise BusinessError(
                    "EXTRACTION_CONTRACT_FIELD_MISMATCH", "字段抽取节点的字段契约与发布规则输出不一致", status_code=500,
                    details={
                        "node_code": node_code,
                        "missing_field_keys": sorted(actual_output_fields - declared_output_fields),
                        "unexpected_field_keys": sorted(declared_output_fields - actual_output_fields),
                    },
                )
            for field_key in contract["field_keys"]:
                if field_key in selected_field_keys:
                    previous = owners.setdefault(field_key, node_code)
                    if previous != node_code:
                        raise BusinessError(
                            "EXTRACTION_FIELD_OWNER_DUPLICATE", "任务字段被多个字段抽取节点声明", status_code=500,
                            details={"field_key": field_key, "node_codes": sorted({previous, node_code})},
                        )
        unassigned = selected_field_keys - owners.keys()
        if unassigned:
            raise BusinessError(
                "EXTRACTION_FIELD_OWNER_MISSING", "任务字段没有对应的字段抽取节点", status_code=500,
                details={"field_keys": sorted(unassigned)},
            )
        contract = nodes_by_code[execution.node_code]["execution_contract"]
        field_keys = {key for key, owner in owners.items() if owner == execution.node_code}
        rule_keys = set(contract["rule_keys"])
        node_rules = [rule for rule in extraction_rules if rule["node"] == execution.node_code]
        undeclared_rules = {
            rule["rule_key"] for rule in node_rules if rule["rule_key"] in enabled_rule_keys
        } - rule_keys
        if undeclared_rules:
            raise BusinessError(
                "EXTRACTION_CONTRACT_RULE_MISSING", "字段抽取规则未列入节点执行契约", status_code=500,
                details={"node_code": execution.node_code, "rule_keys": sorted(undeclared_rules)},
            )
        rules = [
            rule for rule in node_rules
            if rule["rule_key"] in enabled_rule_keys and rule["rule_key"] in rule_keys
        ]
        out_of_scope = {
            key for rule in rules for key in rule["outputs"] if key not in field_keys
        }
        if out_of_scope:
            raise BusinessError(
                "EXTRACTION_CONTRACT_OUTPUT_MISMATCH", "字段抽取规则的输出不属于当前节点", status_code=500,
                details={"node_code": execution.node_code, "field_keys": sorted(out_of_scope)},
            )
    elif len(extraction_nodes) == 1:
        # Old manifests had one extraction instance and no per-node field contract.
        field_keys = selected_field_keys
        rules = [rule for rule in extraction_rules if rule["rule_key"] in enabled_rule_keys]
    else:
        owners = {}
        for rule in extraction_rules:
            for field_key in rule["outputs"]:
                if field_key not in selected_field_keys:
                    continue
                previous = owners.setdefault(field_key, rule["node"])
                if previous != rule["node"]:
                    raise BusinessError(
                        "EXTRACTION_FIELD_OWNER_DUPLICATE", "任务字段被多个字段抽取节点声明", status_code=500,
                        details={"field_key": field_key, "node_codes": sorted({previous, rule["node"]})},
                    )
        unassigned = selected_field_keys - owners.keys()
        if unassigned:
            raise BusinessError(
                "EXTRACTION_FIELD_OWNER_MISSING", "任务字段没有对应的字段抽取节点", status_code=500,
                details={"field_keys": sorted(unassigned)},
            )
        field_keys = {key for key, owner in owners.items() if owner == execution.node_code}
        rules = [
            rule for rule in extraction_rules
            if rule["node"] == execution.node_code and rule["rule_key"] in enabled_rule_keys
        ]
    return [field for field in selected_fields if field["field_key"] in field_keys], rules


def _field_extraction(db: Session, task: AuditTask, manifest: dict[str, Any], execution: NodeExecution) -> dict[str, Any]:
    material_defs = {item["material_key"]: item for item in manifest["materials"]}
    selected_fields, extraction_rules = _extraction_scope(task, manifest, execution)
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
        failed_results = [item for item in response.fields if item.status == "EXTRACTION_FAILED"]
        if failed_results:
            raise BusinessError(
                "FIELD_EXTRACTION_FAILED",
                "字段抽取服务未能完成本材料的字段抽取，请重试当前节点",
                status_code=502,
                details={
                    "material_key": document.material_key,
                    "material_name": document.material_name,
                    "field_keys": [item.field_key for item in failed_results],
                },
            )
        artifact_by_type = {item.artifact_type: item for item in artifacts}
        field_definitions = {item["field_key"]: item for item in fields}
        for result in response.fields:
            field = field_definitions[result.field_key]
            status, raw_value, normalized_value, status_reason = _extraction_fact_value(field, result)
            verified_evidence: list[tuple[str | None, Any, str, str | None, int | None, list[float] | None]] = []
            evidence_items: list[tuple[str | None, Any, Any, str | None]] = []
            if field_strategy.strategy_of(field) == field_strategy.STRUCTURED and isinstance(normalized_value, dict):
                property_schemas = ((field.get("structure_schema") or {}).get("properties") or {})
                evidence_items.extend(
                    (
                        path,
                        evidence,
                        normalized_value.get(path),
                        (
                            property_schemas[path].get("type")
                            if isinstance(property_schemas.get(path), dict)
                            else None
                        ),
                    )
                    for path, evidence in result.property_evidence.items()
                )
            elif result.evidence is not None:
                evidence_items.append((None, result.evidence, raw_value, field.get("value_type")))
            unsupported_paths: list[str] = []
            for field_path, evidence_item, evidence_value, evidence_value_type in evidence_items:
                # 定位核对与内容依据分开：页码/坐标对不上只影响高亮，不影响内容；
                # 只有引文在该产物里确实找不到原文依据时，PRESENT 才降级为证据不足。
                locator_status, locator_note, page_no, bbox, quote_check = _verify_evidence_location(
                    artifact_by_type[evidence_item.artifact_type],
                    evidence_item,
                    raw_value=evidence_value,
                    value_type=evidence_value_type,
                    unit=field.get("unit") if field_path is None else None,
                    currency=field.get("currency") if field_path is None else None,
                )
                if status == "PRESENT" and quote_check == "NOT_FOUND":
                    unsupported_paths.append(field_path or field["name"])
                verified_evidence.append(
                    (field_path, evidence_item, locator_status, locator_note, page_no, bbox)
                )
            if unsupported_paths:
                status = "EVIDENCE_UNCLEAR"
                status_reason = "以下字段属性在处理产物中找不到原文依据：" + "、".join(unsupported_paths)
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
            evidence_ids: list[int] = []
            for field_path, evidence_item, locator_status, locator_note, page_no, bbox in verified_evidence:
                evidence = Evidence(
                    evidence_no=public_no("EVD"),
                    audit_task_id=task.id,
                    document_upload_id=upload.id,
                    field_value_id=fact.id,
                    field_path=field_path,
                    page_no=page_no,
                    quote_text=evidence_item.quote_text,
                    bbox_json=bbox,
                    artifact_id=artifact_by_type[evidence_item.artifact_type].id,
                    locator_status=locator_status or "UNVERIFIED",
                    locator_note=locator_note,
                )
                db.add(evidence)
                db.flush()
                evidence_ids.append(evidence.id)
            if evidence_ids:
                # 兼容现有单证据读取口径：首条作为摘要入口；全部属性证据仍分别保存在 evidence。
                fact.evidence_id = evidence_ids[0]
            # 统一事实：把这次抽取同时写进 task_fact，供计算节点按
            # (字段编号, 期间, 报表口径, 主体范围) 取用。
            #
            # 为什么必须同时写两处：task_field_value 的唯一范围表达不了期间与报表口径，
            # 而财报场景的同一个科目在本期与上期是两个不同的数。两份记录指向同一次
            # 抽取（source_execution_id 相同），因此不会出现口径不一致。
            _write_field_task_fact(
                db,
                task,
                source_execution_id=execution.id,
                field=field,
                material_key=document.material_key,
                status=status,
                normalized_value=normalized_value,
            )
            field_requires_review = bool(field.get("required")) and status in {
                "NOT_PRESENT",
            }
            if field_requires_review or status in {"EVIDENCE_UNCLEAR", "NORMALIZATION_FAILED"}:
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
                            "evidences": _evidence_snapshots(db, fact.id),
                        },
                        business_note=f"字段“{field['name']}”需要人工确认",
                        proposed_value=raw_value,
                    )
                )
            extracted += 1
    rule_summary = _save_extraction_rule_results(db, task, manifest, execution, extraction_rules)
    return {
        "field_results": extracted,
        **rule_summary,
        "skipped": not selected_fields and rule_summary["extraction_rule_count"] == 0,
        "skip_reason": "当前节点没有任务范围内的抽取字段或规则" if not selected_fields and rule_summary["extraction_rule_count"] == 0 else None,
    }


def _write_field_task_fact(
    db: Session,
    task: AuditTask,
    *,
    source_execution_id: int | None,
    field: dict[str, Any],
    material_key: str,
    status: str,
    normalized_value: Any,
) -> None:
    """把抽取或人工纠错写进统一事实表。

    期间与报表口径取自字段配置（03_字段 的声明）。字段把这两列留空时，配置里存的是
    **显式的「不适用」**——那是这份配置真实表达的意思，不是系统替业务补的默认值，
    本函数不代填任何具体口径。

    有效状态必须同时写入与字段类型匹配的唯一取值列。类型不匹配时在这里明确
    失败，不产生「状态有效、值为空」或把错误金额塞入文本列的统一事实。
    """
    # 取数期间与报表口径**来自字段自己的声明**：计算参数按这两项定位事实，
    # 缺了就无法建立定位键。这里不代填默认值——默认成「本期 / 合并」会让一个
    # 声明了上期的计算参数静默取到本期的数，而结果看起来完全正常。
    period_scope = required_field_scope(
        field, "period_scope", code="TASK_SCHEMA_FIELD_SCOPE_MISSING", status_code=500
    )
    statement_scope = required_field_scope(
        field, "statement_scope", code="TASK_SCHEMA_FIELD_SCOPE_MISSING", status_code=500
    )
    entity_scope = str(field.get("entity_scope") or "SELF")
    value_type = str(field.get("value_type") or "DECIMAL")
    # task_field_value 保存抽取候选，供人工复核；task_fact 是计算与 Agent 使用的
    # 统一事实。证据或标准化尚未通过的候选不能在统一事实中携带可执行值。
    executable_value = normalized_value if status in {"PRESENT", "REVIEWED"} else None
    decimal_value = None
    text_value = None
    date_value = None
    boolean_value = None
    if executable_value is not None:
        strategy = field_strategy.strategy_of(field)
        try:
            if strategy == field_strategy.STRUCTURED:
                schema = field.get("structure_schema")
                if not isinstance(schema, dict):
                    raise ValueError("结构化字段缺少结构 Schema")
                structured = field_strategy.normalize_structured_value(executable_value, schema)
                text_value = json.dumps(
                    structured,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
            elif value_type == "DECIMAL":
                if isinstance(executable_value, bool):
                    raise ValueError("金额不能是布尔值")
                decimal_value = Decimal(str(executable_value).strip())
                if not decimal_value.is_finite():
                    raise ValueError("金额必须是有限数值")
            elif value_type == "DATE":
                date_value = (
                    executable_value
                    if type(executable_value) is date
                    else date.fromisoformat(str(executable_value).strip())
                )
            elif value_type == "BOOLEAN":
                if not isinstance(executable_value, bool):
                    raise ValueError("布尔字段的规范值必须是布尔值")
                boolean_value = executable_value
            elif value_type in {"STRING", "CLAUSE", "DECIMAL_CN"}:
                if not isinstance(executable_value, str) or not executable_value.strip():
                    raise ValueError("文本字段的规范值必须是非空文本")
                text_value = executable_value.strip()
            else:
                raise ValueError(f"字段类型不受支持：{value_type}")
        except (InvalidOperation, ValueError) as exc:
            raise BusinessError(
                "TASK_FACT_VALUE_INVALID",
                "字段规范值无法按字段契约写入统一事实",
                status_code=500,
                details={
                    "field_key": field.get("field_key"),
                    "material_key": material_key,
                    "value_type": value_type,
                    "status": status,
                    "reason": str(exc),
                },
            ) from exc
    elif status in {"PRESENT", "REVIEWED"}:
        raise BusinessError(
            "TASK_FACT_VALUE_MISSING",
            "字段已标记为有效，但没有可写入统一事实的规范值",
            status_code=500,
            details={"field_key": field.get("field_key"), "material_key": material_key, "status": status},
        )

    # 同一 (字段, 材料位, 期间, 口径) 已有当前事实时，把它置为非当前再插入新版本。
    # 事实不可变：绝不原地改写（改写会被数据库触发器拒绝）。
    previous = db.scalars(
        select(TaskFact).where(
            TaskFact.audit_task_id == task.id,
            TaskFact.fact_code == str(field.get("source_ref") or field["field_key"]),
            TaskFact.material_key == material_key,
            TaskFact.period_scope == period_scope,
            TaskFact.statement_scope == statement_scope,
            TaskFact.entity_scope == entity_scope,
        )
    ).all()
    revision_no = max((item.revision_no for item in previous), default=0) + 1
    current = [item for item in previous if item.is_current]
    if current:
        db.execute(
            update(TaskFact)
            .where(TaskFact.id.in_([item.id for item in current]))
            .values(is_current=False)
        )
        db.flush()
    db.add(
        TaskFact(
            audit_task_id=task.id,
            scenario_version_id=task.scenario_version_id,
            # 事实编号用业务字段编号（FEX-xxx）：计算参数按它引用，
            # 与字段身份（field_key）是两回事，转换在编译期完成。
            fact_code=str(field.get("source_ref") or field["field_key"]),
            fact_kind="EXTRACTED",
            material_key=material_key,
            entity_scope=entity_scope,
            period_scope=period_scope,
            statement_scope=statement_scope,
            value_type=value_type,
            decimal_value=decimal_value,
            text_value=text_value,
            date_value=date_value,
            boolean_value=boolean_value,
            unit=field.get("unit"),
            currency=field.get("currency"),
            source_execution_id=source_execution_id,
            revision_no=revision_no,
            is_current=True,
            status=status,
        )
    )
    db.flush()


def _extraction_result_payload(
    rule: dict[str, Any],
    field_definitions: dict[str, dict[str, Any]],
    material_names: dict[str, str],
    facts: list[tuple[TaskFieldValue, TaskFact] | None],
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
            # 状态和规范值来自 TaskFact；原文和证据仍来自候选字段版本。
            "status": pair[1].status if pair else "NOT_EXECUTED",
            "raw_value": pair[0].raw_value if pair else None,
            # task_field_value 会保留待复核候选，供人工核对；抽取规则结果只能暴露
            # 已形成有效事实的规范值，避免把 EVIDENCE_UNCLEAR / NORMALIZATION_FAILED
            # 的候选误读成规则可执行输入。
            "normalized_value": (
                fact_snapshot.task_fact_value(pair[1], field_definition=field_definitions[field_key])
                if pair and pair[1].status in {"PRESENT", "REVIEWED"}
                else None
            ),
            "has_evidence": bool(pair and pair[0].evidence_id),
        }
        for field_key, pair in zip(rule["outputs"], facts)
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
    rules: list[dict[str, Any]],
) -> dict[str, Any]:
    # 抽取结果可以展示候选原文，但状态与规范值必须已同步写入统一事实。
    current_pairs = _current_field_fact_pairs(db, task)
    field_definitions = _task_field_definitions(task)
    material_names = _material_names(manifest)
    counts: dict[str, int] = {}
    for rule in rules:
        facts: list[tuple[TaskFieldValue, TaskFact] | None] = []
        for field_key in rule["outputs"]:
            field = field_definitions.get(field_key)
            if not field:
                raise BusinessError("TASK_SCHEMA_FIELD_MISSING", "字段抽取规则引用的字段不在任务清单中", status_code=500)
            facts.append(current_pairs.get((field_key, field["material_key"])))
        statuses = [pair[1].status if pair else "NOT_EXECUTED" for pair in facts]
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
    """按规则明确 inputs 取统一事实，并从当前字段版本补充原文与证据。

    TaskFact 是规则和计算唯一的有效值来源；TaskFieldValue 保存抽取候选、原文、
    证据与人工版本。两者必须同时存在且状态一致，数据漂移时明确报错，不能靠查询
    顺序选一张表继续执行。
    """
    field_definitions = _task_field_definitions(task)
    material_states = _material_states(db, task)
    inputs: list[dict[str, Any]] = []
    for item in rule.get("inputs", []):
        field = field_definitions.get(item["field_key"])
        if field is None or field.get("material_key") != item["material_key"]:
            raise BusinessError(
                "TASK_SCHEMA_FIELD_MISSING",
                "规则输入不在任务冻结的字段清单中",
                status_code=500,
                details={"field_key": item["field_key"], "material_key": item["material_key"]},
            )
        field_value = db.scalar(
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
        period_scope = required_field_scope(
            field, "period_scope", code="TASK_SCHEMA_FIELD_SCOPE_MISSING", status_code=500
        )
        statement_scope = required_field_scope(
            field, "statement_scope", code="TASK_SCHEMA_FIELD_SCOPE_MISSING", status_code=500
        )
        task_fact = db.scalar(
            select(TaskFact).where(
                TaskFact.audit_task_id == task.id,
                TaskFact.scenario_version_id == task.scenario_version_id,
                TaskFact.fact_code == str(field.get("source_ref") or field["field_key"]),
                TaskFact.fact_kind == "EXTRACTED",
                TaskFact.material_key == item["material_key"],
                TaskFact.period_scope == period_scope,
                TaskFact.statement_scope == statement_scope,
                TaskFact.entity_scope == str(field.get("entity_scope") or "SELF"),
                TaskFact.is_current.is_(True),
            )
        )
        common = {
            "value_type": field.get("value_type"),
            "unit": field.get("unit"),
            "currency": field.get("currency"),
            "material_state": material_states.get(item["material_key"]),
        }
        if field_value is None and task_fact is None:
            inputs.append(
                {
                    **item,
                    **common,
                    "status": "NOT_EXECUTED",
                    "raw_value": None,
                    "normalized_value": None,
                    "evidence_id": None,
                    "evidence_ids": [],
                }
            )
            continue
        if field_value is None or task_fact is None:
            raise BusinessError(
                "TASK_FIELD_FACT_INCOMPLETE",
                "当前字段版本与统一事实没有成对生成，不能继续执行规则",
                status_code=500,
                details={
                    "field_key": item["field_key"],
                    "material_key": item["material_key"],
                    "field_value_exists": field_value is not None,
                    "task_fact_exists": task_fact is not None,
                },
            )
        if field_value.status != task_fact.status:
            raise BusinessError(
                "TASK_FACT_FIELD_STATUS_MISMATCH",
                "当前字段版本与统一事实的状态不一致，不能继续执行规则",
                status_code=500,
                details={
                    "field_key": item["field_key"],
                    "material_key": item["material_key"],
                    "field_status": field_value.status,
                    "fact_status": task_fact.status,
                },
            )
        expected_identity = {
            "value_type": field.get("value_type"),
            "unit": field.get("unit"),
            "currency": field.get("currency"),
        }
        actual_identity = {
            "value_type": task_fact.value_type,
            "unit": task_fact.unit,
            "currency": task_fact.currency,
        }
        if actual_identity != expected_identity:
            raise BusinessError(
                "TASK_FACT_FIELD_CONTRACT_MISMATCH",
                "统一事实的类型、单位或币种与任务字段契约不一致",
                status_code=500,
                details={
                    "field_key": item["field_key"],
                    "material_key": item["material_key"],
                    "expected": expected_identity,
                    "actual": actual_identity,
                },
            )
        normalized_value = (
            fact_snapshot.task_fact_value(task_fact, field_definition=field)
            if task_fact.status in {"PRESENT", "REVIEWED"}
            else None
        )
        if task_fact.status in {"PRESENT", "REVIEWED"} and normalized_value is None:
            raise BusinessError(
                "TASK_FACT_VALUE_MISSING",
                "统一事实已标记为有效，但没有可执行值",
                status_code=500,
                details={"field_key": item["field_key"], "material_key": item["material_key"]},
            )
        if (
            task_fact.status in {"PRESENT", "REVIEWED"}
            and fact_snapshot.canonical_field_value(field_value.normalized_value_json, field)
            != fact_snapshot.canonical_field_value(normalized_value, field)
        ):
            raise BusinessError(
                "TASK_FACT_FIELD_VALUE_MISMATCH",
                "当前字段候选值与统一事实的有效值不一致，不能继续执行规则",
                status_code=500,
                details={"field_key": item["field_key"], "material_key": item["material_key"]},
            )
        evidence_ids = [
            evidence.id
            for evidence in db.scalars(
                select(Evidence)
                .where(
                    Evidence.audit_task_id == task.id,
                    Evidence.field_value_id == field_value.id,
                    Evidence.rule_result_id.is_(None),
                )
                .order_by(Evidence.id)
            ).all()
        ]
        inputs.append(
            {
                **item,
                **common,
                "status": task_fact.status,
                "raw_value": field_value.raw_value,
                "normalized_value": normalized_value,
                "evidence_id": field_value.evidence_id,
                "evidence_ids": evidence_ids,
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
        executable = item.get("status") in {"PRESENT", "REVIEWED"} and item.get("normalized_value") is not None
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
                # `value` 是下游可参与判定的值。待复核候选仍保留在 raw_value，
                # 但不能回填到这个通用字段，否则报告会把候选原文误读成有效事实。
                "value": item.get("normalized_value") if executable else None,
                "has_evidence": bool(item.get("evidence_ids") or item.get("evidence_id")),
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
        raw_status = str(item.get("status"))
        status = labels.get(raw_status, raw_status)
        executable = raw_status in {"PRESENT", "REVIEWED"} and item.get("normalized_value") is not None
        raw = item.get("raw_value")
        normalized = item.get("normalized_value") if executable else None
        if raw not in (None, "") and normalized not in (None, "") and str(raw) != str(normalized):
            # 原文与标准化值不同时两个都要给：只给一个都无法核对系统到底拿什么去比。
            shown = f"{raw}（标准化 {normalized}）"
        elif raw not in (None, ""):
            shown = str(raw)
        elif normalized not in (None, ""):
            shown = str(normalized)
        else:
            shown = ""
        if not executable and raw not in (None, ""):
            shown = f"候选原文：{raw}"
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
    """按当前字段事实重算已经完成抽取节点的结果，返回被重算的规则键。

    一个工作流可以有多个字段抽取节点。人工复核可能位于其中两个节点之间，
    因此尚未执行的节点没有 RuleResult，不能因为它尚未执行就阻塞当前节点的事实重算。
    已完成节点若缺少结果仍显式报错，避免把已发生的数据缺失静默当成“无需重算”。
    """
    # 人工换版会同时写候选与统一事实；重算抽取结果前先确认两者没有漂移。
    current_pairs = _current_field_fact_pairs(db, task)
    field_definitions = _task_field_definitions(task)
    enabled_keys = set(task.task_schema_json["enabled_rule_keys"])
    extraction_rules = [
        rule
        for rule in manifest["rules"]
        if rule_node_type_of(rule) == "FIELD_EXTRACTION"
        and rule["operator"] == "LLM_EXTRACT"
        and rule["rule_key"] in enabled_keys
    ]
    extraction_node_codes = {rule["node"] for rule in extraction_rules}
    latest_executions: dict[str, NodeExecution] = {}
    if extraction_node_codes:
        for execution in db.scalars(
            select(NodeExecution)
            .where(
                NodeExecution.audit_task_id == task.id,
                NodeExecution.node_code.in_(extraction_node_codes),
                NodeExecution.status != "INVALIDATED",
            )
            .order_by(NodeExecution.id)
        ).all():
            latest_executions[execution.node_code] = execution
    completed_executions = {
        node_code: execution
        for node_code, execution in latest_executions.items()
        if execution.status == "SUCCEEDED"
    }
    recomputed: list[str] = []
    for rule in extraction_rules:
        execution = completed_executions.get(rule["node"])
        if execution is None:
            # 该抽取节点尚未完成，后续执行会按自身节点契约创建结果。
            continue
        statuses: list[str] = []
        rule_facts: list[tuple[TaskFieldValue, TaskFact] | None] = []
        for field_key in rule["outputs"]:
            field = field_definitions.get(field_key)
            if not field:
                raise BusinessError("TASK_SCHEMA_FIELD_MISSING", "字段抽取规则引用的字段不在任务清单中", status_code=500)
            pair = current_pairs.get((field_key, field["material_key"]))
            rule_facts.append(pair)
            statuses.append(pair[1].status if pair else "NOT_EXECUTED")
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
                # 这条抽取规则的归属是**节点实例码**（`rule["node"]`），不是能力类型：
                # 同一场景可以有两个字段抽取节点，写死能力类型当实例码后一条都查不到。
                RuleResult.node_execution_id == execution.id,
            )
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
