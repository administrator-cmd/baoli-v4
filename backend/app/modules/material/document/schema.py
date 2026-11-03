"""材料位详情与上传历史。"""

from datetime import datetime

from pydantic import BaseModel, Field


class DocumentUploadItem(BaseModel):
    """一次上传的版本记录；version_no 按上传顺序从 1 递增。"""

    upload_no: str
    filename: str
    size_bytes: int
    content_type: str | None = None
    status: str
    is_current: bool
    version_no: int
    created_at: datetime | None = None


class DocumentDetailResponse(BaseModel):
    """材料位详情：当前文件与历史上传版本。"""

    document_no: str
    task_no: str | None = None
    task_name: str | None = None
    material_key: str
    material_name: str
    required: bool
    status: str
    current_upload_no: str | None = None
    uploads: list[DocumentUploadItem] = Field(default_factory=list)
