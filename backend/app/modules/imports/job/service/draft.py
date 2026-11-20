"""将规则库转换结果应用到场景草稿。"""

from sqlalchemy.orm import Session

from app.common.exceptions import BusinessError
from app.domain.configuration import scenario_schema
from app.domain.configuration.manifest import draft_config_of_import
from app.modules.imports.job import crud
from app.modules.imports.job.model import ImportJob
from app.modules.imports.job.service.lookup import ImportsJobLookupService
from app.modules.scenario.draft.crud import ScenarioDraftCRUD
from app.modules.scenario.template.service.crud import ScenarioTemplateCRUD
from app.modules.scenario.template.service.lookup import ScenarioTemplateLookupService
from app.modules.system.operation.service.background import OperationBackgroundService
from app.modules.system.operation.service.operation_log import OperationLogService
from app.modules.system.operation.service.operation_types import OBJECT_IMPORT_JOB


class ImportsJobDraftService:
    """本实体的服务方法；db 由构造时注入。"""

    def __init__(self, db: Session | None = None) -> None:
        self.db = db

    def apply_import_to_draft(self, job_no: str, *, expected_job_revision: int, expected_scene_revision: int | None
    ) -> ImportJob:
        """把解析结果写入场景草稿，不生成或发布场景版本。

        不创建场景：场景身份以上传时记录的 scenario_code 为准，不从文件名、Sheet 名或表头另找。
        写入替换材料、字段、规则、报告与计算定义。
        写入前核对草稿修订，执行时由 LLM 按生效规则处理。
        """
        from app.modules.scenario.draft.service.draft import ScenarioDraftService
        from app.modules.system.operation.service.operation_types import OBJECT_SCENARIO

        job = ImportsJobLookupService(self.db)._job_or_404( job_no, lock=True)
        if job.status == "APPLIED":
            return job
        if job.draft_revision != expected_job_revision:
            raise BusinessError(
                "IMPORT_DRAFT_REVISION_CONFLICT",
                "导入候选在本次确认期间已修改，请重新核对后再写入场景草稿",
                status_code=409,
                details={"job_no": job_no, "expected": expected_job_revision, "actual": job.draft_revision},
            )
        existing_version_id = ScenarioTemplateCRUD(self.db).version_id_by_import_job(job.id)
        if existing_version_id is not None:
            raise BusinessError(
                "IMPORT_ALREADY_PUBLISHED",
                "该导入作业已经产生过场景版本，不能再改写场景草稿",
                status_code=409,
                details={"job_no": job.job_no},
            )
        OperationBackgroundService(self.db).assert_no_active_operation(OBJECT_IMPORT_JOB, job_no)
        if not job.scenario_code:
            raise BusinessError(
                "IMPORT_SCENARIO_REQUIRED",
                "导入作业没有所属场景，不能写入草稿。请先创建场景，再在该场景下重新上传",
                status_code=409,
                details={"job_no": job.job_no},
            )
        template = ScenarioTemplateLookupService(self.db).template_or_404( job.scenario_code, lock=True)
        job_schema = ImportsJobLookupService()._job_schema(job)
        template_schema = scenario_schema.normalize_schema(template.schema_json)
        if job_schema != template_schema or job.schema_key != template.schema_key or job.schema_version != template.schema_version:
            raise BusinessError(
                "IMPORT_SCENARIO_SCHEMA_CHANGED",
                "场景导入协议已在上传后改变，请按当前场景重新导入规则库",
                status_code=409,
                details={"job_no": job.job_no, "scenario_code": job.scenario_code},
            )
        if job.status not in {"PROPOSED"}:
            raise BusinessError("IMPORT_STATUS_NOT_READY", "导入作业尚未成功完成规则转换", status_code=409)
        if not job.proposals_json:
            raise BusinessError("PROPOSALS_MISSING", "规则尚未转换", status_code=409)
        proposals = ImportsJobLookupService()._saved_proposals(job)
        if proposals.get("dependency_questions"):
            raise BusinessError("IMPORT_DEPENDENCIES_PENDING", "请先在解析结果中确认规则引用，再写入草稿", status_code=409)
        draft = ScenarioDraftCRUD(self.db).by_code(template.scenario_code)
        actual_revision = draft.revision_no if draft is not None else None
        if actual_revision != expected_scene_revision:
            raise BusinessError(
                "SCENARIO_DRAFT_REVISION_CONFLICT", "场景草稿在本次确认期间已修改，请重新核对后再写入",
                status_code=409,
                details={"scenario_code": template.scenario_code, "expected": expected_scene_revision, "actual": actual_revision},
            )
        draft = ScenarioDraftService(self.db).ensure_draft( template)
        if (
            draft.schema_json != job_schema
            or draft.schema_key != job.schema_key
            or draft.schema_version != job.schema_version
        ):
            raise BusinessError(
                "IMPORT_DRAFT_SCHEMA_MISMATCH",
                "场景草稿的导入协议与当前作业不一致，请重新导入规则库",
                status_code=409,
                details={"job_no": job.job_no, "scenario_code": job.scenario_code},
            )
        config = draft_config_of_import(
            proposals=proposals,
            namespace=template.scenario_code,
        )
        ScenarioDraftService(self.db).load_import_config_into_draft( draft, config, profile_version=job.profile_version)
        draft.config_json = {**draft.config_json, "import_proposals": job.proposals_json,
                             "import_job_no": job.job_no}
        draft.source = "EXCEL"
        latest = ScenarioTemplateLookupService(self.db).find_latest_published_version( template.scenario_code)
        if latest is not None and draft.base_version_no is None:
            draft.base_version_no = latest.version_no
        ScenarioDraftService()._bump_draft(draft)
        job.status = "APPLIED"
        OperationLogService(self.db).record_operation(

            object_type=OBJECT_IMPORT_JOB,
            object_no=job.job_no,
            object_name=job.source_filename,
            operation_type="APPLY_DRAFT",
            detail={"job_no": job.job_no, "scenario_code": template.scenario_code},
        )
        OperationLogService(self.db).record_operation(

            object_type=OBJECT_SCENARIO,
            object_no=template.scenario_code,
            object_name=template.scenario_name,
            operation_type="IMPORT_APPLY",
            detail={"job_no": job.job_no, "scenario_code": template.scenario_code},
        )
        ScenarioDraftService(self.db)._clear_sync_error( template, draft)
        crud.ImportsJobCRUD(self.db).refresh(job)
        return job
