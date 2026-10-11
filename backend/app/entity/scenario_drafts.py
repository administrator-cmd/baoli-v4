from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from ..database import Base
from ..domain.configuration import empty_config, config_property
from .base import TimestampMixin


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
    # 来源配置的能力版本身份：导入、继承或复制时保留，不能覆盖已有历史身份。
    profile_version: Mapped[str | None] = mapped_column(String(40))
    # 最近一次保存时草稿还不能发布的原因。NULL 表示当前草稿可以通过发布检查，
    # 不是「已经发布」：已发布版本只在显式发布时产生。
    sync_error: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    # 计算规则候选，或历史版本留下的 {"frozen": [已编译计算规则]}。
    # NULL 与空数组都表示没有计算配置。
    calculation_json = config_property("calculation_json")
    # 停用行允许保留未填写的业务列，不能强行塞进完整业务对象表。
    disabled_source_rows = config_property("disabled_source_rows")


class ScenarioDraftNode(Base, TimestampMixin):
    """config_json 中的执行记录技术容器投影，不提供业务节点编排。"""

    __tablename__ = "scenario_draft_node"
    __table_args__ = (
        UniqueConstraint("draft_id", "node_code"),
        CheckConstraint("sequence_no >= 1"),
        Index("idx_scenario_draft_node_draft", "draft_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    draft_id: Mapped[int] = mapped_column(ForeignKey("scenario_draft.id"), nullable=False)
    # 节点实例码：同一场景内唯一，规则按它归属。
    node_code: Mapped[str] = mapped_column(String(80), nullable=False)
    # 技术类型 RULE_EXECUTION，不决定业务规则执行路径。
    node_type: Mapped[str] = mapped_column(String(40), nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    sequence_no: Mapped[int] = mapped_column(Integer, nullable=False)
    # 技术关联属性，统一容器没有上游业务节点。
    depends_on: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    # 技术执行记录的配置。
    config_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default=text("1"))
    sort_no: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default=text("0"))


class ScenarioDraftSheet(Base, TimestampMixin):
    """草稿来源工作表的内部投影；实际清单保存在 config_json。

    新来源保存上传文件的真实 sheet_key/sheet_name，业务配置各自引用节点，
    不按表名、表头或固定角色限制场景；node_code 只保留已有历史绑定数据。
    """

    __tablename__ = "scenario_draft_sheet"
    __table_args__ = (
        UniqueConstraint("draft_id", "sheet_role"),
        Index("idx_scenario_draft_sheet_draft", "draft_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    draft_id: Mapped[int] = mapped_column(ForeignKey("scenario_draft.id"), nullable=False)
    # 来源表标识：动态来源用 RULE_LIBRARY:<sheet_key>，历史记录保留已存标识。
    sheet_role: Mapped[str] = mapped_column(String(80), nullable=False)
    # 该 Sheet 归属的节点实例码。为空表示本场景选用这张表但尚未指定节点。
    node_code: Mapped[str | None] = mapped_column(String(80))
    sort_no: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default=text("0"))


class ScenarioDraftMaterial(Base, TimestampMixin):
    __tablename__ = "scenario_draft_material"
    __table_args__ = (
        UniqueConstraint("draft_id", "material_key"),
        CheckConstraint("sort_no >= 0"),
        Index("idx_scenario_draft_material_draft", "draft_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    draft_id: Mapped[int] = mapped_column(ForeignKey("scenario_draft.id"), nullable=False)
    material_key: Mapped[str] = mapped_column(String(40), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    required: Mapped[bool] = mapped_column(Boolean, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default=text("('')"))
    # 来源业务编号（Excel 的「材料编号」列）：导出时要原样写回，页面新建的行留空。
    source_ref: Mapped[str | None] = mapped_column(String(80))
    origin_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    sort_no: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default=text("0"))


class ScenarioDraftField(Base, TimestampMixin):
    __tablename__ = "scenario_draft_field"
    __table_args__ = (
        UniqueConstraint("draft_id", "field_key"),
        CheckConstraint("sort_no >= 0"),
        CheckConstraint("extraction_strategy IN ('RAW_TEXT', 'DETERMINISTIC', 'STRUCTURED')"),
        CheckConstraint(
            "(extraction_strategy = 'STRUCTURED' AND structure_schema_json IS NOT NULL) OR "
            "(extraction_strategy <> 'STRUCTURED' AND structure_schema_json IS NULL)"
        ),
        Index("idx_scenario_draft_field_draft", "draft_id"),
        Index("idx_scenario_draft_field_material", "draft_id", "material_key"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    draft_id: Mapped[int] = mapped_column(ForeignKey("scenario_draft.id"), nullable=False)
    field_key: Mapped[str] = mapped_column(String(40), nullable=False)
    material_key: Mapped[str] = mapped_column(String(40), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    value_type: Mapped[str] = mapped_column(String(30), nullable=False)
    extraction_strategy: Mapped[str] = mapped_column(String(30), nullable=False)
    # 非结构化字段必须落 SQL NULL；JSON 默认会把 Python None 编码成 JSON null，
    # 无法满足 chk_scenario_draft_field_structure 的 IS NULL 约束。
    structure_schema_json: Mapped[dict[str, Any] | None] = mapped_column(JSON(none_as_null=True))
    unit: Mapped[str | None] = mapped_column(String(40))
    # 币种是金额比较与加总的口径之一：留空表示未声明，不表示人民币。
    currency: Mapped[str | None] = mapped_column(String(40))
    # 产物来源：该字段的取值由哪种处理产物提供（当前仅印章识别 SEAL）。
    # 主体核对必须按这一声明确定「待核对主体取自哪份材料的哪种产物」，
    # 不能靠字段名猜。留空表示字段由常规字段抽取提供。
    artifact_source: Mapped[str | None] = mapped_column(String(30))
    # 取数期间与报表口径：业务涉及时由字段声明，计算参数按这两项定位事实；
    # 草稿 JSON 缺省时由编译层归一为 NOT_APPLICABLE。
    period_scope: Mapped[str] = mapped_column(String(30), nullable=False)
    statement_scope: Mapped[str] = mapped_column(String(20), nullable=False)
    required: Mapped[bool] = mapped_column(Boolean, nullable=False)
    instruction: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default=text("('')"))
    # 来源业务编号（Excel 的「字段编号」列）：导出时写回，页面新建的字段留空。
    source_ref: Mapped[str | None] = mapped_column(String(80))
    origin_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    sort_no: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default=text("0"))


class ScenarioDraftRule(Base, TimestampMixin):
    __tablename__ = "scenario_draft_rule"
    __table_args__ = (
        UniqueConstraint("draft_id", "rule_key"),
        CheckConstraint("sort_no >= 0"),
        Index("idx_scenario_draft_rule_draft", "draft_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    draft_id: Mapped[int] = mapped_column(ForeignKey("scenario_draft.id"), nullable=False)
    rule_key: Mapped[str] = mapped_column(String(40), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    node_code: Mapped[str] = mapped_column(String(80), nullable=False)
    operator_code: Mapped[str] = mapped_column(String(40), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default=text("1"))
    # 规则的业务定义：输入对象（材料/字段引用）、审核要求、参数、适用条件、
    # 风险来源规则与等级建议。发布时由 compile_form_manifest 编译成 manifest 规则。
    rule_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    # 来源业务编号（Excel 的「规则编号」列）：导出时写回，页面新建的规则留空。
    source_ref: Mapped[str | None] = mapped_column(String(80))
    origin_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    sort_no: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default=text("0"))


class ScenarioDraftSection(Base, TimestampMixin):
    """报告配置草稿：一个报告章节。

    场景报告配置按章节保存，并可声明报告编号、报告名称、
    材料范围、输出格式、结果排序与内容类型；这些都是业务维护的配置事实，必须随草稿
    一起保存，否则页面保存一次就会把按编号分组的报告退回成旧的附加展示章节。
    旧格式没有这些列，它们保持 NULL，渲染与导出继续按旧的附加章节语义处理。
    """

    __tablename__ = "scenario_draft_section"
    __table_args__ = (
        UniqueConstraint("draft_id", "section_key"),
        CheckConstraint("sort_no >= 0"),
        Index("idx_scenario_draft_section_draft", "draft_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    draft_id: Mapped[int] = mapped_column(ForeignKey("scenario_draft.id"), nullable=False)
    section_key: Mapped[str] = mapped_column(String(40), nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    content_instruction: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default=text("('')"))
    field_keys: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    sort_no: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default=text("0"))
    # 规则库报告分组属性。
    report_code: Mapped[str | None] = mapped_column(String(80))
    report_name: Mapped[str | None] = mapped_column(String(255))
    material_keys: Mapped[list[str] | None] = mapped_column(JSON)
    formats: Mapped[list[str] | None] = mapped_column(JSON)
    section_order: Mapped[int | None] = mapped_column(Integer)
    content_type: Mapped[str | None] = mapped_column(String(40))
    result_sort: Mapped[str | None] = mapped_column(String(60))
    # 配置对象仅序列化到 config_json，不创建业务明细表。
    risk_level_order: Mapped[list[str] | None] = mapped_column(JSON)
    # 业务配置状态（已明确 / 待确认 / 停用）：待确认的非停用章节属于未完成配置。
    config_status: Mapped[str | None] = mapped_column(String(30))
    origin_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)

