from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel

from app.core.base_schema import PagedResponse


class OperationLogRowResponse(BaseModel):
    entry_no: str
    source: Literal["BUSINESS", "COMMAND"]
    object_type: str
    object_no: str
    object_name: str
    operation_type: str
    result: Literal["SUCCEEDED", "FAILED", "ACCEPTED"]
    duration_ms: int | None
    command_no: str | None
    failure_code: str | None
    failure_message: str | None
    created_at: datetime
    finished_at: datetime | None


class OperationLogPageResponse(PagedResponse[OperationLogRowResponse]):
    pass


class OperationAcceptedResponse(BaseModel):
    """长操作的受理状态。它只表示操作已被受理，绝不表示业务已经成功。"""

    operation_no: str
    object_no: str
    object_type: str
    operation_type: str
    target_node: str | None = None
    input_version: str | None = None
    status: Literal["PENDING", "RUNNING", "SUCCEEDED", "FAILED", "INTERRUPTED"]
    message: str
    reused: bool = False
    result: dict[str, Any] | None = None
    raw_response_path: str | None = None
    failure_code: str | None = None
    failure_message: str | None = None
    # 命令当前停在哪个阶段，用于区分“已保存未执行 / 外部已受理 / 结果未知 /
    # 业务已提交但图未推进 / 等待 interrupt / 结果已就绪 / 检查点暂不可达 / 检查点确实缺失”。
    phase: str | None = None
    external_job_ref: str | None = None
    created_at: datetime | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
