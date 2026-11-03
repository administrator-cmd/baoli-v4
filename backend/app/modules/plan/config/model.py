"""方案与方案配置的模型定义。"""

from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.base_model import ConfigurationShape, TimestampMixin


class PlanMaterial(ConfigurationShape, TimestampMixin):
    """方案自己的材料位。创建方案时从钉住的场景版本复制一份，之后由用户在方案里增删改。

    形状与 `ScenarioDraftMaterial` 一致，只是归属换成 `plan_id`：方案是场景版本的**可编辑
    副本**，配置本身归方案所有，不再随场景版本变化。
    """

    __tablename__ = "plan_material"
    __table_args__ = (
        UniqueConstraint("plan_id", "material_key"),
        CheckConstraint("sort_no >= 0"),
        Index("idx_plan_material_plan", "plan_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    plan_id: Mapped[int] = mapped_column(ForeignKey("audit_plan.id"), nullable=False)
    material_key: Mapped[str] = mapped_column(String(40), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    required: Mapped[bool] = mapped_column(Boolean, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default=text("('')"))
    # 来源业务编号（Excel 的「材料编号」列）：导出时原样写回，页面新建的行留空。
    source_ref: Mapped[str | None] = mapped_column(String(80))
    origin_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    sort_no: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default=text("0"))


class PlanField(ConfigurationShape, TimestampMixin):
    """方案自己的字段定义。列语义与 `ScenarioDraftField` 完全一致。"""

    __tablename__ = "plan_field"
    __table_args__ = (
        UniqueConstraint("plan_id", "field_key"),
        CheckConstraint("sort_no >= 0"),
        CheckConstraint("extraction_strategy IN ('RAW_TEXT', 'DETERMINISTIC', 'STRUCTURED')"),
        CheckConstraint(
            "(extraction_strategy = 'STRUCTURED' AND structure_schema_json IS NOT NULL) OR "
            "(extraction_strategy <> 'STRUCTURED' AND structure_schema_json IS NULL)"
        ),
        Index("idx_plan_field_plan", "plan_id"),
        Index("idx_plan_field_material", "plan_id", "material_key"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    plan_id: Mapped[int] = mapped_column(ForeignKey("audit_plan.id"), nullable=False)
    field_key: Mapped[str] = mapped_column(String(40), nullable=False)
    material_key: Mapped[str] = mapped_column(String(40), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    value_type: Mapped[str] = mapped_column(String(30), nullable=False)
    extraction_strategy: Mapped[str] = mapped_column(String(30), nullable=False)
    # 与场景草稿字段保持一致，非结构化字段写 SQL NULL。
    structure_schema_json: Mapped[dict[str, Any] | None] = mapped_column(JSON(none_as_null=True))
    unit: Mapped[str | None] = mapped_column(String(40))
    # 币种留空表示未声明，不表示人民币。
    currency: Mapped[str | None] = mapped_column(String(40))
    # 产物来源：该字段的取值由哪种处理产物提供（当前仅印章识别 SEAL）；留空表示常规抽取。
    artifact_source: Mapped[str | None] = mapped_column(String(30))
    # 取数期间与报表口径：由字段自己声明，计算参数按这两项定位字段事实。
    # 留空（Excel 里不填）落成显式的「不适用」取值，见 domain/audit_vocab。
    period_scope: Mapped[str] = mapped_column(String(30), nullable=False)
    statement_scope: Mapped[str] = mapped_column(String(20), nullable=False)
    # 主体范围：未声明时为 SELF。
    entity_scope: Mapped[str] = mapped_column(String(20), nullable=False, default="SELF", server_default=text("'SELF'"))
    required: Mapped[bool] = mapped_column(Boolean, nullable=False)
    instruction: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default=text("('')"))
    source_ref: Mapped[str | None] = mapped_column(String(80))
    origin_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    sort_no: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default=text("0"))


class PlanRule(ConfigurationShape, TimestampMixin):
    """方案自己的规则定义。

    `enabled` 就是「这条规则要不要在这个方案里跑」：用户在方案页勾选与取消，直接改它。
    不再区分「系统派生 / 业务规则」——用户在方案里看到的就是全部规则，逐条可勾可取消；
    取消一条抽取规则就是那个字段不抽，取消报告生成规则就是不生成报告。
    """

    __tablename__ = "plan_rule"
    __table_args__ = (
        UniqueConstraint("plan_id", "rule_key"),
        CheckConstraint("sort_no >= 0"),
        Index("idx_plan_rule_plan", "plan_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    plan_id: Mapped[int] = mapped_column(ForeignKey("audit_plan.id"), nullable=False)
    rule_key: Mapped[str] = mapped_column(String(40), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    node_code: Mapped[str] = mapped_column(String(80), nullable=False)
    operator_code: Mapped[str] = mapped_column(String(40), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default=text("1"))
    # 规则的业务定义：输入对象、审核要求、参数、适用条件、风险来源与等级建议。
    # 由 compile_form_manifest 编译成 manifest 规则。
    rule_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    source_ref: Mapped[str | None] = mapped_column(String(80))
    origin_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    sort_no: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default=text("0"))


class PlanSection(ConfigurationShape, TimestampMixin):
    """方案自己的报告章节。列语义与 `ScenarioDraftSection` 完全一致。"""

    __tablename__ = "plan_section"
    __table_args__ = (
        UniqueConstraint("plan_id", "section_key"),
        CheckConstraint("sort_no >= 0"),
        Index("idx_plan_section_plan", "plan_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    plan_id: Mapped[int] = mapped_column(ForeignKey("audit_plan.id"), nullable=False)
    section_key: Mapped[str] = mapped_column(String(40), nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    content_instruction: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default=text("('')"))
    field_keys: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    # 仅为 config_json 中的配置属性形状，不新增业务明细表。
    data_refs: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    sort_no: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default=text("0"))
    report_code: Mapped[str | None] = mapped_column(String(80))
    report_name: Mapped[str | None] = mapped_column(String(255))
    material_keys: Mapped[list[str] | None] = mapped_column(JSON)
    formats: Mapped[list[str] | None] = mapped_column(JSON)
    section_order: Mapped[int | None] = mapped_column(Integer)
    content_type: Mapped[str | None] = mapped_column(String(40))
    result_sort: Mapped[str | None] = mapped_column(String(60))
    # 配置对象仅序列化到 config_json，不创建业务明细表。
    risk_level_order: Mapped[list[str] | None] = mapped_column(JSON)
    config_status: Mapped[str | None] = mapped_column(String(30))
    origin_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
