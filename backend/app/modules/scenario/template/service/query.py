"""场景管理列表、选项与详情查询：SQL 在 crud，本层负责组装视图。"""

from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from app.common.pagination import PageResult, page_result
from app.domain import manifest as manifest_module
from app.modules.scenario.draft.model import ScenarioDraft
from app.modules.scenario.template.service import crud
from app.modules.scenario.template.service.common import DRAFT_SOURCE_LABELS
from app.modules.scenario.template.service.lookup import ScenarioTemplateLookupService


class ScenarioTemplateQueryService:
    """本实体的服务方法；db 由构造时注入。"""

    def __init__(self, db: Session | None = None) -> None:
        self.db = db

    def scenario_detail(self, scenario_code: str) -> dict[str, Any]:
        """场景详情：基本资料、当前发布版本号与配置状态。"""
        template = ScenarioTemplateLookupService(self.db).template_or_404( scenario_code)
        draft = crud.ScenarioTemplateCRUD(self.db).draft_for_code( scenario_code)
        plan_count = crud.ScenarioTemplateCRUD(self.db).count_plans( scenario_code)
        return {
            "scenario_code": template.scenario_code,
            "schema_key": template.schema_key,
            "schema_version": template.schema_version,
            "schema_json": template.schema_json,
            "scenario_name": template.scenario_name,
            "category": template.category,
            "description": template.description,
            "enabled": template.enabled,
            "latest_version_no": template.latest_version_no,
            "created_at": template.created_at,
            "updated_at": template.updated_at,
            "plan_count": plan_count,
            "draft": self._draft_view(draft),
        }


    def list_scenarios_page(self, *,
        name: str | None,
        category: str | None,
        enabled: bool | None,
        published_from: datetime | None,
        published_to: datetime | None,
        page: int,
        page_size: int,
    ) -> PageResult[dict[str, Any]]:
        """场景分页列表（真实后端筛选与 total）。"""
        templates, total = crud.ScenarioTemplateCRUD(self.db).templates_page(
            name=name,
            category=category,
            enabled=enabled,
            published_from=published_from,
            published_to=published_to,
            page=page,
            page_size=page_size,
        )

        codes = [item.scenario_code for item in templates]
        latest_versions = {
            scenario_code: (version_no, published_at, version_id)
            for scenario_code, version_no, published_at, version_id in crud.ScenarioTemplateCRUD(self.db).latest_version_rows( codes)
        }
        draft_by_code = {draft.scenario_code: draft for draft in crud.ScenarioTemplateCRUD(self.db).drafts_for_codes( codes)}
        draft_counts = {
            scenario_code: {
                "material": len(draft.config_json["materials"]),
                "field": len(draft.config_json["fields"]),
                "rule": len(draft.config_json["rules"]),
            }
            for scenario_code, draft in draft_by_code.items()
        }
        # 有草稿时统计来自草稿；只有没有草稿的场景才需要读发布 manifest。
        manifest_ids = [latest[2] for code, latest in latest_versions.items() if code not in draft_by_code]
        published_manifests = crud.ScenarioTemplateCRUD(self.db).manifests_for_ids( manifest_ids)

        items = []
        for template in templates:
            counts = draft_counts.get(template.scenario_code, {"material": 0, "field": 0, "rule": 0})
            latest = latest_versions.get(template.scenario_code)
            draft = draft_by_code.get(template.scenario_code)
            if draft is None and latest is not None:
                manifest = published_manifests[latest[2]]
                counts = {
                    "material": len(manifest["materials"]),
                    "field": len(manifest["fields"]),
                    "rule": sum(
                        not manifest_module.is_system_rule(rule) and not rule.get("calculation_ref")
                        for rule in manifest["rules"]
                    ),
                }
            items.append(
                {
                    "scenario_code": template.scenario_code,
                    "scenario_name": template.scenario_name,
                    "category": template.category,
                    "description": template.description,
                    "enabled": template.enabled,
                    "published_version_no": latest[0] if latest else None,
                    "published_at": latest[1] if latest else None,
                    "latest_version_no": template.latest_version_no,
                    "material_count": counts["material"],
                    "field_count": counts["field"],
                    "rule_count": counts["rule"],
                    "configuration_kind": "DRAFT" if draft is not None or latest is None else "PUBLISHED",
                    "draft_revision": draft.revision_no if draft else None,
                    "draft_source": draft.source if draft else None,
                    "draft_base_version_no": draft.base_version_no if draft else None,
                    # 草稿存在与否单独返回；无编译错误不表示草稿已发布。
                    "sync_error": None,
                    "updated_at": template.updated_at,
                }
            )
        return page_result(items, total, page, page_size)


    def list_admin_scenario_options(self) -> list[dict[str, Any]]:
        """管理员用的场景下拉：未删除的全部场景及其实时状态。

        导入页与场景管理页需要在场景发布之前就选中它（先建场景、再上传规则库），
        因此这里不能按「已发布」过滤——那会让管理员无法在新建场景下上传。
        """
        latest = crud.ScenarioTemplateCRUD(self.db).latest_version_numbers()
        return [
            {
                "scenario_code": item.scenario_code,
                "scenario_name": item.scenario_name,
                "category": item.category,
                "enabled": item.enabled,
                "latest_version_no": latest.get(item.scenario_code, 0),
                "available": item.enabled and item.scenario_code in latest,
            }
            for item in crud.ScenarioTemplateCRUD(self.db).all_templates()
        ]


    def list_scenario_options(self) -> list[dict[str, Any]]:
        """业务用户可用的场景下拉选项（方案、任务等选择器）。

        只列出「未删除 + 已启用 + 已有已发布版本」的场景：草稿、未发布、停用或已删除的
        场景不能用于创建方案。把不可用的场景也列出来、再在创建时报错，
        会让业务用户在一堆选不动的选项里试错。

        `available` 恒为 true；保留该字段是为了让页面无需自己推断，
        并让接口在将来放宽条件时不必改契约。
        """
        latest = crud.ScenarioTemplateCRUD(self.db).latest_version_numbers()
        return [
            {
                "scenario_code": item.scenario_code,
                "scenario_name": item.scenario_name,
                "category": item.category,
                "enabled": item.enabled,
                "latest_version_no": latest[item.scenario_code],
                "available": True,
            }
            for item in crud.ScenarioTemplateCRUD(self.db).all_templates()
            if item.enabled and item.scenario_code in latest
        ]


    def _draft_view(self, draft: ScenarioDraft | None) -> dict[str, Any] | None:
        if draft is None:
            return None
        return {
            "revision_no": draft.revision_no,
            "source": draft.source,
            "source_label": DRAFT_SOURCE_LABELS.get(draft.source, draft.source),
            "base_version_no": draft.base_version_no,
            # 按当前编译逻辑显示发布条件，不能用历史错误锁住发布按钮。
            "sync_error": None,
            "updated_at": draft.updated_at,
        }
