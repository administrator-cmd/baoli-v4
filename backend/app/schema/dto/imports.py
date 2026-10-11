
from pydantic import BaseModel, Field


class SheetBindingItem(BaseModel):
    sheet_key: str
    excluded: bool = False


class SheetBindingsRequest(BaseModel):
    bindings: list[SheetBindingItem]






class ApplyImportDraftRequest(BaseModel):
    expected_job_revision: int = Field(ge=0)
    expected_scene_revision: int = Field(ge=0)
