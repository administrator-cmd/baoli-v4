"""场景草稿材料配置及依赖检查。"""

from typing import Any

from sqlalchemy.orm import Session

from ...common.exceptions import BusinessError
from ...domain.identity import stable_key
from ...entity.scenario_drafts import ScenarioDraft, ScenarioDraftField, ScenarioDraftMaterial, ScenarioDraftRule
from ..shared import configuration as config_store
from .common import _item_ref, _normalized_source_ref
from .draft import _locked_draft, _save_draft, next_sort_no


def create_draft_material(db: Session, scenario_code: str, payload: dict[str, Any]) -> ScenarioDraft:
    """在草稿中新增材料位。已保存到草稿，发布后才供新方案使用。"""
    template, draft = _locked_draft(db, scenario_code)
    name = str(payload.get("name") or "").strip()
    if not name:
        raise BusinessError("MATERIAL_NAME_REQUIRED", "材料名称不能为空", status_code=400)
    source_ref = _normalized_source_ref(payload.get("source_ref"))
    material_key = stable_key("mat", scenario_code, _item_ref())
    config_store.add(
        db, ScenarioDraftMaterial(
            draft_id=draft.id,
            material_key=material_key,
            name=name,
            required=bool(payload.get("required", True)),
            description=str(payload.get("description") or ""),
            source_ref=source_ref,
            sort_no=next_sort_no(db, ScenarioDraftMaterial, draft.id),
        )
    )
    _save_draft(
        db,
        template,
        draft,
        operation_type="DRAFT_MATERIAL_CREATE",
        object_name=name,
        detail={"scenario_code": scenario_code, "material_key": material_key},
    )
    return draft


def _draft_material_or_404(db: Session, draft_id: int, material_key: str) -> ScenarioDraftMaterial:
    item = next(iter([item for item in config_store.rows(db, ScenarioDraftMaterial, draft_id) if (item.material_key == material_key)]), None)
    if not item:
        raise BusinessError("DRAFT_MATERIAL_NOT_FOUND", "材料配置不存在", status_code=404, details={"material_key": material_key})
    return item


def update_draft_material(db: Session, scenario_code: str, material_key: str, payload: dict[str, Any]) -> ScenarioDraft:
    """修改草稿中的材料位（只覆盖提交的字段）。"""
    template, draft = _locked_draft(db, scenario_code)
    item = _draft_material_or_404(db, draft.id, material_key)
    if "name" in payload and payload["name"] is not None:
        name = str(payload["name"]).strip()
        if not name:
            raise BusinessError("MATERIAL_NAME_REQUIRED", "材料名称不能为空", status_code=400)
        item.name = name
    if "required" in payload and payload["required"] is not None:
        item.required = bool(payload["required"])
    if "description" in payload and payload["description"] is not None:
        item.description = str(payload["description"])
    if "source_ref" in payload:
        source_ref = _normalized_source_ref(payload.get("source_ref"))
        item.source_ref = source_ref
    _save_draft(
        db,
        template,
        draft,
        operation_type="DRAFT_MATERIAL_UPDATE",
        object_name=item.name,
        detail={"scenario_code": scenario_code, "material_key": material_key},
    )
    return draft


def delete_draft_material(db: Session, scenario_code: str, material_key: str) -> ScenarioDraft:
    """删除草稿中的材料位；仍被字段或规则引用时 409，不级联删除。"""
    template, draft = _locked_draft(db, scenario_code)
    item = _draft_material_or_404(db, draft.id, material_key)
    fields = [item for item in config_store.rows(db, ScenarioDraftField, draft.id) if (item.material_key == material_key)]
    rules = config_store.rows(db, ScenarioDraftRule, draft.id)
    referencing_rules = [
        rule.name
        for rule in rules
        if material_key in (rule.rule_json or {}).get("materials", [])
        or any(entry.get("material_key") == material_key for entry in (rule.rule_json or {}).get("inputs", []))
    ]
    if fields or referencing_rules:
        raise BusinessError(
            "MATERIAL_REFERENCED",
            "该材料仍被以下字段或规则引用，请先在对应配置中移除后再删除",
            status_code=409,
            details={
                "material_name": item.name,
                "field_keys": [f"{field.name}（{field.field_key}）" for field in fields],
                "rule_keys": referencing_rules,
            },
        )
    config_store.delete(item)
    _save_draft(
        db,
        template,
        draft,
        operation_type="DRAFT_MATERIAL_DELETE",
        object_name=item.name,
        detail={"scenario_code": scenario_code, "material_key": material_key},
    )
    return draft
