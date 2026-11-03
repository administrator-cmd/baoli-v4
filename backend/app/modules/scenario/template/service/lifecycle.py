"""场景创建、属性维护、启停与删除。"""

from typing import Any

from sqlalchemy.orm import Session

from app.common.exceptions import BusinessError
from app.domain import scenario_schema
from app.domain.identity import public_no
from app.modules.scenario.draft.crud import ScenarioDraftCRUD
from app.modules.scenario.draft.model import ScenarioDraft
from app.modules.scenario.draft.service.draft import ScenarioDraftService
from app.modules.scenario.template.model import ScenarioTemplate
from app.modules.scenario.template.service.crud import ScenarioTemplateCRUD
from app.modules.scenario.template.service.lookup import ScenarioTemplateLookupService
from app.modules.system.operation.service.operation_log import OperationLogService
from app.modules.system.operation.service.operation_types import OBJECT_SCENARIO
from app.services.shared import configuration as config_store
from app.services.shared.import_cleanup import (
    delete_import_job_entities,
    discard_staged_trees,
    restore_staged_trees,
)


class ScenarioTemplateLifecycleService:
    """本实体的服务方法；db 由构造时注入。"""

    def __init__(self, db: Session | None = None) -> None:
        self.db = db

    def create_scenario(self, payload: dict[str, Any]) -> ScenarioTemplate:
        """新建场景模板，并建立含规则执行技术容器的空草稿。"""
        name = str(payload.get("scenario_name") or "").strip()
        if not name:
            raise BusinessError("SCENARIO_NAME_REQUIRED", "场景名称不能为空", status_code=400)
        schema = scenario_schema.normalize_schema(payload.get("schema_json"))
        scenario_code = public_no("SCN")
        template = ScenarioTemplate(
            scenario_code=scenario_code,
            scenario_name=name,
            category=(str(payload.get("category")).strip() or None) if payload.get("category") else None,
            description=(str(payload.get("description")).strip() or None) if payload.get("description") else None,
            enabled=bool(payload.get("enabled", True)),
            latest_version_no=0,
            schema_key=schema["schema_key"],
            schema_version=schema["schema_version"],
            schema_json=schema,
        )
        ScenarioTemplateCRUD(self.db).add(template)
        ScenarioDraftCRUD(self.db).flush()
        draft = ScenarioDraft(
            scenario_code=scenario_code,
            revision_no=0,
            source="FORM",
            base_version_no=None,
            schema_key=schema["schema_key"],
            schema_version=schema["schema_version"],
            schema_json=schema,
        )
        ScenarioDraftCRUD(self.db).add(draft)
        ScenarioDraftCRUD(self.db).flush()
        # 只建立规则执行记录所需的技术容器，不预置可编排的业务阶段。
        for row in ScenarioDraftService()._default_draft_nodes(draft.id):
            config_store.add(self.db, row)
        ScenarioDraftCRUD(self.db).flush()
        # 创建仅保存空草稿；配置编译由读取派生项或显式发布时执行。
        draft.sync_error = None
        OperationLogService(self.db).record_operation(
            
            object_type=OBJECT_SCENARIO,
            object_no=scenario_code,
            object_name=name,
            operation_type="CREATE",
            detail={"scenario_code": scenario_code},
        )
        ScenarioDraftCRUD(self.db).commit()
        ScenarioTemplateCRUD(self.db).refresh(template)
        return template


    def update_scenario(self, scenario_code: str, payload: dict[str, Any]) -> ScenarioTemplate:
        """编辑场景基本资料（只覆盖提交的字段）。

        场景名称会进入下一次发布的版本。名称变化时锁定草稿并递增修订号，
        使旧的 expected_revision 不能把另一位管理员改过的名称发布出去。
        保存只更新场景基本资料与相关草稿；生成生效版本必须由管理员显式发布。
        """
        template = ScenarioTemplateLookupService(self.db).template_or_404( scenario_code, lock=True)
        config_changed = False
        if "scenario_name" in payload and payload["scenario_name"] is not None:
            name = str(payload["scenario_name"]).strip()
            if not name:
                raise BusinessError("SCENARIO_NAME_REQUIRED", "场景名称不能为空", status_code=400)
            if name != template.scenario_name:
                template.scenario_name = name
                config_changed = True
        if "schema_json" in payload:
            schema = scenario_schema.normalize_schema(payload["schema_json"])
            current = scenario_schema.normalize_schema(template.schema_json)
            if schema != current:
                template.schema_key = schema["schema_key"]
                template.schema_version = schema["schema_version"]
                template.schema_json = schema
                config_changed = True
        if "category" in payload:
            category = payload.get("category")
            template.category = (str(category).strip() or None) if category is not None else None
        if "description" in payload:
            description = payload.get("description")
            template.description = (str(description).strip() or None) if description is not None else None
        if "enabled" in payload and payload["enabled"] is not None:
            template.enabled = bool(payload["enabled"])
        if config_changed:
            draft = ScenarioDraftCRUD(self.db).by_code(template.scenario_code, lock=True)
            if draft is None:
                draft = ScenarioDraftService(self.db).ensure_draft( template)
                ScenarioDraftCRUD(self.db).flush()
                draft = ScenarioDraftCRUD(self.db).by_code(template.scenario_code, lock=True)
            draft.schema_key = template.schema_key
            draft.schema_version = template.schema_version
            draft.schema_json = template.schema_json
            ScenarioDraftService()._bump_draft(draft)
            ScenarioDraftCRUD(self.db).flush()
        OperationLogService(self.db).record_operation(
            
            object_type=OBJECT_SCENARIO,
            object_no=scenario_code,
            object_name=template.scenario_name,
            operation_type="UPDATE",
            detail={"scenario_code": scenario_code},
        )
        if config_changed:
            ScenarioDraftService(self.db)._commit_config_change( template, draft)
        else:
            ScenarioDraftCRUD(self.db).commit()
        ScenarioTemplateCRUD(self.db).refresh(template)
        return template


    def set_scenario_enabled(self, scenario_code: str, enabled: bool) -> ScenarioTemplate:
        """启用 / 停用场景。"""
        template = ScenarioTemplateLookupService(self.db).template_or_404( scenario_code, lock=True)
        template.enabled = enabled
        OperationLogService(self.db).record_operation(
            
            object_type=OBJECT_SCENARIO,
            object_no=scenario_code,
            object_name=template.scenario_name,
            operation_type="ENABLE" if enabled else "DISABLE",
            detail={"scenario_code": scenario_code},
        )
        ScenarioDraftCRUD(self.db).commit()
        ScenarioTemplateCRUD(self.db).refresh(template)
        return template


    def delete_scenario(self, scenario_code: str) -> None:
        """实际删除场景、草稿及关联导入产物；独立方案、任务与已发布执行快照不级联删除。"""
        template = ScenarioTemplateLookupService(self.db).template_or_404( scenario_code, lock=True)
        draft = ScenarioDraftCRUD(self.db).by_code(scenario_code, lock=True)
        import_jobs = ScenarioDraftCRUD(self.db).import_jobs_by_scenario(scenario_code, lock=True)
        OperationLogService(self.db).record_operation(
            
            object_type=OBJECT_SCENARIO,
            object_no=scenario_code,
            object_name=template.scenario_name,
            operation_type="DELETE",
            permanent=True,
            detail={
                "scenario_code": scenario_code,
                "deletion_mode": "PHYSICAL",
                "deleted_import_job_nos": [job.job_no for job in import_jobs],
            },
        )
        # 导入作业、后台转换和 LLM 原始响应属于该场景的临时验证数据，随场景删除。
        # 活跃转换会在共享清理函数中明确拒绝，避免后台线程在删除后继续写回。
        # 文件目录只改名暂存，提交成功后再清理，提交失败原样改回。
        staged = []
        try:
            staged = delete_import_job_entities(self.db, import_jobs)
            if draft is not None:
                ScenarioDraftCRUD(self.db).delete(draft)
            ScenarioTemplateCRUD(self.db).delete(template)
            ScenarioDraftCRUD(self.db).commit()
        except Exception:
            restore_staged_trees(staged)
            ScenarioDraftCRUD(self.db).rollback()
            raise
        discard_staged_trees(staged, object_no=scenario_code)
