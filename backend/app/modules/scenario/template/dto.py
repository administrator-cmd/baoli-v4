"""场景请求模型。"""

from typing import Any

from pydantic import BaseModel, Field


class ScenarioCreateRequest(BaseModel):
    scenario_name: str = Field(min_length=1, max_length=255)
    category: str | None = Field(default=None, max_length=80)
    description: str | None = Field(default=None, max_length=4000)
    enabled: bool = True
    schema_definition: dict[str, Any] | None = Field(default=None, alias="schema_json")


class ScenarioUpdateRequest(BaseModel):
    scenario_name: str | None = Field(default=None, min_length=1, max_length=255)
    category: str | None = Field(default=None, max_length=80)
    description: str | None = Field(default=None, max_length=4000)
    enabled: bool | None = None
    schema_definition: dict[str, Any] | None = Field(default=None, alias="schema_json")


class ScenarioEnabledRequest(BaseModel):
    enabled: bool


class ScenarioPublishRequest(BaseModel):
    expected_revision: int = Field(ge=0)
    change_summary: str | None = Field(default=None, max_length=500)
