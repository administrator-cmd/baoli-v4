"""AI 辅助生成规则库的响应模型。"""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from app.core.base_schema import PagedResponse
from app.modules.system.operation.schema import OperationAcceptedResponse


class GenerationImportLinkResponse(BaseModel):
    """导出与真实导入作业的关联；导入被删除时保留关联并如实标记。"""

    import_job_no: str | None = None
    scenario_code: str | None = None
    import_status: str | None = None
    deleted: bool = False
    created_at: datetime


class GenerationExportResponse(BaseModel):
    """冻结导出的真实文件与关联导入状态。"""

    export_no: str
    revision: int
    filename: str
    size: int
    sha256: str
    export_format_version: str
    created_at: datetime
    imports: list[GenerationImportLinkResponse] = Field(default_factory=list)


class GenerationJobResponse(BaseModel):
    """生成作业详情；文档只在这里返回，列表不携带大 JSON。"""

    job_no: str
    source_filename: str
    source_size: int
    business_background: str
    generation_requirements: str
    document_schema_version: str
    prompt_version: str
    revision: int
    # 由最近后台操作与已保存文档推导，不复制模型调用状态。
    status: str
    status_message: str
    document: dict[str, Any] | None = None
    latest_operation: OperationAcceptedResponse | None = None
    document_supported: bool = True
    entry_count: int = 0
    open_issue_count: int = 0
    can_generate: bool = False
    can_retry: bool = False
    retry_requires_new_job: bool = False
    can_edit: bool = False
    created_at: datetime
    updated_at: datetime
    reused: bool = False


class GenerationExportSummary(BaseModel):
    """列表行可下载的最新导出；不暴露全部修订。"""

    export_no: str
    revision: int
    filename: str


class GenerationJobRowResponse(BaseModel):
    """生成记录列表行；不动库里的大 JSON 列。"""

    job_no: str
    source_filename: str
    source_size: int
    status: str
    status_message: str
    revision: int
    latest_operation: OperationAcceptedResponse | None = None
    latest_export: GenerationExportSummary | None = None
    created_at: datetime
    updated_at: datetime


class GenerationJobPageResponse(PagedResponse[GenerationJobRowResponse]):
    """生成记录分页：total 与筛选范围一致。"""


class GenerationCreatedResponse(BaseModel):
    """上传创建结果；reused=True 表示同一幂等键同内容返回原作业。"""

    job: GenerationJobResponse
    reused: bool


class GenerationRetryResponse(BaseModel):
    """重试的真实目标记录与后台操作；旧版本重试可能返回新作业。"""

    job: GenerationJobResponse
    operation: OperationAcceptedResponse
    reused: bool


class GenerationMessageResponse(BaseModel):
    id: int
    role: str
    content: str
    issue_id: str | None = None
    answer: str | None = None
    revision: int
    created_at: datetime
    operation: OperationAcceptedResponse


class GenerationMessagesResponse(BaseModel):
    items: list[GenerationMessageResponse]
    next_before_id: int | None = None


class GenerationRevisionResponse(BaseModel):
    revision: int
    summary: str
    origin: str
    created_at: datetime


class GenerationImportResponse(BaseModel):
    """选择场景导入的结果；返回真实导入作业编号。"""

    export_no: str
    revision: int
    import_job_no: str
    scenario_code: str
    import_status: str
    reused: bool
