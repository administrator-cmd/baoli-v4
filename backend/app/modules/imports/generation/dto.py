"""AI 辅助生成规则库的请求参数。"""

from typing import Any

from pydantic import BaseModel, Field


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


class AssistantMessageRequest(BaseModel):
    message: str = Field(min_length=1, max_length=6000, pattern=r"\S")
    issue_id: str | None = Field(default=None, min_length=1, max_length=80, pattern=r"\S")
    expected_revision: int = Field(ge=1)
    idempotency_key: str = Field(min_length=1, max_length=120, pattern=r"\S")


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
