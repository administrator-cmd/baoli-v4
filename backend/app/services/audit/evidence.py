"""证据的内容依据核对、返回投影与复制。

`quote_check` 仅表示完整引文能否在所属产物中找到
（FOUND / NOT_FOUND / NOT_CHECKABLE），不依赖模型生成页码或坐标。

比对前消除 OCR 排版产物（行内公式包裹、Markdown 标记、HTML 标签、空白换行），但仍按规范化后
的连续子串判定，不做相似度匹配；原始引文原样保存与展示。"""

from ...domain.identity import public_no
from ...common.exceptions import BusinessError
from ...entity import AuditTask, Evidence, RuleResult, StoredArtifact, TaskFieldValue
from .artifacts import _artifact_payload
from .shared import QUOTE_CHECK_LABELS, _locatable_text
from sqlalchemy import select
from sqlalchemy.orm import Session
from typing import Any


def result_content_view(value: dict[str, Any]) -> dict[str, Any]:
    """返回规则结果，不触碰字段值和业务配置 JSON。"""
    return dict(value)


def _check_evidence_content(texts: list[str], quote_text: str) -> str:
    """仅定位引文；不核对金额、日期、单位或抽取值。"""
    quoted = _locatable_text(quote_text)
    if not quoted:
        return "NOT_CHECKABLE"
    return "FOUND" if any(quoted in _locatable_text(text) for text in texts) else "NOT_FOUND"


def artifact_content_texts(content: Any) -> list[str]:
    """只收集实际文本，不把 JSON 转义字符当成原文。"""
    if isinstance(content, str):
        return [content]
    if isinstance(content, dict):
        return [text for value in content.values() for text in artifact_content_texts(value)]
    if isinstance(content, list):
        return [text for value in content for text in artifact_content_texts(value)]
    return []


def fact_evidence_ids(db: Session, task: AuditTask, fact: Any) -> list[int]:
    """按事实的字段身份和来源执行批次读取证据，避免串用另一版字段。"""
    if fact.fact_kind == "DERIVED":
        return list(db.scalars(select(Evidence.id).join(RuleResult, Evidence.rule_result_id == RuleResult.id).where(
            RuleResult.audit_task_id == task.id, RuleResult.node_execution_id == fact.source_execution_id,
            RuleResult.rule_code == fact.calculation_rule_code)))
    from ...domain.fact_projection import field_identity_index, fact_identity
    index = field_identity_index({field["field_key"]: field for field in task.task_schema_json["fields"]})
    key = index.get(fact_identity(fact))
    if key is None:
        raise BusinessError("TASK_FACT_FIELD_UNDECLARED", "事实证据无法关联冻结的完整字段身份", status_code=500,
                            details={"fact_id": fact.id})
    return list(db.scalars(select(Evidence.id).join(TaskFieldValue, Evidence.field_value_id == TaskFieldValue.id).where(
        TaskFieldValue.audit_task_id == task.id, TaskFieldValue.field_key == key,
        TaskFieldValue.material_key == fact.material_key, TaskFieldValue.is_current.is_(True),
        TaskFieldValue.extraction_execution_id == fact.source_execution_id,
        Evidence.rule_result_id.is_(None))))


def save_artifact_references(db: Session, task: AuditTask, execution: Any, rule_result: RuleResult, rule: dict[str, Any],
                             verdict: dict[str, Any] | None) -> list[int]:
    """保存已核对的工具产物引用；字段引用绑定本次真实抽取记录。"""
    definitions = {field["field_key"]: field for field in rule.get("output_definitions") or []}
    evidence_ids = []
    seen = set()
    for reference in (verdict or {}).get("evidence") or []:
        artifact_id = reference.get("artifact_id")
        if artifact_id is None:
            continue
        artifact = db.get(StoredArtifact, artifact_id)
        if artifact is None or artifact.audit_task_id != task.id or artifact.status != "READY":
            raise BusinessError("RULE_EVIDENCE_ARTIFACT_INVALID", "证据引用的产物不属于当前任务的成功产物", status_code=500)
        output_code = reference.get("output_code")
        field_value = None
        if output_code is not None:
            definition = definitions[output_code]
            if reference.get("material_key") != definition["material_key"]:
                raise BusinessError("RULE_EVIDENCE_SOURCE_INVALID", "字段证据与声明材料不一致")
            field_value = db.scalar(select(TaskFieldValue).where(
                TaskFieldValue.audit_task_id == task.id, TaskFieldValue.field_key == output_code,
                TaskFieldValue.document_upload_id == artifact.document_upload_id,
                TaskFieldValue.extraction_execution_id == execution.id, TaskFieldValue.is_current.is_(True)))
            # 没有本次抽取记录（例如沿用人工值）时，引用只作规则依据。
        identity = (artifact.id, field_value.id if field_value else None, reference.get("quote_text"))
        if identity in seen:
            continue
        seen.add(identity)
        evidence = Evidence(evidence_no=public_no("EVD"), audit_task_id=task.id,
                            document_upload_id=artifact.document_upload_id, artifact_id=artifact.id,
                            field_value_id=field_value.id if field_value else None,
                            rule_result_id=None if field_value else rule_result.id,
                            quote_text=reference.get("quote_text") or "")
        db.add(evidence)
        db.flush()
        if field_value:
            if field_value.evidence_id is None:
                field_value.evidence_id = evidence.id
            evidence_ids.append(evidence.id)
    return evidence_ids


def _evidence_snapshot(db: Session, evidence_id: int | None) -> dict[str, Any] | None:
    """复核提交前的证据快照；人工值不以旧引文作证明，因此快照只用于说明系统原判断。"""
    if not evidence_id:
        return None
    evidence = db.get(Evidence, evidence_id)
    if not evidence:
        return None
    quote_check = _quote_check_of(db, evidence)
    return {
        "evidence_no": evidence.evidence_no,
        "field_path": evidence.field_path,
        "quote_text": evidence.quote_text,
        # 内容依据结论：引文在所属材料里是否找得到。
        "quote_check": quote_check,
        "quote_check_label": QUOTE_CHECK_LABELS.get(quote_check, "内容依据状态未知"),
    }


def _evidence_snapshots(db: Session, field_value_id: int) -> list[dict[str, Any]]:
    """字段事实的全部原始证据快照；规则复制出来的证据不重复计入。"""
    rows = db.scalars(
        select(Evidence)
        .where(
            Evidence.field_value_id == field_value_id,
            Evidence.rule_result_id.is_(None),
        )
        .order_by(Evidence.id)
    ).all()
    return [snapshot for row in rows if (snapshot := _evidence_snapshot(db, row.id)) is not None]


def _rule_evidence_snapshots(db: Session, rule_result_id: int) -> list[dict[str, Any]]:
    """规则结果在形成复核待办时使用的全部证据快照。"""
    rows = db.scalars(
        select(Evidence)
        .where(Evidence.rule_result_id == rule_result_id)
        .order_by(Evidence.id)
    ).all()
    return [snapshot for row in rows if (snapshot := _evidence_snapshot(db, row.id)) is not None]


def _copy_evidence_to_rule(db: Session, task: AuditTask, rule_result: RuleResult, evidence_ids: list[int]) -> list[str]:
    copied_numbers: list[str] = []
    for evidence_id in sorted(set(evidence_ids)):
        source = db.get(Evidence, evidence_id)
        if not source or source.audit_task_id != task.id:
            raise BusinessError("RULE_EVIDENCE_MISSING", "规则输入引用的证据不存在", status_code=500)
        copied = Evidence(
            evidence_no=public_no("EVD"),
            audit_task_id=task.id,
            document_upload_id=source.document_upload_id,
            rule_result_id=rule_result.id,
            field_value_id=source.field_value_id,
            field_path=source.field_path,
            quote_text=source.quote_text,
            artifact_id=source.artifact_id,
        )
        db.add(copied)
        copied_numbers.append(copied.evidence_no)
    if copied_numbers:
        db.flush()
    return copied_numbers

def _copy_artifact_evidence_to_rule(
    db: Session,
    task: AuditTask,
    rule_result: RuleResult,
    artifacts: list[StoredArtifact],
) -> list[str]:
    copied_numbers: list[str] = []
    for artifact in artifacts:
        # 产物元数据的解析口径与读取统一走 artifacts：本模块不直接理解供应商 DTO。
        for source in _artifact_payload(artifact).evidence:
            copied = Evidence(
                evidence_no=public_no("EVD"),
                audit_task_id=task.id,
                document_upload_id=artifact.document_upload_id,
                rule_result_id=rule_result.id,
                quote_text=source.quote_text,
                artifact_id=artifact.id,
            )
            db.add(copied)
            copied_numbers.append(copied.evidence_no)
    if copied_numbers:
        db.flush()
    return copied_numbers

def _fact_evidence_suffix(evidence_by_id: dict[int, Evidence], fact: TaskFieldValue) -> str:
    evidences = sorted(
        (
            evidence
            for evidence in evidence_by_id.values()
            if evidence.field_value_id == fact.id and evidence.rule_result_id is None
        ),
        key=lambda item: item.id,
    )
    if not evidences:
        return "（未记录证据原文）"
    parts = []
    for evidence in evidences:
        path = f"属性 {evidence.field_path}｜" if evidence.field_path else ""
        parts.append(f"{path}原文：{evidence.quote_text}")
    return f"（证据：{'；'.join(parts)}）"

def _quote_check_of(db: Session, evidence: Evidence) -> str:
    """一条证据的内容依据结论：引文能否在它所属的处理产物里找到原文。

    逐条按所属产物定位引文，不落库，不比较字段值或业务口径。
    """
    if not evidence.artifact_id or not (evidence.quote_text or "").strip():
        return "NOT_CHECKABLE"
    artifact = db.get(StoredArtifact, evidence.artifact_id)
    if artifact is None:
        return "NOT_CHECKABLE"
    payload = _artifact_payload(artifact)
    texts = artifact_content_texts(payload.content)
    if artifact.artifact_type == "OCR":
        texts.append("\n\n".join(page["text"] for page in payload.content["pages"]))
    texts.extend(item.quote_text for item in payload.evidence)
    if not texts:
        return "NOT_CHECKABLE"
    return _check_evidence_content(texts, evidence.quote_text)
