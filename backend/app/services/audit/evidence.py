"""证据的内容依据核对、返回投影与复制。

`quote_check` 表示引文或字段值能否在所属产物中找到原文依据
（FOUND / VALUE_FOUND / NOT_FOUND / NOT_CHECKABLE），不依赖模型生成页码或坐标。

比对前消除 OCR 排版产物（行内公式包裹、Markdown 标记、HTML 标签、空白换行），但仍按规范化后
的连续子串判定，不做相似度匹配；原始引文原样保存与展示。"""

from ...domain.identity import public_no
from ...common.exceptions import BusinessError
from ...entity import AuditTask, Evidence, RuleResult, StoredArtifact, TaskFieldValue
from .. import task_csv_service
from .artifacts import _artifact_page_text, _artifact_payload
from .shared import QUOTE_CHECK_LABELS, _evidence_supports_value, _locatable_text
from sqlalchemy import select
from sqlalchemy.orm import Session
from typing import Any


def evidence_content_view(value: dict[str, Any] | None) -> dict[str, Any] | None:
    """只投影证据原文和来源，历史快照中的定位字段不再返回。"""
    if value is None:
        return None
    return {
        key: item for key, item in value.items()
        if key not in {
            "page_no", "page_no_source", "bbox", "bbox_json", "locator_status", "locator_note",
            "char_start", "char_end", "heading_path",
        }
    }


def retrieval_content_view(value: dict[str, Any] | None) -> dict[str, Any] | None:
    """仅投影检索候选和采用证据，保留真实身份及范围，不改写检索记录。"""
    if value is None:
        return None
    result = dict(value)
    for key in ("candidates", "adopted"):
        if key in value:
            result[key] = [evidence_content_view(item) for item in value[key]]
    return result


def result_content_view(value: dict[str, Any]) -> dict[str, Any]:
    """结果及复核中的检索/印章投影，不触碰字段值和业务配置 JSON。"""
    result = dict(value)
    if "retrieval" in value:
        result["retrieval"] = retrieval_content_view(value["retrieval"])
    if "seal_records" in value and value["seal_records"] is not None:
        result["seal_records"] = [evidence_content_view(item) for item in value["seal_records"]]
    return result


def review_content_view(value: dict[str, Any]) -> dict[str, Any]:
    """历史和新复核快照共用的证据视图，保留原记录不变。"""
    result = result_content_view(value)
    if "evidence" in value:
        result["evidence"] = evidence_content_view(value["evidence"])
    if "evidences" in value and value["evidences"] is not None:
        result["evidences"] = [evidence_content_view(item) for item in value["evidences"]]
    return result


def _check_evidence_content(
    texts: list[str], quote_text: str, *, raw_value: Any = None,
    value_type: str | None = None, unit: str | None = None, currency: str | None = None,
) -> str:
    """核对所属产物中的引文和字段值，不依赖模型声明的位置。"""
    quoted = _locatable_text(quote_text)
    quote_found = bool(quoted) and any(quoted in _locatable_text(text) for text in texts)
    if raw_value is None:
        return "FOUND" if quote_found else ("NOT_FOUND" if quoted else "NOT_CHECKABLE")
    supported = _evidence_supports_value(
        raw_value, quote_text, value_type=value_type, unit=unit, currency=currency,
    )
    if quote_found and supported:
        return "FOUND"
    if supported and any(
        _evidence_supports_value(raw_value, text, value_type=value_type, unit=unit, currency=currency)
        for text in texts
    ):
        return "VALUE_FOUND"
    return "NOT_FOUND"


def _verify_evidence_content(
    artifact: StoredArtifact, evidence: Any, *, raw_value: Any = None,
    value_type: str | None = None, unit: str | None = None, currency: str | None = None,
) -> str:
    """按所属处理产物的真实文本核对内容；没有可核对文本时如实标记。"""
    pages = _artifact_page_text(artifact)
    if pages is None:
        return "NOT_CHECKABLE"
    return _check_evidence_content(
        list(pages.values()), evidence.quote_text, raw_value=raw_value,
        value_type=value_type, unit=unit, currency=currency,
    )


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

    逐条按所属产物重新核对，不落库、不改写历史记录；与抽取时共用内容核对口径，
    不借用其它产物中的引文或字段值。
    """
    if not evidence.artifact_id or not (evidence.quote_text or "").strip():
        return "NOT_CHECKABLE"
    artifact = db.get(StoredArtifact, evidence.artifact_id)
    if artifact is None:
        return "NOT_CHECKABLE"
    pages = _artifact_page_text(artifact)
    if pages is None:
        return "NOT_CHECKABLE"
    field_value = None
    if evidence.field_value_id:
        task = db.get(AuditTask, evidence.audit_task_id)
        indexed = db.get(TaskFieldValue, evidence.field_value_id)
        if indexed is not None and task is not None:
            field_value = task_csv_service.from_index(db, task, TaskFieldValue, [indexed])[0]
    evidence_value = field_value.raw_value if field_value else None
    value_type = unit = currency = None
    if field_value is not None:
        task = db.get(AuditTask, evidence.audit_task_id)
        field = next(
            (
                item
                for item in ((task.task_schema_json or {}).get("fields") or [])
                if item.get("field_key") == field_value.field_key
                and item.get("material_key") == field_value.material_key
            ),
            {},
        ) if task is not None else {}
        value_type = field.get("value_type")
        unit = field.get("unit")
        currency = field.get("currency")
    if field_value and evidence.field_path and isinstance(field_value.normalized_value_json, dict):
        evidence_value = field_value.normalized_value_json.get(evidence.field_path)
        property_schema = ((field.get("structure_schema") or {}).get("properties") or {}).get(evidence.field_path)
        value_type = property_schema.get("type") if isinstance(property_schema, dict) else None
        unit = currency = None
    return _check_evidence_content(
        list(pages.values()), evidence.quote_text, raw_value=evidence_value,
        value_type=value_type, unit=unit, currency=currency,
    )
