"""场景草稿报告章节及分组字段配置。"""

from typing import Any
from sqlalchemy.orm import Session
from ...repositories.shared import configuration as config_store
from ...domain.report_data import report_data_refs
from ...common.exceptions import BusinessError
from ...entity import ScenarioDraft, ScenarioDraftField, ScenarioDraftMaterial, ScenarioDraftSection
from ...domain.identity import stable_key
from ...repositories.scenario.draft import next_sort_no
from .common import _item_ref
from .draft import _locked_draft, _save_draft


def _section_group_fields(db: Session, draft_id: int, payload: dict[str, Any]) -> dict[str, Any]:
    """报告章节的分组属性：报告编号、报告名称、材料范围、输出格式、结果排序、
    章节顺序、内容类型与业务配置状态。

    只保存用户实际填写的属性，未声明的分组属性保持为空。
    """
    materials = config_store.rows(db, ScenarioDraftMaterial, draft_id)
    known_materials = {item.material_key for item in materials}
    result: dict[str, Any] = {}
    if "report_code" in payload:
        value = payload.get("report_code")
        result["report_code"] = str(value).strip() or None if value is not None else None
    if "report_name" in payload:
        value = payload.get("report_name")
        result["report_name"] = str(value).strip() or None if value is not None else None
    if "material_keys" in payload:
        keys: list[str] = []
        for key in payload.get("material_keys") or []:
            key = str(key)
            if key not in known_materials:
                raise BusinessError(
                    "SECTION_MATERIAL_MISSING",
                    "报告章节的材料范围引用了不存在的材料",
                    status_code=400,
                    details={"material_key": key},
                )
            if key not in keys:
                keys.append(key)
        result["material_keys"] = keys
    if "formats" in payload:
        result["formats"] = [str(item) for item in (payload.get("formats") or [])]
    if "section_order" in payload:
        value = payload.get("section_order")
        result["section_order"] = int(value) if value is not None else None
    if "content_type" in payload:
        value = payload.get("content_type")
        result["content_type"] = str(value).strip() or None if value is not None else None
    if "result_sort" in payload:
        value = payload.get("result_sort")
        result["result_sort"] = str(value).strip() or None if value is not None else None
    if "risk_level_order" in payload:
        result["risk_level_order"] = list(payload.get("risk_level_order") or [])
    if "config_status" in payload:
        value = payload.get("config_status")
        result["config_status"] = str(value).strip() or None if value is not None else None
    return result


def create_draft_section(db: Session, scenario_code: str, payload: dict[str, Any]) -> ScenarioDraft:
    """在草稿中新增报告章节。已保存到草稿，发布后才供新方案使用。"""
    template, draft = _locked_draft(db, scenario_code)
    title = str(payload.get("title") or "").strip()
    if not title:
        raise BusinessError("SECTION_TITLE_REQUIRED", "章节名称不能为空", status_code=400)
    data_refs = report_data_refs(payload)
    known_fields = {item.field_key for item in config_store.rows(db, ScenarioDraftField, draft.id)}
    field_keys = [ref for ref in data_refs if ref in known_fields]
    section_key = stable_key("sec", scenario_code, _item_ref())
    config_store.add(
        db, ScenarioDraftSection(
            draft_id=draft.id,
            section_key=section_key,
            title=title,
            content_instruction=str(payload.get("content_instruction") or ""),
            field_keys=field_keys,
            data_refs=data_refs,
            sort_no=next_sort_no(db, ScenarioDraftSection, draft.id),
            **_section_group_fields(db, draft.id, payload),
        )
    )
    _save_draft(
        db,
        template,
        draft,
        operation_type="DRAFT_SECTION_CREATE",
        object_name=title,
        detail={"scenario_code": scenario_code, "section_key": section_key},
    )
    return draft


def update_draft_section(db: Session, scenario_code: str, section_key: str, payload: dict[str, Any]) -> ScenarioDraft:
    """修改草稿中的报告章节。"""
    template, draft = _locked_draft(db, scenario_code)
    item = next(iter([item for item in config_store.rows(db, ScenarioDraftSection, draft.id) if (item.section_key == section_key)]), None)
    if not item:
        raise BusinessError("DRAFT_SECTION_NOT_FOUND", "报告章节不存在", status_code=404, details={"section": section_key})
    if "title" in payload and payload["title"] is not None:
        title = str(payload["title"]).strip()
        if not title:
            raise BusinessError("SECTION_TITLE_REQUIRED", "章节名称不能为空", status_code=400)
        item.title = title
    if "content_instruction" in payload and payload["content_instruction"] is not None:
        item.content_instruction = str(payload["content_instruction"])
    if "data_refs" in payload or "field_keys" in payload:
        item.data_refs = report_data_refs(payload)
        known_fields = {field.field_key for field in config_store.rows(db, ScenarioDraftField, draft.id)}
        item.field_keys = [ref for ref in item.data_refs if ref in known_fields]
    # 报告分组属性：本次提交未覆盖的键保持原值，避免页面保存一次就丢掉报告编号、
    # 材料范围、输出格式或结果排序。
    for key, value in _section_group_fields(db, draft.id, payload).items():
        setattr(item, key, value)
    _save_draft(
        db,
        template,
        draft,
        operation_type="DRAFT_SECTION_UPDATE",
        object_name=item.title,
        detail={"scenario_code": scenario_code, "section_key": section_key},
    )
    return draft


def delete_draft_section(db: Session, scenario_code: str, section_key: str) -> ScenarioDraft:
    """删除草稿中的报告章节。"""
    template, draft = _locked_draft(db, scenario_code)
    item = next(iter([item for item in config_store.rows(db, ScenarioDraftSection, draft.id) if (item.section_key == section_key)]), None)
    if not item:
        raise BusinessError("DRAFT_SECTION_NOT_FOUND", "报告章节不存在", status_code=404, details={"section": section_key})
    config_store.delete(item)
    _save_draft(
        db,
        template,
        draft,
        operation_type="DRAFT_SECTION_DELETE",
        object_name=item.title,
        detail={"scenario_code": scenario_code, "section_key": section_key},
    )
    return draft
