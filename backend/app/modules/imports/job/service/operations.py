"""规则库转换的后台命令提交与当前性检查。"""

from typing import Any

from sqlalchemy.orm import Session

from app.common.exceptions import BusinessError
from app.modules.imports.job import crud
from app.modules.imports.job.model import ImportJob
from app.modules.imports.job.service.llm import CONVERSION_MODE_FULL, CONVERSION_MODE_RESUME, ImportsJobLlmService
from app.modules.system.operation.service import background as background_service
from app.modules.system.operation.service.background import PHASE_GRAPH_ADVANCED, PHASE_LLM_REQUEST
from app.modules.system.operation.service.operation_types import OBJECT_IMPORT_JOB
from app.services.shared.commands import _idempotency_key, _reject_unsupported_resume


class ImportsJobOperationsService:
    """本实体的服务方法；db 由构造时注入。"""

    def __init__(self, db: Session | None = None) -> None:
        self.db = db

    def _assert_import_command_current(self, job: ImportJob, content: dict[str, Any]) -> None:
        """执行前核对命令内容仍是当前输入，过期命令不按新输入重算。"""
        if (
            job.source_sha256 != content.get("source_sha256")
            or job.draft_revision != content.get("draft_revision")
        ):
            raise BusinessError(
                "IMPORT_CONVERSION_INPUT_STALE",
                "该转换命令对应的来源文件、草稿修订或工作表绑定已被改动，拒绝按过期命令执行",
                status_code=409,
            )


    def submit_conversion(self, job: ImportJob,
        *,
        idempotency_key: str | None,
        mode: str = CONVERSION_MODE_FULL,
    ) -> tuple[Any, bool]:
        """提交规则转换为后台操作。

        `mode=RESUME_REFINEMENT` 只重跑第二轮；没有可复用的第一轮结果时同步拒绝，
        不在后台静默退回整份重跑。
        """
        from app.modules.imports.job.service import lookup as imports_lookup

        if mode not in {CONVERSION_MODE_FULL, CONVERSION_MODE_RESUME}:
            raise BusinessError("IMPORT_CONVERSION_MODE_INVALID", "不支持的规则库解析方式", status_code=400)
        request = {
            "job_no": job.job_no,
            "source_sha256": job.source_sha256,
            "draft_revision": job.draft_revision,
            "mode": mode,
        }

        def validate() -> None:
            imports_lookup.ImportsJobLookupService(self.db)._ensure_import_mutable(job)
            if mode == CONVERSION_MODE_RESUME and not ImportsJobLlmService().can_resume_initial(
                job.job_no, job.profile_version, job.schema_key, job.schema_version, job.source_sha256,
                job.scenario_code,
            ):
                raise BusinessError(
                    "IMPORT_INITIAL_STAGE_UNAVAILABLE",
                    "第一轮解析结果不可用（缺失、损坏或解析契约已变化），请整份重新解析",
                    status_code=409,
                    details={"job_no": job.job_no},
                )

        def runner(session: Session, command: background_service.CommandHandle) -> dict[str, Any]:
            from app.modules.imports.job.execution import ImportConversionExecution

            # 导入图的推进与业务写入在同一服务函数内，无法只补齐图推进，明确拒绝而不是重做写入。
            _reject_unsupported_resume(command)
            content = command.request
            current = crud.ImportsJobCRUD(session).by_no(content["job_no"])
            if not current:
                raise BusinessError("IMPORT_JOB_NOT_FOUND", "导入作业不存在", status_code=404, details={"job_no": content["job_no"]})
            self._assert_import_command_current(current, content)
            command.phase(PHASE_LLM_REQUEST)
            converted = ImportConversionExecution(session).retry_conversion(content["job_no"], operation_no=command.operation_no,
                mode=content.get("mode", CONVERSION_MODE_FULL),
            )
            # 导入解析完成后图直接结束，没有业务裁决等待点。
            command.phase(PHASE_GRAPH_ADVANCED)
            return {
                "job_no": converted.job_no,
                "status": converted.status,
                "failure_code": converted.failure_code,
                "failure_message": converted.failure_message,
                "mode": content.get("mode", CONVERSION_MODE_FULL),
            }

        return background_service.OperationBackgroundService(self.db).submit_operation(object_type=OBJECT_IMPORT_JOB,
            object_no=job.job_no,
            operation_type="IMPORT_CONVERT",
            idempotency_key=_idempotency_key(idempotency_key),
            request=request,
            runner=runner,
            target_node=None,
            input_version=str(job.draft_revision),
            validate=validate,
        )
