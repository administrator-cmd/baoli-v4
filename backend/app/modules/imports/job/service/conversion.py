"""规则库转换状态准备、候选清理与重试。"""

from sqlalchemy.orm import Session

from app.common.exceptions import BusinessError
from app.modules.imports.job.model import ImportJob
from app.modules.imports.job.service.lookup import ImportsJobLookupService
from app.modules.system.operation.crud import SystemOperationCRUD
from app.modules.system.operation.service.operation_types import OBJECT_IMPORT_JOB


class ImportsJobConversionService:
    """本实体的服务方法；db 由构造时注入。"""

    def __init__(self, db: Session | None = None) -> None:
        self.db = db

    def _conversion_job_for_write(self, job_no: str, operation_no: str, *, draft_revision: int | None = None,
    ) -> ImportJob:
        """短事务内确认转换仍有效；与中止共用加锁顺序，拒绝旧线程写回。"""
        job = ImportsJobLookupService(self.db)._job_or_404( job_no, lock=True)
        operation = SystemOperationCRUD(self.db).lock_by_operation_no(
            operation_no, object_type=OBJECT_IMPORT_JOB, object_no=job_no,
        )
        if operation is None:
            raise BusinessError("OPERATION_NOT_FOUND", "转换操作不存在", status_code=404)
        if operation.status != "RUNNING":
            raise BusinessError(
                "OPERATION_NOT_ACTIVE", "转换操作已结束或被中止，拒绝写入旧结果", status_code=409,
                details={"operation_no": operation_no, "status": operation.status},
            )
        if draft_revision is not None and job.draft_revision != draft_revision:
            raise BusinessError(
                "IMPORT_DRAFT_REVISION_CONFLICT", "导入配置已变化，拒绝写入旧转换结果", status_code=409,
                details={"job_no": job_no},
            )
        ImportsJobLookupService(self.db)._ensure_import_mutable( job)
        return job


    def _reset_conversion_candidates(self, job: ImportJob) -> None:
        """新结果与旧候选的替换在同一短事务中提交。"""
        job.proposals_json = None
        job.draft_revision = job.draft_revision + 1
