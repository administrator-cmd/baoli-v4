from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CHAR,
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
from sqlalchemy.dialects.mysql import DATETIME as MySQLDateTime
from sqlalchemy.orm import Mapped, mapped_column

from ..database import Base
from ..domain.configuration import empty_config, config_property
from .base import ConfigurationShape, TimestampMixin


class AuditPlan(Base, TimestampMixin):
    """审核方案：已发布场景版本的一份可编辑副本。

    创建方案时把那一版配置整份复制到 `config_json`，之后由用户增删改。节点与 Sheet 是场景级执行结构，只读地
    存在 `context_json` 里随方案一起冻结，用户在方案页只能看不能改。

    保存方案 = 用自己的配置重新编译并写 `manifest_json`，再按编译产物算
    任务范围写 `scope_json` / `scope_hash`；算不出来时保存照常成功，原因记在 `sync_error`，
    页面显示「待核对」。

    已建任务不受方案后续改动影响：任务创建时把方案的编译产物整份抓进
    `audit_task.manifest_json`，执行期只读它，不再读场景版本。
    """

    __tablename__ = "audit_plan"
    __table_args__ = (
        UniqueConstraint("plan_code"),
        Index("idx_audit_plan_scenario", "scenario_code"),
        Index("idx_audit_plan_deleted", "deleted_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    plan_code: Mapped[str] = mapped_column(String(80), nullable=False)
    plan_name: Mapped[str] = mapped_column(String(255), nullable=False)
    scenario_code: Mapped[str] = mapped_column(String(80), nullable=False)
    # 这份方案从哪一版场景复制而来。只是溯源：执行与范围都不再读它，
    # 场景后来发布新版本也不会改变本方案（要换基准请另建方案）。
    scenario_version_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("scenario_version.id"))
    description: Mapped[str | None] = mapped_column(Text)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default=text("1"))
    deleted_at: Mapped[datetime | None] = mapped_column(MySQLDateTime(fsp=6))
    # 场景级、方案里只读的那部分上下文：节点实例、选用 Sheet 与协议版本。
    # 它们不参与方案编辑，但编译（compile_form_manifest）必须有，因此随方案冻结一份。
    context_json = config_property("context")
    # 方案自己的动态材料、字段、规则与报告配置，不再写 plan_* 明细表。
    config_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=empty_config)
    # 方案的编译产物：报告渲染、任务冻结都读它。未编译成功时为空，原因见 sync_error。
    manifest_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    # 最近一次保存时范围未能生效的原因（编译未通过：没选规则、字段引用缺失等）。
    # NULL 表示当前范围已生效。这是可展示状态，不是静默降级。
    sync_error: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    # 范围指纹：内容没变时不重复写 scope_json。
    scope_hash: Mapped[str | None] = mapped_column(CHAR(64))
    # 保存时刻算好的范围快照；任务创建直接复用它，不重新解释选择。
    scope_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)


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

