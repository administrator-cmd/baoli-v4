"""删除 AI 编制记录及其专属文件，保留已独立创建的场景导入。"""

from pathlib import Path

from sqlalchemy.orm import Session

from app.common.exceptions import BusinessError
from app.constants.status import ACTIVE_OPERATION_STATUSES
from app.core.transactions import after_commit, before_rollback
from app.modules.imports.generation.crud import RulebookGenerationCRUD
from app.modules.imports.generation.service.query import GenerationQueryService
from app.modules.system.operation.crud import SystemOperationCRUD
from app.modules.system.operation.service.operation_log import OperationLogService
from app.modules.system.operation.service.operation_types import OBJECT_RULEBOOK_GENERATION
from app.services.shared.import_cleanup import discard_staged_trees, restore_staged_trees, stage_tree
from app.services.shared.storage import resolve_storage_path


class GenerationLifecycleService:
    def __init__(self, db: Session) -> None:
        self.db = db

    def delete_job(self, job_no: str) -> None:
        job = GenerationQueryService(self.db).get_job(job_no, lock=True)
        operations = SystemOperationCRUD(self.db)
        related = operations.operations_for_jobs(OBJECT_RULEBOOK_GENERATION, [job_no], lock=True)
        if any(operation.status in ACTIVE_OPERATION_STATUSES for operation in related):
            raise BusinessError(
                "GENERATION_OPERATION_ACTIVE", "AI 操作尚未结束，请等待完成后再删除",
                status_code=409, details={"job_no": job_no},
            )

        directory = Path(job.source_path).parent.as_posix()
        resolved = resolve_storage_path(directory)
        if resolved.parent != resolve_storage_path(".") / "rulebook_generations" or not resolved.name.endswith(f"_{job_no}"):
            raise BusinessError("STORAGE_PATH_INVALID", "编制记录的文件目录不正确，无法删除", status_code=500)

        item = stage_tree(resolved)
        if item is not None:
            staged = [item]
            before_rollback(self.db, lambda: restore_staged_trees(staged))
            after_commit(self.db, lambda: discard_staged_trees(
                staged, object_no=job_no, error_code="GENERATION_FILE_CLEANUP_FAILED", object_label="编制记录",
            ))

        records = RulebookGenerationCRUD(self.db)
        records.delete_dependents(job.id)
        for operation in related:
            operations.delete(operation)
        records.delete(job)
        OperationLogService(self.db).record_operation(
            object_type=OBJECT_RULEBOOK_GENERATION, object_no=job_no, object_name=job.source_filename,
            operation_type="DELETE", permanent=True,
        )
