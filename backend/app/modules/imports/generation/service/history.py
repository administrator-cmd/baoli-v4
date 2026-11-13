"""编制文档的只追加快照与历史恢复；事务由请求或后台执行层管理。"""

from sqlalchemy.orm import Session

from app.common.exceptions import BusinessError
from app.domain.generation.document import GenerationDocument
from app.modules.imports.generation.crud import GenerationRevisionCRUD
from app.modules.imports.generation.model import RulebookGenerationJob, RulebookGenerationRevision
from app.modules.imports.generation.service.query import GenerationQueryService
from app.modules.system.operation.service.background import OperationBackgroundService
from app.modules.system.operation.service.operation_types import OBJECT_RULEBOOK_GENERATION


class GenerationHistoryService:
    def __init__(self, db: Session) -> None:
        self.db = db

    def snapshot(self, job: RulebookGenerationJob, *, origin: str, summary: str, operation_id: int | None = None) -> None:
        if job.document_json is None or job.revision < 1:
            return
        crud = GenerationRevisionCRUD(self.db)
        existing = crud.by_revision(job.id, job.revision)
        if existing is not None:
            if existing.document_json != job.document_json:
                raise BusinessError("GENERATION_HISTORY_CONFLICT", "同一版本已有不同的文档快照，拒绝覆盖", status_code=409)
            return
        crud.add(RulebookGenerationRevision(
            generation_job_id=job.id, revision=job.revision, document_json=job.document_json,
            origin=origin, summary=summary, operation_id=operation_id,
        ))

    def document_at(self, job: RulebookGenerationJob, revision: int) -> GenerationDocument:
        if revision == job.revision:
            return GenerationQueryService(self.db).document(job)
        item = GenerationRevisionCRUD(self.db).by_revision(job.id, revision)
        if item is None:
            raise BusinessError("GENERATION_REVISION_NOT_FOUND", "此版本没有保存的文档快照", status_code=404)
        return GenerationDocument.model_validate(item.document_json)

    def restore(self, job_no: str, *, expected_revision: int, target_revision: int) -> RulebookGenerationJob:
        job = GenerationQueryService(self.db).get_job(job_no, lock=True)
        GenerationQueryService(self.db).document(job)
        OperationBackgroundService(self.db).assert_no_active_operation(OBJECT_RULEBOOK_GENERATION, job_no)
        if job.revision != expected_revision:
            raise BusinessError("GENERATION_REVISION_CONFLICT", "当前文档已更新，请刷新后再恢复历史版本", status_code=409)
        if target_revision == job.revision:
            return job
        document = self.document_at(job, target_revision)
        # 恢复正文但保留已使用的流水上界，之后新增条目不能重用已删除版本的编号。
        current = GenerationQueryService(self.db).document(job)
        document.code_counters = {
            key: max(document.code_counters.get(key, 0), current.code_counters.get(key, 0))
            for key in document.code_counters.keys() | current.code_counters.keys()
        }
        self.snapshot(job, origin="BASELINE", summary="启用修订记录时的已保存版本")
        job.revision += 1
        job.document_json = document.model_dump(mode="json")
        self.snapshot(job, origin="RESTORE", summary=f"恢复第 {target_revision} 版")
        return job
