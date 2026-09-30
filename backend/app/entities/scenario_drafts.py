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
    revision_no: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default=text("0"))
    source: Mapped[str] = mapped_column(String(20), nullable=False, default="FORM", server_default=text("'FORM'"))
    base_version_no: Mapped[int | None] = mapped_column(Integer)
    # 配置草稿的来源格式版本（contract_review_v1 / v2 / v3）。页面表单没有这个版本概念，
    # 因此它由规则库导入、发布快照继承或复制从原始 manifest 继承：发布时按它导出，
    # 不能一律用默认版本给所有草稿打戳，那样会把旧格式配置说成节点版格式。
    profile_version: Mapped[str | None] = mapped_column(String(40))
    # 最近一次保存时草稿还不能发布的原因。NULL 表示当前草稿可以通过发布检查，
    # 不是「已经发布」：已发布版本只在显式发布时产生。
    sync_error: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    # 计算规则候选，或历史版本留下的 {"frozen": [已编译计算规则]}。
    # NULL 与空数组都表示没有计算配置。
    calculation_json: Mapped[Any | None] = mapped_column(JSON)
    # 停用行允许保留未填写的业务列，不能强行塞进完整业务对象表。
    disabled_source_rows: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON)


class ScenarioDraftNode(Base, TimestampMixin):
    """场景配置草稿里的**节点实例**：用户配置的流程节点。

    节点的存在、顺序与依赖由用户配置，不由系统按规则内容或固定序列自动生成。
    一个场景可以有两个同类节点（例如两张报表各一个字段抽取），因此
    `node_code` 是**实例码**，`node_type` 才是能力类型——两者不能混用。

    `depends_on` 保存上游**实例码**列表：依赖表达的是数据流，必须指向真实存在的实例。
    发布期校验缺失依赖、自依赖、循环依赖与顺序冲突，运行期不再遇到这类问题。

    节点与 Sheet 的关系是**一对多**：一个节点可以绑定多张 Sheet
    （财报的计算三表都绑到同一个 CALCULATION 实例上），一张 Sheet 只能绑定一个节点
    （见 `import_sheet.bound_node_code`）。
    """

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
    # 能力类型：来自统一模板协议的 node_types，决定谁执行这个节点。
    node_type: Mapped[str] = mapped_column(String(40), nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    sequence_no: Mapped[int] = mapped_column(Integer, nullable=False)
    # 上游节点**实例码**列表。
    depends_on: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    # 节点级配置（例如计算节点参与的计算规则编号）。为空表示该节点没有额外配置。
    config_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default=text("1"))
    sort_no: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default=text("0"))


class ScenarioDraftSheet(Base, TimestampMixin):
    """场景**选用**的 Sheet：这个场景用统一模板协议里的哪几张表，每张归哪个节点。

    统一模板协议定义「系统支持哪些 Sheet 以及每张表的结构」；场景负责配置
    「使用哪些 Sheet、Sheet 对应哪些节点及其流程关系」。这张表就是后者的落点。

    没有它，模板下载只能按草稿里已有内容反推（有材料→材料清单表，有规则→按节点类型
    反查），于是「本场景选了哪些表」没有权威来源：反推区分不了合同版与财报版的同名
    Sheet，也会把用户明确排除的表重新算进来。

    一个节点可以绑多张 Sheet（财报的计算三表绑到同一个计算节点实例上），
    一张 Sheet 只能绑一个节点——与 `import_sheet.bound_node_code` 同一口径。
    """

    __tablename__ = "scenario_draft_sheet"
    __table_args__ = (
        UniqueConstraint("draft_id", "sheet_role"),
        Index("idx_scenario_draft_sheet_draft", "draft_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    draft_id: Mapped[int] = mapped_column(ForeignKey("scenario_draft.id"), nullable=False)
    # Sheet 角色：来自统一模板协议的 sheet_role。
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
    # 取数期间与报表口径：**由字段自己声明**，计算参数按这两项定位字段事实
    # （参数表不再声明它们，因此这里是唯一的来源）。必填、无默认值——
    # 默认成「本期 / 合并」会让一个声明了上期的计算参数静默取到本期的数，
    # 而结果看起来完全正常。
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
    node_code: Mapped[str] = mapped_column(String(40), nullable=False)
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

    节点版（contract_review_v3）的报告配置一行一个章节，并自带报告编号、报告名称、
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
    # 以下为节点版报告分组属性；旧格式留空。
    report_code: Mapped[str | None] = mapped_column(String(80))
    report_name: Mapped[str | None] = mapped_column(String(255))
    material_keys: Mapped[list[str] | None] = mapped_column(JSON)
    formats: Mapped[list[str] | None] = mapped_column(JSON)
    section_order: Mapped[int | None] = mapped_column(Integer)
    content_type: Mapped[str | None] = mapped_column(String(40))
    result_sort: Mapped[str | None] = mapped_column(String(60))
    # 业务配置状态（已明确 / 待确认 / 停用）：待确认的非停用章节属于未完成配置。
    config_status: Mapped[str | None] = mapped_column(String(30))
    origin_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)

