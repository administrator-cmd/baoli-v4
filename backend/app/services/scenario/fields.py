"""场景草稿字段配置、来源引用及删除检查。"""

from typing import Any
from sqlalchemy.orm import Session
from ...domain.calculation_config import calculations_referencing_field, remap_calculation_field_codes
from ...repositories.shared import configuration as config_store
from ...domain import field_strategy
from ...domain.rule_library import active
from ...common.exceptions import BusinessError
from ...entity import ScenarioDraft, ScenarioDraftField, ScenarioDraftRule, ScenarioDraftSection
from ...domain.identity import stable_key
from ...repositories.scenario.draft import next_sort_no
from .common import DRAFT_VALUE_TYPES, FIELD_ARTIFACT_SOURCES, _field_scope, _item_ref, _normalized_source_ref
from .draft import _locked_draft, _save_draft
from .materials import _draft_material_or_404


def _assert_field_source_ref_available(
    db: Session, draft_id: int, source_ref: str | None, *, field_key: str | None = None
) -> None:
    code = source_ref or field_key
    if code is None:
        return
    existing = next((item for item in config_store.rows(db, ScenarioDraftField, draft_id)
                     if (_normalized_source_ref(item.source_ref) or item.field_key) == code
                     and (field_key is None or item.field_key != field_key)), None)
    if existing is not None:
        raise BusinessError(
            "FIELD_CODE_DUPLICATE",
            f"字段编号“{code}”已由字段“{existing.name}”使用",
            status_code=409,
            details={"source_ref": code, "field_key": existing.field_key},
        )


def create_draft_field(db: Session, scenario_code: str, payload: dict[str, Any]) -> ScenarioDraft:
    """在草稿中新增字段。已保存到草稿，发布后才供新方案使用。"""
    template, draft = _locked_draft(db, scenario_code)
    name = str(payload.get("name") or "").strip()
    if not name:
        raise BusinessError("FIELD_NAME_REQUIRED", "字段名称不能为空", status_code=400)
    material_key = str(payload.get("material_key") or "")
    _draft_material_or_404(db, draft.id, material_key)
    value_type = str(payload.get("value_type") or "STRING")
    if value_type not in DRAFT_VALUE_TYPES:
        raise BusinessError(
            "FIELD_TYPE_INVALID",
            "字段类型不受支持",
            status_code=400,
            details={"value_type": value_type, "allowed": sorted(DRAFT_VALUE_TYPES)},
        )
    extraction_strategy, structure_schema = field_strategy.validate_field_strategy(
        value_type=value_type,
        extraction_strategy=payload.get("extraction_strategy"),
        structure_schema=payload.get("structure_schema"),
        field_name=name,
    )
    source_ref = _normalized_source_ref(payload.get("source_ref"))
    _assert_field_source_ref_available(db, draft.id, source_ref)
    field_key = stable_key("fld", scenario_code, _item_ref())
    field = config_store.add(
        db, ScenarioDraftField(
            draft_id=draft.id,
            field_key=field_key,
            material_key=material_key,
            name=name,
            value_type=value_type,
            extraction_strategy=extraction_strategy,
            structure_schema_json=structure_schema,
            unit=(str(payload["unit"]).strip() or None) if payload.get("unit") else None,
            currency=(str(payload["currency"]).strip() or None) if payload.get("currency") else None,
            artifact_source=(
                str(payload["artifact_source"]).strip() or None
            )
            if payload.get("artifact_source")
            else None,
            period_scope=_field_scope(payload, "period_scope"),
            statement_scope=_field_scope(payload, "statement_scope"),
            required=bool(payload.get("required", False)),
            instruction=str(payload.get("instruction") or ""),
            source_ref=source_ref,
            sort_no=next_sort_no(db, ScenarioDraftField, draft.id),
        )
    )
    field.node_code = "RULE_EXECUTION"
    _save_draft(
        db,
        template,
        draft,
        operation_type="DRAFT_FIELD_CREATE",
        object_name=name,
        detail={"scenario_code": scenario_code, "field_key": field_key},
    )
    return draft


def _draft_field_or_404(db: Session, draft_id: int, field_key: str) -> ScenarioDraftField:
    item = next(iter([item for item in config_store.rows(db, ScenarioDraftField, draft_id) if (item.field_key == field_key)]), None)
    if not item:
        raise BusinessError("DRAFT_FIELD_NOT_FOUND", "字段配置不存在", status_code=404, details={"field_key": field_key})
    return item


def _rules_referencing_field(db: Session, draft_id: int, field_key: str) -> list[str]:
    rules = config_store.rows(db, ScenarioDraftRule, draft_id)
    return [
        rule.name
        for rule in rules
        if any(entry.get("field_key") == field_key for entry in (rule.rule_json or {}).get("inputs", []))
        or field_key in ((rule.rule_json or {}).get("outputs") or [])
    ]


def update_draft_field(db: Session, scenario_code: str, field_key: str, payload: dict[str, Any]) -> ScenarioDraft:
    """修改草稿中的字段（只覆盖提交的字段）。"""
    template, draft = _locked_draft(db, scenario_code)
    item = _draft_field_or_404(db, draft.id, field_key)
    if "material_key" in payload and payload["material_key"] is not None and payload["material_key"] != item.material_key:
        referencing = _rules_referencing_field(db, draft.id, field_key)
        if referencing:
            raise BusinessError(
                "FIELD_MATERIAL_CHANGE_REFERENCED",
                "该字段已被规则引用，先调整相关规则后再更换所属材料",
                status_code=409,
                details={"field_name": item.name, "rule_keys": referencing},
            )
        _draft_material_or_404(db, draft.id, str(payload["material_key"]))
        item.material_key = str(payload["material_key"])
    if "name" in payload and payload["name"] is not None:
        name = str(payload["name"]).strip()
        if not name:
            raise BusinessError("FIELD_NAME_REQUIRED", "字段名称不能为空", status_code=400)
        item.name = name
    if "value_type" in payload and payload["value_type"] is not None:
        value_type = str(payload["value_type"])
        if value_type not in DRAFT_VALUE_TYPES:
            raise BusinessError(
                "FIELD_TYPE_INVALID",
                "字段类型不受支持",
                status_code=400,
                details={"value_type": value_type, "allowed": sorted(DRAFT_VALUE_TYPES)},
            )
        item.value_type = value_type
    if "extraction_strategy" in payload or "structure_schema" in payload or "value_type" in payload:
        extraction_strategy, structure_schema = field_strategy.validate_field_strategy(
            value_type=item.value_type,
            extraction_strategy=(
                payload.get("extraction_strategy")
                if "extraction_strategy" in payload
                else item.extraction_strategy
            ),
            structure_schema=(
                payload.get("structure_schema")
                if "structure_schema" in payload
                else item.structure_schema_json
            ),
            field_name=item.name,
            enabled=active(item.data),
        )
        item.extraction_strategy = extraction_strategy
        item.structure_schema_json = structure_schema
    if "unit" in payload:
        unit = payload.get("unit")
        item.unit = (str(unit).strip() or None) if unit else None
    if "currency" in payload:
        currency = payload.get("currency")
        item.currency = (str(currency).strip() or None) if currency else None
    if "artifact_source" in payload:
        # 产物来源：留空表示字段由常规字段抽取提供，不表示“未声明所以随便猜”。
        artifact_source = payload.get("artifact_source")
        normalized_source = (str(artifact_source).strip() or None) if artifact_source else None
        if normalized_source is not None and normalized_source not in FIELD_ARTIFACT_SOURCES:
            raise BusinessError(
                "FIELD_ARTIFACT_SOURCE_INVALID",
                "字段的产物来源不受支持",
                status_code=400,
                details={"artifact_source": normalized_source, "allowed": sorted(FIELD_ARTIFACT_SOURCES)},
            )
        item.artifact_source = normalized_source
    if "period_scope" in payload and payload["period_scope"] is not None:
        item.period_scope = _field_scope(payload, "period_scope")
    if "statement_scope" in payload and payload["statement_scope"] is not None:
        item.statement_scope = _field_scope(
            payload, "statement_scope"
        )
    if "required" in payload and payload["required"] is not None:
        item.required = bool(payload["required"])
    if "instruction" in payload and payload["instruction"] is not None:
        item.instruction = str(payload["instruction"])
    if "source_ref" in payload:
        source_ref = _normalized_source_ref(payload.get("source_ref"))
        _assert_field_source_ref_available(db, draft.id, source_ref, field_key=field_key)
        previous_code = _normalized_source_ref(item.source_ref) or field_key
        next_code = source_ref or field_key
        if previous_code != next_code and previous_code != field_key:
            # 内部字段键不随业务编号修改；只更新按原业务编号保存的引用。
            draft.calculation_json = remap_calculation_field_codes(
                draft.calculation_json, {previous_code: next_code},
            )
        item.source_ref = source_ref
    _save_draft(
        db,
        template,
        draft,
        operation_type="DRAFT_FIELD_UPDATE",
        object_name=item.name,
        detail={"scenario_code": scenario_code, "field_key": field_key},
    )
    return draft


def delete_draft_field(db: Session, scenario_code: str, field_key: str) -> ScenarioDraft:
    """删除草稿中的字段；仍被规则、计算或报告引用时 409，不级联删除。"""
    template, draft = _locked_draft(db, scenario_code)
    item = _draft_field_or_404(db, draft.id, field_key)
    referencing = _rules_referencing_field(db, draft.id, field_key)
    calculations = calculations_referencing_field(
        draft.calculation_json, field_key,
    )
    sections = config_store.rows(db, ScenarioDraftSection, draft.id)
    referencing_sections = [section.title for section in sections if field_key in (section.field_keys or [])]
    if referencing or calculations or referencing_sections:
        raise BusinessError(
            "FIELD_REFERENCED",
            "该字段仍被规则、计算或报告章节引用，请先移除引用后再删除",
            status_code=409,
            details={"field_name": item.name, "rule_keys": referencing,
                     "calculation_rules": calculations, "section_names": referencing_sections},
        )
    config_store.delete(item)
    _save_draft(
        db,
        template,
        draft,
        operation_type="DRAFT_FIELD_DELETE",
        object_name=item.name,
        detail={"scenario_code": scenario_code, "field_key": field_key},
    )
    return draft
