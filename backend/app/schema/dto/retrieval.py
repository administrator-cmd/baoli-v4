from typing import Any

from pydantic import BaseModel, Field


class RetrievalRequest(BaseModel):
    """一次任务内检索：查询内容与材料范围都必须显式给出。"""

    query_text: str = Field(min_length=1, max_length=4000)
    # 材料范围：只能引用本任务已发布的材料 key；为空表示未限定范围，服务端会明确拒绝。
    material_keys: list[str] = Field(min_length=1)
    rule_key: str | None = Field(default=None, max_length=40)
    top_k: int | None = Field(default=None, ge=1, le=64)

class RetrievalDecisionRequest(BaseModel):
    """工作人员对一次主体检索候选的结论。

    只接受一致 / 不一致 / 证据不足；相似度不能代替审核结论，
    也不存在“自动采纳前几条候选”的取值。
    """

    decision: str = Field(pattern="^(CONSISTENT|INCONSISTENT|EVIDENCE_INSUFFICIENT)$")
    note: str | None = Field(default=None, max_length=2000)
    # 确认一致或不一致时必须指明所依据的候选（本次检索返回的候选身份）。
    selected: dict[str, Any] | None = None
    # 提交时的任务输入修订：与当前修订不一致说明候选已过期。
    expected_input_revision: int | None = Field(default=None, ge=0)

__all__ = [
    "RetrievalRequest",
    "RetrievalDecisionRequest",
]

