"""规则库转换的后台命令提交与当前性检查。"""

from typing import Any
from sqlalchemy import select
from sqlalchemy.orm import Session
from ...common.exceptions import BusinessError
from ...entity import ImportJob
from ..system import background as background_service
from ..system.background import PHASE_LLM_REQUEST, PHASE_GRAPH_ADVANCED
from ..system.operation_types import OBJECT_IMPORT_JOB
from ..shared.commands import _idempotency_key, _reject_unsupported_resume


def _assert_import_command_current(db: Session, job: ImportJob, content: dict[str, Any]) -> None:
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


def submit_conversion(
    db: Session,
    job: ImportJob,
    *,
    idempotency_key: str | None,
) -> tuple[Any, bool]:
    """提交规则转换为后台操作。"""
    from . import lookup as imports_lookup

    request = {
        "job_no": job.job_no,
        "source_sha256": job.source_sha256,
        "draft_revision": job.draft_revision,
    }

    def validate() -> None:
        imports_lookup._ensure_import_mutable(db, job)

    def runner(session: Session, command: background_service.CommandHandle) -> dict[str, Any]:
        from . import conversion as imports_conversion

        # 导入图的推进与业务写入在同一服务函数内，无法只补齐图推进，明确拒绝而不是重做写入。
        _reject_unsupported_resume(command)
        content = command.request
        current = session.scalar(select(ImportJob).where(ImportJob.job_no == content["job_no"]))
        if not current:
            raise BusinessError("IMPORT_JOB_NOT_FOUND", "导入作业不存在", status_code=404, details={"job_no": content["job_no"]})
        _assert_import_command_current(session, current, content)
        command.phase(PHASE_LLM_REQUEST)
        converted = imports_conversion.retry_conversion(
            session, content["job_no"], operation_no=command.operation_no,
        )
        # 导入解析完成后图直接结束，没有业务裁决等待点。
        command.phase(PHASE_GRAPH_ADVANCED)
        return {
            "job_no": converted.job_no,
            "status": converted.status,
            "failure_code": converted.failure_code,
            "failure_message": converted.failure_message,
        }

    return background_service.submit_operation(
        db,
        object_type=OBJECT_IMPORT_JOB,
        object_no=job.job_no,
        operation_type="IMPORT_CONVERT",
        idempotency_key=_idempotency_key(idempotency_key),
        request=request,
        runner=runner,
        target_node=None,
        input_version=str(job.draft_revision),
        validate=validate,
    )
