"""AI 辅助生成规则库的请求参数。"""

from typing import Any

from pydantic import BaseModel, Field, model_validator


class StartGenerationRequest(BaseModel):
    """开始生成或失败后显式重试；同一幂等键的重发复用已有后台操作。"""

    idempotency_key: str | None = Field(default=None, max_length=120)


class RetryGenerationRequest(BaseModel):
    """显式重试；响应丢失后的重发必须复用同一标识。"""

    idempotency_key: str = Field(min_length=1, max_length=120, pattern=r"\S")


class SaveGenerationDocumentRequest(BaseModel):
    """保存完整编辑文档；旧修订保存明确冲突。"""

    expected_revision: int = Field(ge=0)
    document: dict[str, Any]


class AssistantIssueAnswer(BaseModel):
    issue_id: str = Field(min_length=1, max_length=80, pattern=r"\S")
    answer: str = Field(min_length=1, max_length=6000, pattern=r"\S")


class AssistantMessageRequest(BaseModel):
    message: str | None = Field(default=None, min_length=1, max_length=6000, pattern=r"\S")
    issue_id: str | None = Field(default=None, min_length=1, max_length=80, pattern=r"\S")
    issue_answers: list[AssistantIssueAnswer] | None = Field(default=None, min_length=1)
    expected_revision: int = Field(ge=1)
    idempotency_key: str = Field(min_length=1, max_length=120, pattern=r"\S")

    @model_validator(mode="after")
    def validate_submission(self):
        if self.issue_answers is not None:
            if self.message is not None or self.issue_id is not None:
                raise ValueError("批量回答不能与单条回答或修改指令同时提交")
            if len({item.issue_id for item in self.issue_answers}) != len(self.issue_answers):
                raise ValueError("同一待补充事项不能重复回答")
        elif self.message is None:
            raise ValueError("请填写希望 AI 修改或解释的内容")
        return self


class RestoreGenerationRequest(BaseModel):
    expected_revision: int = Field(ge=1)
    target_revision: int = Field(ge=1)


class CreateGenerationExportRequest(BaseModel):
    """按指定修订创建或取得导出；不调用模型。"""

    expected_revision: int = Field(ge=1)


class ImportGenerationRequest(BaseModel):
    """把冻结导出的指定修订导入已有场景。"""

    export_no: str = Field(min_length=1, max_length=40)
    scenario_code: str = Field(min_length=2, max_length=80, pattern=r"^[A-Za-z0-9_-]+$")
    idempotency_key: str = Field(min_length=1, max_length=120)
