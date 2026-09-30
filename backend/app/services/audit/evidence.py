"""证据的定位核对、内容依据核对与复制。

两个结论必须分开表达，不能互相顶替：

- `quote_check`：引文或字段值能否在**它所属的那个产物**里找到原文依据（FOUND /
  VALUE_FOUND / NOT_FOUND / NOT_CHECKABLE）。它决定字段内容是否可用于审核。
- `locator_status`：页码与坐标能否准确定位到该页（VERIFIED / UNVERIFIED / UNLOCATABLE）。
  它只是辅助信息，页码缺失或写错**不**否定字段内容。

比对前消除 OCR 排版产物（行内公式包裹、Markdown 标记、HTML 标签、空白换行），
但仍按规范化后的连续子串判定，不做相似度匹配；原始引文原样保存与展示。"""

import math
from ...domain.identity import public_no
from ...common.exceptions import BusinessError
from ...entity import AuditTask, Evidence, RuleResult, StoredArtifact, TaskFieldValue
from .artifacts import _artifact_page_text, _artifact_payload
from .shared import LOCATOR_STATUS_LABELS, QUOTE_CHECK_LABELS, _evidence_supports_value, _locatable_text
from sqlalchemy import select
from sqlalchemy.orm import Session
from typing import Any


def _verify_evidence_location(
    artifact: StoredArtifact,
    evidence: Any,
    *,
    raw_value: Any = None,
    value_type: str | None = None,
    unit: str | None = None,
    currency: str | None = None,
) -> tuple[str, str | None, int | None, list[float] | None, str | None]:
    """按产物真实内容核对模型给出的证据位置。

    返回 (locator_status, locator_note, page_no, bbox, quote_check)。
    定位与内容依据是两件事（口径见模块说明）：页码缺失、页码错、坐标格式错误都只影响
    定位结论，不单独否定字段内容；引文逐字定位失败时，如果字段原始值仍能在声明页或
    产物中确定性核对，内容仍可用，只把 quote_check 标为 VALUE_FOUND。只有原始值也找不到
    原文依据时，内容才按证据不足处理。

    坐标核对不通过时一律置空并在说明里写清原因，绝不伪造高亮。
    """
    page_no = evidence.page_no
    bbox = evidence.bbox
    bbox_valid = not isinstance(bbox, list) or (
        len(bbox) == 4
        and all(
            isinstance(item, (int, float)) and not isinstance(item, bool) and math.isfinite(float(item))
            for item in bbox
        )
    )
    bbox = bbox if bbox_valid else None
    bbox_note = None if bbox_valid else "证据坐标不符合 [x0,y0,x1,y1] 且数值有限的契约，坐标已置空"
    pages = _artifact_page_text(artifact)
    if pages is None:
        # 产物没有可核对的页文本（例如印章识别只给坐标）：内容依据无法在这里核对，
        # 不因此断言字段内容有问题，也不伪造定位结论。
        return (
            "UNVERIFIED",
            "该处理产物没有可核对的页文本，无法在本产物内核对引文的原文依据与页码",
            page_no,
            bbox,
            "NOT_CHECKABLE",
        )
    quoted = _locatable_text(evidence.quote_text) if evidence.quote_text else ""
    located_pages: list[int] = []
    if quoted:
        located_pages = [number for number, text in pages.items() if quoted in _locatable_text(text)]
    value_pages: list[int] = []
    if raw_value is not None:
        value_pages = [
            number
            for number, text in pages.items()
            if _evidence_supports_value(
                raw_value,
                text,
                value_type=value_type,
                unit=unit,
                currency=currency,
            )
        ]
        quote_supports_value = _evidence_supports_value(
            raw_value,
            evidence.quote_text,
            value_type=value_type,
            unit=unit,
            currency=currency,
        )
        # 首先采用引文逐字命中。若模型引文带有 OCR 排版差异或引用范围不准确，
        # 只要字段原始值仍能在声明页（没有页码时则在产物某页）被确定性核对，
        # 记录 VALUE_FOUND：字段事实可以执行，但引文定位仍会在证据记录中显示为
        # UNVERIFIED/UNLOCATABLE。只有原始值也找不到时才返回 NOT_FOUND，触发人工复核。
        if located_pages and quote_supports_value:
            quote_check = "FOUND"
        elif quote_supports_value and value_pages:
            quote_check = "VALUE_FOUND"
        else:
            quote_check = "NOT_FOUND"
    else:
        quote_check = "FOUND" if located_pages else ("NOT_FOUND" if quoted else "NOT_CHECKABLE")
    if bbox_note:
        return "UNLOCATABLE", bbox_note, page_no, None, quote_check
    if page_no is None:
        if located_pages:
            return (
                "UNVERIFIED",
                f"证据未声明页码；引文在该产物第 {'、'.join(str(number) for number in located_pages)} 页找到原文依据，"
                "坐标保持原值",
                None,
                bbox,
                quote_check,
            )
        return (
            "UNVERIFIED",
            "证据未声明页码，无法核对引文所在页；坐标保持原值",
            None,
            bbox,
            quote_check,
        )
    if page_no not in pages:
        return (
            "UNLOCATABLE",
            f"证据页码 {page_no} 不在该产物页数范围内（共 {len(pages)} 页），坐标已置空",
            page_no,
            None,
            quote_check,
        )
    if quoted and quoted not in _locatable_text(pages[page_no]):
        if located_pages:
            return (
                "UNLOCATABLE",
                f"引文不在声明页码第 {page_no} 页，实际出现在第 "
                f"{'、'.join(str(number) for number in located_pages)} 页，坐标已置空",
                page_no,
                None,
                quote_check,
            )
        return (
            "UNLOCATABLE",
            "引文在该产物的页文本中找不到，坐标已置空；这是内容依据的核对结论，"
            "由原文依据核对决定字段内容是否可用",
            None,
            None,
            quote_check,
        )
    return "VERIFIED", None, page_no, bbox, quote_check

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
        "page_no": evidence.page_no,
        "quote_text": evidence.quote_text,
        "bbox": evidence.bbox_json,
        "locator_status": evidence.locator_status,
        "locator_note": evidence.locator_note,
        # 内容依据结论：引文在所属材料里是否找得到。人工判断需要它与位置结论分开看。
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
            page_no=source.page_no,
            quote_text=source.quote_text,
            bbox_json=source.bbox_json,
            artifact_id=source.artifact_id,
            # 引用工单证据时保留它自己的定位核对结论，不重新断言为已核对。
            locator_status=source.locator_status,
            locator_note=source.locator_note,
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
                page_no=source.page_no,
                quote_text=source.quote_text,
                bbox_json=source.bbox,
                artifact_id=artifact.id,
                # 产物自带的证据记录未在页文本内复核，如实标注为未核实。
                locator_status="UNVERIFIED",
                locator_note="证据来自外部处理产物自身记录，未在产物页文本内复核",
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
        page = f"第 {evidence.page_no} 页" if evidence.page_no else "未标注页码"
        locator = LOCATOR_STATUS_LABELS.get(evidence.locator_status, "状态未知")
        path = f"属性 {evidence.field_path}｜" if evidence.field_path else ""
        parts.append(f"{path}{page}｜定位核对：{locator}｜原文：{evidence.quote_text}")
    return f"（证据：{'；'.join(parts)}）"

def _quote_check_of(db: Session, evidence: Evidence) -> str:
    """一条证据的**内容依据**结论：引文能否在它所属的处理产物里找到原文。

    与定位结论分开表达。逐条按产物重新核对，不落库、不改写历史记录：
    这里只回答「这条引文现在还能不能在它声称的产物里找到」，
    找不到就是找不到，绝不用别的产物或别的位置顶上。
    """
    if not evidence.artifact_id or not (evidence.quote_text or "").strip():
        return "NOT_CHECKABLE"
    artifact = db.get(StoredArtifact, evidence.artifact_id)
    if artifact is None:
        return "NOT_CHECKABLE"
    pages = _artifact_page_text(artifact)
    if pages is None:
        return "NOT_CHECKABLE"
    quoted = _locatable_text(evidence.quote_text)
    located_pages = [number for number, text in pages.items() if quoted in _locatable_text(text)]
    field_value = db.get(TaskFieldValue, evidence.field_value_id) if evidence.field_value_id else None
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
    if located_pages and (
        field_value is None
        or _evidence_supports_value(
            evidence_value,
            evidence.quote_text,
            value_type=value_type,
            unit=unit,
            currency=currency,
        )
    ):
        return "FOUND"
    if (
        field_value is not None
        and _evidence_supports_value(
            evidence_value,
            evidence.quote_text,
            value_type=value_type,
            unit=unit,
            currency=currency,
        )
        and not located_pages
        and evidence.page_no in pages
        and _evidence_supports_value(
            evidence_value,
            pages[evidence.page_no],
            value_type=value_type,
            unit=unit,
            currency=currency,
        )
    ):
        return "VALUE_FOUND"
    return "NOT_FOUND"
