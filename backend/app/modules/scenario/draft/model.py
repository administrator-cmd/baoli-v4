from dataclasses import dataclass, field
from typing import Any, ClassVar

from sqlalchemy import (
    JSON,
    BigInteger,
    CheckConstraint,
    ForeignKeyConstraint,
    Integer,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.base_model import TimestampMixin
from app.core.database import Base
from app.domain.configuration import config_property, empty_config


class ScenarioDraft(Base, TimestampMixin):
    """场景配置草稿（每个场景一条）。

    revision_no 是草稿修订号：材料/字段/规则/报告配置的每次写入都会递增，
    发布命令携带它，过期修订的发布会与保存内容不一致而被明确拒绝。
    source 记录草稿的来源（FORM=页面直接建立；EXCEL=规则库导入；COPY=复制；
    REVISION=草稿缺失时继承最新发布快照）；base_version_no 记录继承的版本号，
    没有基于任何版本时为 NULL——不伪造“第 0 版”。
    """

    __tablename__ = "scenario_draft"
    __table_args__ = (
        UniqueConstraint("scenario_code"),
        ForeignKeyConstraint(
            ["scenario_code"],
            ["scenario_template.scenario_code"],
            name="fk_scenario_draft_template",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        CheckConstraint("revision_no >= 0"),
        CheckConstraint("base_version_no IS NULL OR base_version_no >= 1"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    scenario_code: Mapped[str] = mapped_column(String(80), nullable=False)
    schema_key: Mapped[str] = mapped_column(String(80), nullable=False)
    schema_version: Mapped[str] = mapped_column(String(40), nullable=False)
    schema_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    # 动态业务配置的唯一存储：节点、材料、字段、规则、报告章节。
    config_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=empty_config)
    revision_no: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default=text("0"))
    source: Mapped[str] = mapped_column(String(20), nullable=False, default="FORM", server_default=text("'FORM'"))
    base_version_no: Mapped[int | None] = mapped_column(Integer)
    # 来源配置的执行契约版本身份，随导入、继承或复制保存。
    profile_version: Mapped[str | None] = mapped_column(String(40))
    # 最近一次保存时草稿还不能发布的原因。NULL 表示当前草稿可以通过发布检查，
    # 不是「已经发布」：已发布版本只在显式发布时产生。
    sync_error: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    # 原始计算定义列表。
    # NULL 与空数组都表示没有计算配置。
    calculation_json = config_property("calculation_json")
    # 停用行允许保留未填写的业务列，不能强行塞进完整业务对象表。


@dataclass(kw_only=True)
class ScenarioDraftNode:
    """nodes 配置值；只序列化到所属对象的 JSON。"""

    section: ClassVar[str] = "nodes"
    owner_key: ClassVar[str] = "draft_id"

    id: int | None = None
    draft_id: int
    node_code: str
    node_type: str
    name: str
    sequence_no: int
    depends_on: list[str] = field(default_factory=list)
    config_json: dict[str, Any] | None = None
    enabled: bool = True
    sort_no: int = 0


@dataclass(kw_only=True)
class ScenarioDraftMaterial:
    """materials 配置值；只序列化到所属对象的 JSON。"""

    section: ClassVar[str] = "materials"
    owner_key: ClassVar[str] = "draft_id"

    id: int | None = None
    draft_id: int
    material_key: str
    name: str
    required: bool
    description: str = ''
    source_ref: str | None = None
    origin_json: dict[str, Any] | None = None
    sort_no: int = 0


@dataclass(kw_only=True)
class ScenarioDraftField:
    """fields 配置值；只序列化到所属对象的 JSON。"""

    section: ClassVar[str] = "fields"
    owner_key: ClassVar[str] = "draft_id"

    id: int | None = None
    draft_id: int
    field_key: str
    material_key: str
    name: str
    value_type: str
    extraction_strategy: str
    structure_schema_json: dict[str, Any] | None = None
    unit: str | None = None
    currency: str | None = None
    artifact_source: str | None = None
    period_scope: str
    statement_scope: str
    entity_scope: str = 'SELF'
    required: bool
    instruction: str = ''
    source_ref: str | None = None
    origin_json: dict[str, Any] | None = None
    sort_no: int = 0


@dataclass(kw_only=True)
class ScenarioDraftRule:
    """rules 配置值；只序列化到所属对象的 JSON。"""

    section: ClassVar[str] = "rules"
    owner_key: ClassVar[str] = "draft_id"

    id: int | None = None
    draft_id: int
    rule_key: str
    name: str
    node_code: str
    operator_code: str
    enabled: bool = True
    rule_json: dict[str, Any]
    source_ref: str | None = None
    origin_json: dict[str, Any] | None = None
    sort_no: int = 0


@dataclass(kw_only=True)
class ScenarioDraftSection:
    """sections 配置值；只序列化到所属对象的 JSON。"""

    section: ClassVar[str] = "sections"
    owner_key: ClassVar[str] = "draft_id"

    id: int | None = None
    draft_id: int
    section_key: str
    title: str
    content_instruction: str = ''
    field_keys: list[str]
    data_refs: list[str] = field(default_factory=list)
    sort_no: int = 0
    report_code: str | None = None
    report_name: str | None = None
    material_keys: list[str] | None = None
    formats: list[str] | None = None
    section_order: int | None = None
    content_type: str | None = None
    result_sort: str | None = None
    risk_level_order: list[str] | None = None
    config_status: str | None = None
    origin_json: dict[str, Any] | None = None
