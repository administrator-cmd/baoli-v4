"""受理一句话修改；消息和命令在同一事务落库，提交后才调用模型。"""

from sqlalchemy.orm import Session

from app.common.exceptions import BusinessError
from app.domain.generation_assistant import ASSISTANT_PROMPT_VERSION
from app.modules.imports.generation.crud import GenerationMessageCRUD
from app.modules.imports.generation.model import RulebookGenerationMessage
from app.modules.imports.generation.service.history import GenerationHistoryService
from app.modules.imports.generation.service.query import GenerationQueryService
from app.modules.system.operation.service.background import OperationBackgroundService
from app.modules.system.operation.service.operation_types import OBJECT_RULEBOOK_GENERATION


class GenerationAssistantService:
    def __init__(self, db: Session) -> None:
        self.db = db

    def submit(self, job_no: str, *, message: str, expected_revision: int, idempotency_key: str,
               issue_id: str | None = None):
        job = GenerationQueryService(self.db).get_job(job_no, lock=True)
        message = message.strip()
        if not message:
            raise BusinessError("ASSISTANT_MESSAGE_EMPTY", "请填写希望 AI 修改或解释的内容", status_code=400)
        request = {
            "job_no": job_no, "revision": expected_revision, "message": message,
            "source_sha256": job.source_sha256, "prompt_version": ASSISTANT_PROMPT_VERSION,
        }
        if issue_id is not None:
            request["issue_id"] = issue_id
        message_content = message

        def validate():
            nonlocal message_content
            document = GenerationQueryService(self.db).document(job)
            if job.revision != expected_revision:
                raise BusinessError("GENERATION_REVISION_CONFLICT", "文档已更新，请读取最新内容后再发送要求", status_code=409)
            if issue_id is not None:
                issue = next((item for item in document.issues if item.issue_id == issue_id), None)
                if issue is None:
                    raise BusinessError("GENERATION_ISSUE_NOT_FOUND", "待补充事项已变化，请刷新后重新选择", status_code=409)
                message_content = f"关于「{issue.title}」，请根据我的补充修改规则库：\n{message}"
            GenerationHistoryService(self.db).snapshot(job, origin="BASELINE", summary="启用修订记录时的已保存版本")

        def runner(db, command):
            from app.modules.imports.generation.assistant_execution import GenerationAssistantExecution
            return GenerationAssistantExecution(db).run(command)

        operation, reused = OperationBackgroundService(self.db).submit_operation(
            object_type=OBJECT_RULEBOOK_GENERATION, object_no=job_no, operation_type="RULEBOOK_REVISE",
            idempotency_key=idempotency_key.strip(), request=request, runner=runner,
            input_version=f"{expected_revision}:{ASSISTANT_PROMPT_VERSION}", validate=validate,
        )
        if not reused:
            GenerationMessageCRUD(self.db).add(RulebookGenerationMessage(
                generation_job_id=job.id, revision=expected_revision,
                operation_id=operation.id, role="USER", content=message_content,
            ))
        return operation, reused
