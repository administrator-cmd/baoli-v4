"""把冻结的编制导出导入已有场景，并保存真实导入作业关联。"""

import hashlib
import json

from sqlalchemy.orm import Session

from app.common.exceptions import BusinessError
from app.modules.imports.generation import crud
from app.modules.imports.generation.model import RulebookGenerationExport, RulebookGenerationImport
from app.modules.imports.generation.service.export import GenerationExportService
from app.modules.imports.generation.service.query import GenerationQueryService
from app.modules.imports.job.model import ImportJob
from app.modules.imports.job.service.upload import ImportsJobUploadService


class GenerationImportService:
    def __init__(self, db: Session) -> None:
        self.db = db

    def _request_hash(self, export: RulebookGenerationExport, scenario_code: str) -> str:
        payload = {
            "export_no": export.export_no,
            "revision": export.revision,
            "scenario_code": scenario_code,
            "export_sha256": export.sha256,
        }
        return hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8"),
        ).hexdigest()

    def _deleted_error(self, export_no: str) -> BusinessError:
        return BusinessError(
            "GENERATION_IMPORT_DELETED",
            "该请求对应的导入作业已被删除，系统不会自动创建替代导入；如需继续请使用新的幂等标识重新导入",
            status_code=409,
            details={"export_no": export_no},
        )

    def import_export(self, job_no: str, *,
        export_no: str,
        scenario_code: str,
        idempotency_key: str,
    ) -> tuple[RulebookGenerationImport, ImportJob, RulebookGenerationExport, bool]:
        """同一导出同一幂等键的重复请求返回同一导入作业；不同场景不能误复用。"""
        job = GenerationQueryService(self.db).get_job(job_no, lock=True)
        export = crud.RulebookGenerationExportCRUD(self.db).by_no(
            int(job.id), export_no, lock=True,
        )
        if export is None:
            raise BusinessError(
                "GENERATION_EXPORT_NOT_FOUND", "该作业下不存在指定的导出修订", status_code=404,
                details={"job_no": job_no, "export_no": export_no},
            )
        request_hash = self._request_hash(export, scenario_code)
        existing = crud.RulebookGenerationImportCRUD(self.db).by_request_key(
            int(export.id), idempotency_key,
        )
        if existing is not None:
            if existing.request_hash != request_hash:
                raise BusinessError(
                    "GENERATION_IMPORT_IDEMPOTENCY_CONFLICT",
                    "同一导入幂等标识已用于不同的导出修订或目标场景，拒绝复用已有请求",
                    status_code=409,
                    details={"export_no": export_no, "idempotency_key": idempotency_key},
                )
            if existing.import_job_id is None:
                raise self._deleted_error(export_no)
            import_job = crud.RulebookGenerationImportCRUD(self.db).import_jobs(
                [int(existing.import_job_id)],
            ).get(int(existing.import_job_id))
            if import_job is None:
                raise self._deleted_error(export_no)
            return existing, import_job, export, True

        path, _export = GenerationExportService(self.db).export_file(job_no, export_no)
        import_job = ImportsJobUploadService(self.db).create_import_job_from_file(
            path, source_filename=export.filename, scenario_code=scenario_code,
        )
        link = RulebookGenerationImport(
            generation_export_id=int(export.id),
            import_job_id=int(import_job.id),
            idempotency_key=idempotency_key,
            request_hash=request_hash,
        )
        crud.RulebookGenerationImportCRUD(self.db).add(link)
        return link, import_job, export, False
