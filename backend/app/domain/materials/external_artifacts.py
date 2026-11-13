"""外部处理产物在系统内部的信封结构。

这不是供应商响应：供应商返回的原始 JSON 从不原样保存或向上传递，
适配器负责把它归一化成这里的形状，业务层只读这个结构。

因此它属于内部契约而非集成细节，放在 domain 供两侧共同引用：
`integrations/*` 产出它，`modules/audit/*` 消费它；两边都不必依赖对方。
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ExternalEvidence(BaseModel):
    """所属处理产物中的一条引用原文。"""

    model_config = ConfigDict(extra="forbid")

    quote_text: str = Field(min_length=1)


class OcrPage(BaseModel):
    """归一化 OCR 页面；空白页的 text 可以为空字符串。"""

    model_config = ConfigDict(strict=True, extra="allow")

    page_no: int
    text: str


class OcrContent(BaseModel):
    """OCR 内容入口契约；页面损坏时拒绝读取，不跳过坏页。"""

    model_config = ConfigDict(strict=True, extra="allow")

    pages: list[OcrPage]


class ExternalArtifact(BaseModel):
    """产物信封：状态、结构化内容与证据条目。供应商载荷不逐字保存。"""

    model_config = ConfigDict(extra="forbid")

    status: str
    content: dict[str, Any]
    evidence: list[ExternalEvidence] = Field(default_factory=list)


@dataclass(frozen=True)
class ExternalServiceResult:
    """一次外部调用的结果：内部结构 + 需要落盘的二进制（如果有）。"""

    artifact: ExternalArtifact
    binary_content: bytes | None = None
    filename: str | None = None
    content_type: str | None = None


@dataclass(frozen=True)
class OcrJobSubmission:
    job_id: str
    deadline_at: datetime
