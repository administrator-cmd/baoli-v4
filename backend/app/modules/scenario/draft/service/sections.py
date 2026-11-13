"""场景草稿报告章节及分组字段配置。"""

from typing import Any

from sqlalchemy.orm import Session

from app.common.exceptions import BusinessError
from app.domain.configuration.config_changes import SectionChanges
from app.domain.shared.identity import stable_key
from app.domain.configuration.report_data import report_data_refs
from app.modules.scenario.draft.model import (
    ScenarioDraft,
    ScenarioDraftField,
    ScenarioDraftMaterial,
    ScenarioDraftSection,
)
from app.modules.scenario.draft.service.draft import ScenarioDraftService
from app.modules.scenario.template.service.common import ScenarioTemplateCommonService
from app.services.shared import configuration as config_store


class ScenarioDraftSectionsService:
    """本实体的服务方法；db 由构造时注入。"""

    def __init__(self, db: Session | None = None) -> None:
        self.db = db

    def _section_group_fields(self, draft_id: int, payload: dict[str, Any]) -> SectionChanges:
        """报告章节的分组属性：报告编号、报告名称、材料范围、输出格式、结果排序、
        章节顺序、内容类型与业务配置状态。

        只保存用户实际填写的属性，未声明的分组属性保持为空。
        """
        materials = config_store.rows(self.db, ScenarioDraftMaterial, draft_id)
        known_materials = {item.material_key for item in materials}
        result: SectionChanges = {}
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


    def create_draft_section(self, scenario_code: str, payload: dict[str, Any]) -> ScenarioDraft:
        """在草稿中新增报告章节。已保存到草稿，发布后才供新方案使用。"""
        template, draft = ScenarioDraftService(self.db)._locked_draft( scenario_code)
        title = str(payload.get("title") or "").strip()
        if not title:
            raise BusinessError("SECTION_TITLE_REQUIRED", "章节名称不能为空", status_code=400)
        data_refs = report_data_refs(payload)
        known_fields = {item.field_key for item in config_store.rows(self.db, ScenarioDraftField, draft.id)}
        field_keys = [ref for ref in data_refs if ref in known_fields]
        section_key = stable_key("sec", scenario_code, ScenarioTemplateCommonService()._item_ref())
        config_store.add(
            self.db, ScenarioDraftSection(
                draft_id=draft.id,
                section_key=section_key,
                title=title,
                content_instruction=str(payload.get("content_instruction") or ""),
                field_keys=field_keys,
                data_refs=data_refs,
                sort_no=ScenarioDraftService(self.db).next_sort_no( ScenarioDraftSection, draft.id),
                **self._section_group_fields( draft.id, payload),
            )
        )
        ScenarioDraftService(self.db)._save_draft(
            
            template,
            draft,
            operation_type="DRAFT_SECTION_CREATE",
            object_name=title,
            detail={"scenario_code": scenario_code, "section_key": section_key},
        )
        return draft


    def update_draft_section(self, scenario_code: str, section_key: str, payload: dict[str, Any]) -> ScenarioDraft:
        """修改草稿中的报告章节。"""
        template, draft = ScenarioDraftService(self.db)._locked_draft( scenario_code)
        item = next(iter([item for item in config_store.rows(self.db, ScenarioDraftSection, draft.id) if (item.section_key == section_key)]), None)
        if not item:
            raise BusinessError("DRAFT_SECTION_NOT_FOUND", "报告章节不存在", status_code=404, details={"section": section_key})
        if "title" in payload and payload["title"] is not None:
            title = str(payload["title"]).strip()
            if not title:
                raise BusinessError("SECTION_TITLE_REQUIRED", "章节名称不能为空", status_code=400)
            config_store.update_section(item, title=title)
        if "content_instruction" in payload and payload["content_instruction"] is not None:
            config_store.update_section(item, content_instruction=str(payload["content_instruction"]))
        if "data_refs" in payload or "field_keys" in payload:
            config_store.update_section(item, data_refs=report_data_refs(payload))
            known_fields = {field.field_key for field in config_store.rows(self.db, ScenarioDraftField, draft.id)}
            config_store.update_section(item, field_keys=[ref for ref in item.data_refs if ref in known_fields])
        # 报告分组属性：本次提交未覆盖的键保持原值，避免页面保存一次就丢掉报告编号、
        # 材料范围、输出格式或结果排序。
        config_store.update_section(item, **self._section_group_fields(draft.id, payload))
        ScenarioDraftService(self.db)._save_draft(
            
            template,
            draft,
            operation_type="DRAFT_SECTION_UPDATE",
            object_name=item.title,
            detail={"scenario_code": scenario_code, "section_key": section_key},
        )
        return draft


    def delete_draft_section(self, scenario_code: str, section_key: str) -> ScenarioDraft:
        """删除草稿中的报告章节。"""
        template, draft = ScenarioDraftService(self.db)._locked_draft( scenario_code)
        item = next(iter([item for item in config_store.rows(self.db, ScenarioDraftSection, draft.id) if (item.section_key == section_key)]), None)
        if not item:
            raise BusinessError("DRAFT_SECTION_NOT_FOUND", "报告章节不存在", status_code=404, details={"section": section_key})
        config_store.delete(item)
        ScenarioDraftService(self.db)._save_draft(
            
            template,
            draft,
            operation_type="DRAFT_SECTION_DELETE",
            object_name=item.title,
            detail={"scenario_code": scenario_code, "section_key": section_key},
        )
        return draft
