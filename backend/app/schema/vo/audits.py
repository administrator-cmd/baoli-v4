from datetime import datetime
from typing import Any

from ...common.api import ApiResponse


class OcrArtifactStatusResponse(ApiResponse):
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

