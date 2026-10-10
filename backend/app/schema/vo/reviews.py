from typing import Any

from pydantic import Field

from ...common.api import ApiResponse


class ReviewDraftResponse(ApiResponse):
    task_no: str
    pending_hash: str | None = None
    saved_review_nos: list[str] = Field(default_factory=list)
    drafts: dict[str, dict[str, Any]] = Field(default_factory=dict)


# 审核任务检索取证（RAG）
#
# 检索记录只用于取证：相似度仅作候选排序，不构成主体一致或审核通过的证明；
# 检索不到内容不等于材料不存在该条款。这些结论必须如实呈现在响应里。

__all__ = [
    "ReviewDraftResponse",
]

