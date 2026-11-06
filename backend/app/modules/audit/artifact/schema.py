"""审核响应模型。"""

from datetime import datetime
from typing import Any

from pydantic import BaseModel


class ReplacementImpact(BaseModel):
    """替换上传文件的影响范围：历史事实仍可追溯，但不再进入当前报告。"""

    affected_artifacts: int = 0
    stale_field_values: int = 0
    invalidated_rule_results: int = 0
    invalidated_node_executions: int = 0
    invalidated_reviews: int = 0
    reset_to_node: str | None = None
    description: str = ""


class DocumentUploadResponse(BaseModel):
    document_no: str
    material_name: str
    filename: str
    size_bytes: int
    status: str
    material_classify: dict[str, Any] | None = None
    impact: ReplacementImpact | None = None


class ExternalArtifactStatusResponse(BaseModel):
    document_no: str
    artifact_type: str
    artifact_no: str | None = None
    status: str
    message: str


class DocumentRemoveResponse(BaseModel):
    document_no: str
    material_name: str
    status: str
    input_revision: int
    impact: ReplacementImpact | None = None

class OcrArtifactStatusResponse(BaseModel):
    """当前上传文件 OCR 产物的只读状态。"""

    document_no: str
    artifact_no: str | None = None
    status: str
    job_id: str | None = None
    deadline_at: datetime | None = None
    message: str
    submit_attempts: int | None = None
    provider_state: str | None = None
    extract_progress: dict[str, Any] | None = None
    progress_updated_at: datetime | None = None
