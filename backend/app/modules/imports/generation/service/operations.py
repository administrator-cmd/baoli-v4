"""生成作业的后台命令提交与输入当前性检查。"""

from typing import Any

from sqlalchemy.orm import Session

from app.common.exceptions import BusinessError
from app.domain.generation.document import GENERATION_DOCUMENT_SCHEMA_VERSION, GENERATION_PROMPT_VERSION
from app.modules.imports.generation import crud
from app.modules.imports.generation.model import RulebookGenerationJob
from app.modules.imports.generation.service.upload import GenerationUploadService
from app.modules.system.operation.model import BackgroundOperation
from app.modules.system.operation.service import background as background_service
from app.modules.system.operation.service.background import (
    PHASE_LLM_REQUEST,
    PHASE_RESULT_READY,
)
from app.modules.system.operation.service.operation_types import OBJECT_RULEBOOK_GENERATION
from app.services.shared.commands import _idempotency_key, _reject_unsupported_resume
from app.services.shared.storage import resolve_storage_path


class GenerationOperationsService:
    """开始生成只受理后台操作；成功后不提供原地再次生成。"""

    def __init__(self, db: Session) -> None:
        self.db = db

    def _assert_command_current(self, job: RulebookGenerationJob, content: dict[str, Any]) -> None:
        if (
            job.source_sha256 != content.get("source_sha256")
            or job.request_hash != content.get("request_hash")
            or job.prompt_version != content.get("prompt_version")
            or job.document_schema_version != content.get("document_schema_version")
            or job.revision != content.get("revision")
            or job.document_json is not None
        ):
            raise BusinessError(
                "GENERATION_INPUT_STALE",
                "该生成命令对应的原文件、生成输入或契约版本已变化，拒绝按过期命令执行",
                status_code=409,
                details={"job_no": job.job_no},
            )

    def retry_generation(
        self, job: RulebookGenerationJob, *, idempotency_key: str,
    ) -> tuple[RulebookGenerationJob, BackgroundOperation, bool]:
        """同版本重新受理；旧版本复用原件建立新作业，不改写冻结输入。

        作业与后台命令在请求事务内一并保存，提交后才调度模型。
        """
        requires_new_job = (
            job.document_schema_version != GENERATION_DOCUMENT_SCHEMA_VERSION
            or job.prompt_version != GENERATION_PROMPT_VERSION
        )
        if not requires_new_job:
            operation, reused = self.submit_generation(job, idempotency_key=idempotency_key)
            return job, operation, reused

        background = background_service.OperationBackgroundService(self.db)
        if job.document_json is not None or job.revision > 0:
            raise BusinessError(
                "GENERATION_DOCUMENT_EXISTS",
                "该记录已有编制结果；请通过新建编制上传资料，已有结果不会被重试覆盖",
                status_code=409, details={"job_no": job.job_no},
            )
        target, _ = GenerationUploadService(self.db).reuse_source(job)
        # 先锁定目标，再读取后台状态，避免旧事务快照漏掉刚受理的目标操作。
        background.assert_no_active_operation(OBJECT_RULEBOOK_GENERATION, job.job_no)
        try:
            operation, reused = self.submit_generation(target, idempotency_key=idempotency_key)
        except BusinessError as exc:
            if exc.code not in {"OPERATION_ALREADY_ACTIVE", "GENERATION_DOCUMENT_EXISTS"}:
                raise
            # 另一次点击已开始或完成：带用户到真实目标，不重复执行或覆盖文档。
            operation = background.latest_operation(OBJECT_RULEBOOK_GENERATION, target.job_no)
            if operation is None:
                raise
            reused = True
        return target, operation, reused

    def submit_generation(self, job: RulebookGenerationJob, *, idempotency_key: str | None) -> tuple[BackgroundOperation, bool]:
        key = _idempotency_key(idempotency_key)
        background = background_service.OperationBackgroundService(self.db)
        existing = background.existing_operation(
            object_type=OBJECT_RULEBOOK_GENERATION,
            object_no=job.job_no,
            operation_type="RULEBOOK_GENERATE",
            idempotency_key=key,
        )
        request = {
            "job_no": job.job_no,
            "source_sha256": job.source_sha256,
            "request_hash": job.request_hash,
            "document_schema_version": job.document_schema_version,
            "prompt_version": job.prompt_version,
            # 重发比较受理时的修订，不能被生成完成或后续编辑改变。
            "revision": existing.request_json["revision"] if existing is not None else job.revision,
        }

        def validate() -> None:
            if (
                job.document_schema_version != GENERATION_DOCUMENT_SCHEMA_VERSION
                or job.prompt_version != GENERATION_PROMPT_VERSION
            ):
                raise BusinessError(
                    "GENERATION_VERSION_UNSUPPORTED",
                    "该作业冻结的编制或提示词版本与当前程序不一致，请新建生成作业",
                    status_code=409,
                    details={"job_no": job.job_no, "document_schema_version": job.document_schema_version,
                             "prompt_version": job.prompt_version},
                )
            if job.document_json is not None or job.revision > 0:
                raise BusinessError(
                    "GENERATION_DOCUMENT_EXISTS",
                    "该作业已经生成成功，不能原地再次 AI 生成；如需另一份结果请新建生成作业",
                    status_code=409,
                    details={"job_no": job.job_no, "revision": job.revision},
                )
            if not resolve_storage_path(job.source_path).is_file():
                raise BusinessError(
                    "GENERATION_SOURCE_FILE_MISSING", "原始 Excel 已不在存储目录中，无法生成",
                    status_code=404, details={"job_no": job.job_no},
                )

        def runner(session: Session, command: background_service.CommandHandle) -> dict[str, Any]:
            from app.modules.imports.generation.execution import GenerationExecution

            _reject_unsupported_resume(command)
            content = command.request
            current = crud.RulebookGenerationCRUD(session).by_no(content["job_no"])
            if current is None:
                raise BusinessError(
                    "GENERATION_JOB_NOT_FOUND", "生成作业不存在", status_code=404,
                    details={"job_no": content["job_no"]},
                )
            self._assert_command_current(current, content)
            command.phase(PHASE_LLM_REQUEST)
            generated = GenerationExecution(session).generate(
                content["job_no"], operation_no=command.operation_no, content=content,
            )
            command.phase(PHASE_RESULT_READY)
            document = generated.document_json or {}
            return {
                "job_no": generated.job_no,
                "revision": generated.revision,
                "documents": {
                    "sheets": len(document["sheets"]),
                    "entries": sum(len(sheet["rows"]) for sheet in document["sheets"]),
                    "issues": len(document.get("issues") or []),
                },
            }

        return background.submit_operation(
            object_type=OBJECT_RULEBOOK_GENERATION,
            object_no=job.job_no,
            operation_type="RULEBOOK_GENERATE",
            idempotency_key=key,
            request=request,
            runner=runner,
            target_node=None,
            input_version=f"{job.revision}:{job.prompt_version}",
            validate=validate,
        )
