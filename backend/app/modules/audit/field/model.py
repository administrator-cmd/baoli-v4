"""模型定义，由 entity/tasks.py 按实体拆分。"""

from typing import Any

from sqlalchemy import (
    CHAR,
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    Computed,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.base_model import TimestampMixin
from app.core.database import Base


class TaskFieldValue(Base, TimestampMixin):
    """字段事实的版本与当前位索引；取值本身存在本次任务的 CSV。

    这里只回答「哪个任务、哪个字段、哪个材料位的哪一版是当前」，
    业务取值（抽取原文与规范值）由 `services.audit.value_store` 按 id 从任务 CSV 取。
    事实不可变：重跑不覆盖旧行，而是新增一个 revision_no 并把它置为当前；
    旧行保留为历史，旧复核仍然指向它们当时依据的那一版事实。
    同一任务同一字段同一材料位最多只有一个当前事实，由生成列唯一键在数据库层保证。
    """

    __tablename__ = "task_field_value"
    __table_args__ = (
        UniqueConstraint(
            "audit_task_id",
            "field_key",
            "material_key",
            "document_upload_id",
            "revision_no",
        ),
        UniqueConstraint("audit_task_id", "current_fact_scope"),
        UniqueConstraint("id", "audit_task_id"),
        ForeignKeyConstraint(
            ["audit_task_id", "scenario_version_id"],
            ["audit_task.id", "audit_task.scenario_version_id"],
            name="fk_task_field_value_task_version",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["audit_task_id", "material_key"],
            ["task_document.audit_task_id", "task_document.material_key"],
            name="fk_task_field_value_task_material",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["document_upload_id", "audit_task_id"],
            ["document_upload.id", "document_upload.audit_task_id"],
            name="fk_task_field_value_upload_task",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["extraction_execution_id", "audit_task_id"],
            ["node_execution.id", "node_execution.audit_task_id"],
            name="fk_task_field_value_execution_task",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        Index("idx_task_field_lookup", "audit_task_id", "field_key", "material_key", "document_upload_id"),
        Index("idx_task_field_version", "audit_task_id", "scenario_version_id"),
        Index("idx_task_field_material", "audit_task_id", "material_key"),
        Index("idx_task_field_current", "audit_task_id", "is_current"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    audit_task_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    scenario_version_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    field_key: Mapped[str] = mapped_column(String(40), nullable=False)
    material_key: Mapped[str] = mapped_column(String(40), nullable=False)
    document_upload_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    revision_no: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default=text("1"))
    is_current: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default=text("1"))
    status: Mapped[str] = mapped_column(String(40), nullable=False)
    # 产生这条事实的执行批次；人工纠错产生的版本没有抽取执行，记为 NULL。
    extraction_execution_id: Mapped[int | None] = mapped_column(BigInteger)
    # 事实来源：DOCUMENT_EXTRACTION=模型抽取；HUMAN_CORRECTION=人工主动纠错。
    origin: Mapped[str] = mapped_column(String(30), nullable=False, default="DOCUMENT_EXTRACTION", server_default=text("'DOCUMENT_EXTRACTION'"))
    # 当前事实的唯一定位：只有 is_current=1 的行才有值，因此唯一键对历史版本不生效。
    current_fact_scope: Mapped[str | None] = mapped_column(
        String(120),
        Computed(
            "IF(is_current = 1, CONCAT(CHAR_LENGTH(field_key), ':', field_key,"
            " CHAR_LENGTH(material_key), ':', material_key), NULL)",
            persisted=False,
        ),
        nullable=True,
    )


class TaskFact(Base, TimestampMixin):
    """统一事实的身份、口径与当前位索引；取值本身存在本次任务的 CSV。

    与 `task_field_value` 的抽取候选分开，统一表达字段事实和计算结果。
    事实身份包含来源编号、材料、期间、报表口径与主体范围，定位口径来自业务配置，
    不涉及时显式声明不适用，不根据场景名称补值。

    事实不可变：重跑不覆盖旧行，而是新增 `revision_no` 并置为当前；旧行保留为历史，
    旧复核仍指向它们当时依据的那一版事实。同一 (任务, 事实编号, 材料位, 期间,
    报表口径, 主体范围) 最多只有一条当前事实，由生成列唯一键在数据库层保证。

    业务取值、单位与币种、计算判断由 `services.audit.value_store` 按 id 从任务 CSV 取。
    """

    __tablename__ = "task_fact"
    __table_args__ = (
        UniqueConstraint(
            "audit_task_id",
            "fact_code",
            "material_scope",
            "period_scope",
            "statement_scope",
            "entity_scope",
            "revision_no",
        ),
        UniqueConstraint("audit_task_id", "current_fact_scope"),
        UniqueConstraint("id", "audit_task_id"),
        CheckConstraint("revision_no >= 1"),
        CheckConstraint("material_key IS NULL OR material_key <> ''"),
        ForeignKeyConstraint(
            ["audit_task_id", "scenario_version_id"],
            ["audit_task.id", "audit_task.scenario_version_id"],
            name="fk_task_fact_task",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["audit_task_id", "material_key"],
            ["task_document.audit_task_id", "task_document.material_key"],
            name="fk_task_fact_material",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["source_execution_id", "audit_task_id"],
            ["node_execution.id", "node_execution.audit_task_id"],
            name="fk_task_fact_execution_task",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        Index(
            "idx_task_fact_lookup",
            "audit_task_id",
            "fact_code",
            "period_scope",
            "statement_scope",
            "entity_scope",
        ),
        Index("idx_task_fact_kind", "audit_task_id", "fact_kind", "is_current"),
        Index("idx_task_fact_current", "audit_task_id", "is_current"),
        Index("idx_task_fact_rule", "audit_task_id", "calculation_rule_code"),
        Index("idx_task_fact_material", "audit_task_id", "material_key"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    audit_task_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    scenario_version_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    # 业务事实编号：EXTRACTED 是字段编号，DERIVED 是计算结果编号。
    fact_code: Mapped[str] = mapped_column(String(80), nullable=False)
    fact_kind: Mapped[str] = mapped_column(String(20), nullable=False)
    material_key: Mapped[str | None] = mapped_column(String(40))
    entity_scope: Mapped[str] = mapped_column(String(20), nullable=False, default="SELF", server_default=text("'SELF'"))
    period_scope: Mapped[str] = mapped_column(String(30), nullable=False)
    statement_scope: Mapped[str] = mapped_column(String(20), nullable=False)
    value_type: Mapped[str] = mapped_column(String(20), nullable=False)
    source_execution_id: Mapped[int | None] = mapped_column(BigInteger)
    calculation_rule_code: Mapped[str | None] = mapped_column(String(80))
    revision_no: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default=text("1"))
    is_current: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default=text("1"))
    status: Mapped[str] = mapped_column(String(40), nullable=False)
    # 唯一键不比较 NULL，用空串代替以覆盖无材料归属的计算事实。
    material_scope: Mapped[str | None] = mapped_column(
        String(40), Computed("IFNULL(material_key, '')", persisted=False), nullable=True
    )
    # 长度前缀编码区分分隔符和 NULL，避免不同完整身份拼出同一个唯一键。
    current_fact_scope: Mapped[str | None] = mapped_column(
        String(255),
        Computed(
            "IF(is_current = 1, CONCAT(CHAR_LENGTH(fact_code), ':', fact_code,"
            " IF(material_key IS NULL, '-1:', CONCAT(CHAR_LENGTH(material_key), ':', material_key)),"
            " CHAR_LENGTH(period_scope), ':', period_scope,"
            " CHAR_LENGTH(statement_scope), ':', statement_scope,"
            " CHAR_LENGTH(entity_scope), ':', entity_scope), NULL)",
            persisted=False,
        ),
        nullable=True,
    )


class ManualReview(Base, TimestampMixin):
    """一条字段人工裁决记录。

    system_judgment_json 是提交时的不可变系统快照；人工结论与依据分开保存：
    - decision / business_note / before_value / after_value：真实决定了什么、改了什么；
    - basis_type：这条人工结论的依据类型（人工说明 / 文档证据 / 上传记录），
      人工值不以旧引文冒充新值证明，因此依据类型必须与文档证据分开记录。
    """

    __tablename__ = "manual_review"
    __table_args__ = (
        ForeignKeyConstraint(
            ["field_value_id", "audit_task_id"],
            ["task_field_value.id", "task_field_value.audit_task_id"],
            name="fk_manual_review_field_value_task",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        Index("idx_manual_review_task_status", "audit_task_id", "status"),
        Index("idx_manual_review_field_value", "field_value_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    review_no: Mapped[str] = mapped_column(String(40), unique=True, nullable=False)
    audit_task_id: Mapped[int] = mapped_column(ForeignKey("audit_task.id", name="fk_manual_review_task", ondelete="RESTRICT", onupdate="RESTRICT"), nullable=False)
    field_value_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False)
    system_judgment_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    decision: Mapped[str | None] = mapped_column(String(40))
    business_note: Mapped[str | None] = mapped_column(Text)
    # 修改前后的真实值：只保存数量无法说明人工到底改了什么。
    before_value: Mapped[str | None] = mapped_column(Text)
    after_value: Mapped[str | None] = mapped_column(Text)
    # HUMAN_STATEMENT=人工业务判断；DOCUMENT_EVIDENCE=人工指定了文档证据；
    # UPLOAD_RECORD=依据真实上传记录（例如材料存在）。
    basis_type: Mapped[str | None] = mapped_column(String(30))
    # 产生这次裁决的后台命令编号，用于把裁决与命令对账。
    decision_command_no: Mapped[str | None] = mapped_column(String(40))


class FieldReviewConfirmation(Base, TimestampMixin):
    """字段核对确认：某一批次在某一版输入上被人工确认继续审核。

    确认不可变：字段事实或材料版本再次变化后，旧确认仍保留为历史，但不再代表新输入。
    `field_digest` 是确认当时的字段版本摘要（字段、材料、上传版本、事实版本与状态的组合），
    续跑与任务详情据此判断「当前字段是否已被确认」，不按时间或人工修正标记猜测。

    本次不引入认证能力：没有真实用户身份时 `confirmed_by` 为空，不虚构核对人。
    """

    __tablename__ = "field_review_confirmation"
    __table_args__ = (
        UniqueConstraint("confirmation_no", name="uq_field_review_no"),
        ForeignKeyConstraint(
            ["node_execution_id", "audit_task_id"],
            ["node_execution.id", "node_execution.audit_task_id"],
            name="fk_field_review_execution_task",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        Index("idx_field_review_task", "audit_task_id", "node_execution_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    confirmation_no: Mapped[str] = mapped_column(String(40), nullable=False)
    audit_task_id: Mapped[int] = mapped_column(
        ForeignKey("audit_task.id", name="fk_field_review_task", ondelete="RESTRICT", onupdate="RESTRICT"),
        nullable=False,
    )
    node_execution_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    # 确认所依据的任务输入修订号；材料版本或人工有效值变化会递增它。
    input_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    field_digest: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    # 确认时仍未取到值的字段：允许用户明确带缺项继续，但必须记下确认了什么。
    missing_field_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default=text("0"))
    missing_field_keys: Mapped[list[Any] | None] = mapped_column(JSON)
    note: Mapped[str | None] = mapped_column(Text)
    # 产生本次确认的后台命令编号，用于把确认与命令对账。
    operation_no: Mapped[str | None] = mapped_column(String(40))
