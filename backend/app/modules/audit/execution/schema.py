"""执行记录列表的响应契约。"""

from datetime import datetime
from typing import Any

from pydantic import BaseModel


class ExecutionProgressResponse(BaseModel):
    executed: int
    blocked: int
    not_executed: int


class ExecutionModelTurnsResponse(BaseModel):
    count: int | None
    available: bool


class ExecutionBlockReasonResponse(BaseModel):
    code: str | None
    text: str


class ExecutionRowResponse(BaseModel):
    execution_id: int
    task_no: str
    task_name: str
    scenario_name: str
    scenario_version_no: int
    node_code: str
    node_name: str
    sequence_no: int
    status: str
    invalidated: bool
    invalidated_at: datetime | None
    invalidation_reason: str | None
    started_at: datetime | None
    finished_at: datetime | None
    duration_ms: int | None
    failure_code: str | None
    failure_message: str | None
    result_summary: dict[str, Any] | None
    progress: ExecutionProgressResponse
    model_turns: ExecutionModelTurnsResponse
    current_blocked: int
    block_reason: ExecutionBlockReasonResponse | None
