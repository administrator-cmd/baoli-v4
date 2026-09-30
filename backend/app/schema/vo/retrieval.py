from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from ...common.api import ApiResponse


class RetrievalCandidateResponse(BaseModel):
    """一条印章主体候选。

    主体名称就是检索文本本身（向量只由它生成）。页码等定位信息不在主体确认界面展示，
    也不参与相似度；候选身份用于人工选择与提交校验。
    """

    chunk_id: int
    chunk_no: int
    index_no: str | None = None
    # 印章条目在原产物中的条目序号；用于回到原始条目。
    source_entry_no: int | None = None
    subject_name: str
    material_key: str | None = None
    material_name: str | None = None
    filename: str | None = None
    upload_no: str | None = None
    # 相似度只用于排序，不构成主体一致的结论。
    similarity: str

class RetrievalResponse(ApiResponse):
    task_no: str
    retrieval_no: str
    status: str
    message: str
    model_id: str | None = None
    model_revision: str | None = None
    index_version: str | None = None
    index_versions: list[str] = Field(default_factory=list)
    scope: dict[str, Any] = Field(default_factory=dict)
    candidates: list[RetrievalCandidateResponse] = Field(default_factory=list)
    # 本次实际送入后续步骤的正文条目；它是上下文预算，不是人工结论。
    adopted: list[RetrievalCandidateResponse] = Field(default_factory=list)
    # 有上传但没有可用主体索引的材料及原因（尚未识别主体 / 尚未建索引 / 索引失效）。
    uncovered_materials: list[dict[str, Any]] = Field(default_factory=list)
    excluded_indexes: list[dict[str, Any]] = Field(default_factory=list)

class RetrievalDecisionResponse(ApiResponse):
    """人工对主体候选的结论。

    selected 只在该次检索真实返回的候选内取值；decision_input_revision 记录提交时
    的任务输入修订，修订变化后旧结论不得套用到新的候选。
    """

    task_no: str
    retrieval_no: str
    decision: str
    note: str | None = None
    selected: dict[str, Any] | None = None
    decided_at: datetime | None = None
    decision_input_revision: int | None = None

class EvidenceIndexItem(BaseModel):
    """一个上传版本某个印章产物的索引状态。"""

    index_no: str
    artifact_type: str
    status: str
    # 索引是否仍适用于当前产物内容：产物重新识别后旧索引只是历史记录。
    current_for_artifact: bool = False
    chunk_count: int
    model_id: str
    model_revision: str
    embedding_dim: int | None = None
    chunk_version: str | None = None
    index_version: str
    document_upload_no: str | None = None
    filename: str | None = None
    material_name: str | None = None
    built_at: datetime | None = None
    failure_code: str | None = None
    failure_message: str | None = None

class TaskEvidenceIndexResponse(ApiResponse):
    task_no: str
    # 有索引记录时为真实索引版本；没有任何索引时为 None（不编造一个“当前版本”）。
    index_version: str | None = None
    model_id: str | None = None
    model_revision: str | None = None
    chunk_version: str | None = None
    indexed: bool = False
    items: list[EvidenceIndexItem] = Field(default_factory=list)

__all__ = [
    "RetrievalCandidateResponse",
    "RetrievalResponse",
    "RetrievalDecisionResponse",
    "EvidenceIndexItem",
    "TaskEvidenceIndexResponse",
]

