"""场景草稿材料配置及依赖检查。"""

from typing import Any

from sqlalchemy.orm import Session

from app.common.exceptions import BusinessError
from app.domain.shared.identity import stable_key
from app.modules.scenario.draft.model import (
    ScenarioDraft,
    ScenarioDraftField,
    ScenarioDraftMaterial,
    ScenarioDraftRule,
    ScenarioDraftSection,
)
from app.modules.scenario.draft.service.draft import ScenarioDraftService
from app.modules.scenario.template.service.common import ScenarioTemplateCommonService
from app.services.shared import configuration as config_store


class ScenarioDraftMaterialsService:
    """本实体的服务方法；db 由构造时注入。"""

    def __init__(self, db: Session | None = None) -> None:
        self.db = db

    def create_draft_material(self, scenario_code: str, payload: dict[str, Any]) -> ScenarioDraft:
        """在草稿中新增材料位。已保存到草稿，发布后才供新方案使用。"""
        template, draft = ScenarioDraftService(self.db)._locked_draft( scenario_code)
        name = str(payload.get("name") or "").strip()
        if not name:
            raise BusinessError("MATERIAL_NAME_REQUIRED", "材料名称不能为空", status_code=400)
        source_ref = ScenarioTemplateCommonService()._normalized_source_ref(payload.get("source_ref"))
        material_key = stable_key("mat", scenario_code, ScenarioTemplateCommonService()._item_ref())
        config_store.add(
            self.db, ScenarioDraftMaterial(
                draft_id=draft.id,
                material_key=material_key,
                name=name,
                required=bool(payload.get("required", True)),
                description=str(payload.get("description") or ""),
                source_ref=source_ref,
                sort_no=ScenarioDraftService(self.db).next_sort_no( ScenarioDraftMaterial, draft.id),
            )
        )
        ScenarioDraftService(self.db)._save_draft(
            
            template,
            draft,
            operation_type="DRAFT_MATERIAL_CREATE",
            object_name=name,
            detail={"scenario_code": scenario_code, "material_key": material_key},
        )
        return draft


    def _draft_material_or_404(self, draft_id: int, material_key: str) -> config_store.ConfigItem:
        item = next(iter([item for item in config_store.rows(self.db, ScenarioDraftMaterial, draft_id) if (item.material_key == material_key)]), None)
        if not item:
            raise BusinessError("DRAFT_MATERIAL_NOT_FOUND", "材料配置不存在", status_code=404, details={"material_key": material_key})
        return item


    def update_draft_material(self, scenario_code: str, material_key: str, payload: dict[str, Any]) -> ScenarioDraft:
        """修改草稿中的材料位（只覆盖提交的字段）。"""
        template, draft = ScenarioDraftService(self.db)._locked_draft( scenario_code)
        item = self._draft_material_or_404( draft.id, material_key)
        if "name" in payload and payload["name"] is not None:
            name = str(payload["name"]).strip()
            if not name:
                raise BusinessError("MATERIAL_NAME_REQUIRED", "材料名称不能为空", status_code=400)
            config_store.update_material(item, name=name)
        if "required" in payload and payload["required"] is not None:
            config_store.update_material(item, required=bool(payload["required"]))
        if "description" in payload and payload["description"] is not None:
            config_store.update_material(item, description=str(payload["description"]))
        if "source_ref" in payload:
            source_ref = ScenarioTemplateCommonService()._normalized_source_ref(payload.get("source_ref"))
            config_store.update_material(item, source_ref=source_ref)
        ScenarioDraftService(self.db)._save_draft(
            
            template,
            draft,
            operation_type="DRAFT_MATERIAL_UPDATE",
            object_name=item.name,
            detail={"scenario_code": scenario_code, "material_key": material_key},
        )
        return draft


    def delete_draft_material(self, scenario_code: str, material_key: str) -> ScenarioDraft:
        """删除草稿中的材料位；仍被字段或规则引用时 409，不级联删除。"""
        template, draft = ScenarioDraftService(self.db)._locked_draft( scenario_code)
        item = self._draft_material_or_404( draft.id, material_key)
        fields = [item for item in config_store.rows(self.db, ScenarioDraftField, draft.id) if (item.material_key == material_key)]
        rules = config_store.rows(self.db, ScenarioDraftRule, draft.id)
        referencing_rules = [
            rule.name
            for rule in rules
            if material_key in (rule.rule_json or {}).get("materials", [])
            or any(entry.get("material_key") == material_key for entry in (rule.rule_json or {}).get("inputs", []))
        ]
        # 报告章节的材料范围也是真实引用：只按字段与规则检查会留下悬空材料键，
        # 编译后的报告规则随之出现材料依赖缺失。
        sections = config_store.rows(self.db, ScenarioDraftSection, draft.id)
        referencing_sections = [section.title for section in sections if material_key in (section.material_keys or [])]
        if fields or referencing_rules or referencing_sections:
            raise BusinessError(
                "MATERIAL_REFERENCED",
                "该材料仍被以下字段、规则或报告章节引用，请先在对应配置中移除后再删除",
                status_code=409,
                details={
                    "material_name": item.name,
                    "field_keys": [f"{field.name}（{field.field_key}）" for field in fields],
                    "rule_keys": referencing_rules,
                    "section_names": referencing_sections,
                },
            )
        config_store.delete(item)
        ScenarioDraftService(self.db)._save_draft(
            
            template,
            draft,
            operation_type="DRAFT_MATERIAL_DELETE",
            object_name=item.name,
            detail={"scenario_code": scenario_code, "material_key": material_key},
        )
        return draft
