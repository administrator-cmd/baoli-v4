"""规则库转换的输入读取、模型调用与结果提交。"""

from app.common.exceptions import BusinessError
from app.core.transactions import commit_transaction, rollback_transaction
from app.domain.shared.identity import public_no
from app.modules.imports.job.model import ImportJob
from app.modules.imports.job.service.conversion import ImportsJobConversionService
from app.modules.imports.job.service.llm import CONVERSION_MODE_FULL, CONVERSION_MODE_RESUME, ImportsJobLlmService
from app.modules.imports.job.service.lookup import ImportsJobLookupService
from app.modules.imports.job.service.workbook import ImportsJobWorkbookService
from app.services.shared.storage import resolve_storage_path


class ImportConversionExecution(ImportsJobConversionService):
    """读取输入、释放锁、请求模型，再提交候选或真实失败。"""

    def retry_conversion(self, job_no: str, *, operation_no: str, mode: str = CONVERSION_MODE_FULL,
    ) -> ImportJob:
        """读取输入后释放事务，模型返回时再锁定作业并提交结果。

        工作簿原文按 source_path 重新读取：命令执行前已经核对过 source_sha256，
        同一份文件重读得到同一份 IR，因此不需要把整份原文也存进数据库。

        `mode=RESUME_REFINEMENT` 复用上次保存的第一轮目录，只重跑第二轮。
        第一轮结果按作业固定存放，输入指纹不符或文件缺失时明确拒绝，不静默退回整份重跑。
        """
        job = self._conversion_job_for_write( job_no, operation_no)
        if mode not in {CONVERSION_MODE_FULL, CONVERSION_MODE_RESUME}:
            raise BusinessError("IMPORT_CONVERSION_MODE_INVALID", "不支持的规则库解析方式", status_code=400)
        draft_revision = job.draft_revision
        scenario_code = job.scenario_code
        profile_version = job.profile_version
        schema_key = job.schema_key
        schema_version = job.schema_version
        source_sha256 = job.source_sha256
        source_path = job.source_path
        # 校验作业冻结的场景 schema；本模块不再使用返回值。
        ImportsJobLookupService()._job_schema(job)
        fingerprint = ImportsJobLlmService().initial_fingerprint(profile_version, schema_key, schema_version, source_sha256, scenario_code)
        if mode == CONVERSION_MODE_RESUME and not ImportsJobLlmService().can_resume_initial(
            source_path, profile_version, schema_key, schema_version, source_sha256, scenario_code,
        ):
            raise BusinessError(
                "IMPORT_INITIAL_STAGE_UNAVAILABLE",
                "第一轮解析结果不可用（缺失、损坏或解析契约已变化），需要整份重新解析",
                status_code=409,
                details={"job_no": job_no},
            )
        try:
            workbook_ir = ImportsJobWorkbookService().read_workbook_ir(
                resolve_storage_path(job.source_path),
                workbook_id=public_no("wb"),
                source_filename=job.source_filename,
                source_size=job.source_size,
                source_sha256=job.source_sha256,
                profile_version=job.profile_version,
                scenario_code=scenario_code,
                schema_key=job.schema_key,
                schema_version=job.schema_version,
            )
            # 到此只有本地读取：显式结束事务并释放行锁，模型返回前不持有任何锁，
            # 后续不得访问已过期的 ORM 输入。
            rollback_transaction(self.db)
            proposals = ImportsJobLlmService().convert_workbook(
                workbook_ir, operation_no=operation_no, job_no=job_no, source_path=source_path,
                fingerprint=fingerprint, mode=mode,
            )
            job = self._conversion_job_for_write( job_no, operation_no, draft_revision=draft_revision)
            self._reset_conversion_candidates( job)
            job.proposals_json = proposals
            job.status = "PROPOSED"
            job.failure_code = None
            job.failure_message = None
            commit_transaction(self.db)
            return job
        except BusinessError as exc:
            # 撤销本次未提交的候选；只给仍在执行且输入未变化的命令记录转换失败。
            rollback_transaction(self.db)
            job = self._conversion_job_for_write( job_no, operation_no, draft_revision=draft_revision)
            self._reset_conversion_candidates( job)
            if exc.code == "IMPORT_INITIAL_STAGE_UNAVAILABLE":
                # 检查点不可用不是模型失败：作业保持可再次提交，页面按「整份重新解析」处理。
                job.status = "PARSED"
            elif exc.code in {
                "LLM_CONFIG_MISSING",
                "LLM_TIMEOUT",
                "LLM_AUTH_FAILED",
                "LLM_NETWORK_FAILED",
                "LLM_RESULT_UNKNOWN",
                "LLM_EXTERNAL_FAILED",
            }:
                job.status = "EXTERNAL_FAILED"
            else:
                job.status = "CONTRACT_INVALID"
            job.failure_code = exc.code
            job.failure_message = exc.message
            commit_transaction(self.db)
            raise
