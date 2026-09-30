
from pydantic import BaseModel, Field


class ReviewDraftItem(BaseModel):
    review_no: str = Field(min_length=1, max_length=40)
    decision: str | None = Field(default=None, max_length=40)
    business_note: str | None = Field(default=None, max_length=2000)
    field_value: str | None = Field(default=None, max_length=2000)
    basis_type: str | None = Field(default=None, max_length=30)

class ReviewDraftRequest(BaseModel):
    items: list[ReviewDraftItem] = Field(default_factory=list)

__all__ = [
    "ReviewDraftItem",
    "ReviewDraftRequest",
]

