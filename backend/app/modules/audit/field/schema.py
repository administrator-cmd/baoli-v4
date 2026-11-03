"""审核响应模型。"""

from typing import Any

from pydantic import BaseModel, Field


class FieldCorrectionResponse(BaseModel):
    task_no: str
    field_key: str
    material_key: str
    review_no: str
    fact_revision_no: int
    previous_revision_no: int | None = None
    before_value: str | None = None
    after_value: str | None = None
    normalized_value: Any | None = None
    status: str
    invalidated_reviews: int = 0
    invalidated_rule_results: int = 0
    recomputed_rules: list[str] = Field(default_factory=list)


class FieldReviewSaveResponse(BaseModel):
    """字段核对保存结果：真实发生的事实版本变化与剩余缺项。"""

    task_no: str
    input_revision: int
    applied: list[dict[str, Any]] = Field(default_factory=list)
    missing_field_count: int = 0
    missing_fields: list[dict[str, Any]] = Field(default_factory=list)
