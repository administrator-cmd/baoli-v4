"""规则库导入作业查询、结构读取与可变状态检查。"""

from typing import Any

from sqlalchemy.orm import Session

from app.common.exceptions import BusinessError
from app.domain.configuration import scenario_schema
from app.modules.imports.job import crud
from app.modules.imports.job.model import ImportJob
from app.modules.scenario.template.service.crud import ScenarioTemplateCRUD


class ImportsJobLookupService:
    """本实体的服务方法；db 由构造时注入。"""

    def __init__(self, db: Session | None = None) -> None:
        self.db = db

    def _job_or_404(self, job_no: str, *, lock: bool = False) -> ImportJob:
        job = crud.ImportsJobCRUD(self.db).by_no(job_no, lock=lock)
        if not job:
            raise BusinessError("IMPORT_JOB_NOT_FOUND", "导入作业不存在", status_code=404, details={"job_no": job_no})
        return job


    def _job_schema(self, job: ImportJob) -> dict[str, Any]:
        if not isinstance(job.schema_json, dict):
            raise BusinessError(
                "IMPORT_SCENARIO_SCHEMA_MISSING",
                "导入作业缺少创建时的场景 schema 快照，不能继续解释工作簿",
                status_code=409,
                details={"job_no": job.job_no},
            )
        schema = scenario_schema.normalize_schema(job.schema_json)
        if job.schema_key != schema["schema_key"] or job.schema_version != schema["schema_version"]:
            raise BusinessError(
                "IMPORT_SCENARIO_SCHEMA_MISMATCH",
                "导入作业的场景 schema 身份与快照不一致",
                status_code=409,
                details={"job_no": job.job_no},
            )
        return schema


    def _ensure_import_mutable(self, job: ImportJob) -> None:
        published_version_id = ScenarioTemplateCRUD(self.db).version_id_by_import_job(job.id)
        if job.status == "APPLIED":
            raise BusinessError(
                "IMPORT_ALREADY_APPLIED",
                "该导入作业已写入场景草稿，不能再修改。请到场景草稿里核对配置，或重新上传一份规则库",
                status_code=409,
                details={"job_no": job.job_no, "scenario_code": job.scenario_code},
            )
        if published_version_id is not None:
            raise BusinessError(
                "IMPORT_ALREADY_PUBLISHED",
                "该导入作业已产生场景版本，不能再修改或重新编译",
                status_code=409,
            )


    def _saved_proposals(self, job: ImportJob) -> dict[str, Any]:
        """最终模型结果入库前已解析，读取时只确认有 JSON 对象。"""
        if not isinstance(job.proposals_json, dict):
            raise BusinessError("IMPORT_PROPOSALS_INVALID", "导入作业没有已保存的配置 JSON", status_code=409,
                                details={"job_no": job.job_no})
        return job.proposals_json
