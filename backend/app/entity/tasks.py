from datetime import datetime
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

from ..core.database import Base
from .base import TimestampMixin, UTCDateTime


class AuditTask(Base, TimestampMixin):
    __tablename__ = "audit_task"
    __table_args__ = (
        UniqueConstraint("id", "scenario_version_id"),
        ForeignKeyConstraint(
            ["scenario_version_id", "current_node"],
            ["scenario_node.scenario_version_id", "scenario_node.node_code"],
            name="fk_audit_task_current_node",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        Index("idx_audit_task_version_status", "scenario_version_id", "status"),
        Index("idx_audit_task_plan", "plan_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    task_no: Mapped[str] = mapped_column(String(40), unique=True, nullable=False)
    scenario_version_id: Mapped[int] = mapped_column(ForeignKey("scenario_version.id", name="fk_audit_task_scenario_version", ondelete="RESTRICT", onupdate="RESTRICT"), nullable=False)
    # 任务创建时使用的审核方案。
    plan_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("audit_plan.id", name="fk_audit_task_plan", ondelete="RESTRICT", onupdate="RESTRICT"))
    task_name: Mapped[str] = mapped_column(String(255), nullable=False)
    # 任务备注：属于可编辑的基本资料，不进入执行快照。
    remark: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(40), nullable=False)
    current_node: Mapped[str] = mapped_column(String(80), nullable=False)
    # 本任务的执行快照：方案的编译产物整份冻结（材料/字段/规则/报告章节/节点）。
    # 执行期只读它，不读场景版本、也不读方案当前配置——方案后来怎么改都不影响已建任务。
    manifest_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    # 本任务的规则范围（启用/未启用规则与字段清单）。从上面的 manifest 算出。
    task_schema_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    # 业务 CSV 清单：format_version、batches（只登记 current/files/record_count）。
    # 由 services.audit.value_store 读写。
    value_file_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    # 任务输入修订号：首次补传、替换材料和人工有效值变化都会递增。
    # 写入口在对象锁内核对命令携带的修订号，过期命令不得覆盖新输入。
    input_revision: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default=text("0"))



class TaskDocument(Base, TimestampMixin):
    __tablename__ = "task_document"
    __table_args__ = (
        UniqueConstraint("audit_task_id", "material_key"),
        UniqueConstraint("id", "audit_task_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    document_no: Mapped[str] = mapped_column(String(40), unique=True, nullable=False)
    audit_task_id: Mapped[int] = mapped_column(ForeignKey("audit_task.id", name="fk_task_document_task", ondelete="RESTRICT", onupdate="RESTRICT"), nullable=False)
    material_key: Mapped[str] = mapped_column(String(40), nullable=False)
    material_name: Mapped[str] = mapped_column(String(255), nullable=False)
    required: Mapped[bool] = mapped_column(Boolean, nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False)


class DocumentUpload(Base, TimestampMixin):
    __tablename__ = "document_upload"
    __table_args__ = (
        UniqueConstraint("task_document_id", "upload_no"),
        UniqueConstraint("id", "audit_task_id"),
        UniqueConstraint("id", "task_document_id"),
        UniqueConstraint("current_task_document_id"),
        ForeignKeyConstraint(
            ["task_document_id", "audit_task_id"],
            ["task_document.id", "task_document.audit_task_id"],
            name="fk_document_upload_task_document_task",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        CheckConstraint("size_bytes >= 0"),
        CheckConstraint("is_current IN (0, 1)"),
        Index("idx_document_upload_document_current", "task_document_id", "is_current"),
        Index("idx_document_upload_sha256", "sha256"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    upload_no: Mapped[str] = mapped_column(String(40), nullable=False)
    task_document_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    audit_task_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    original_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    relative_path: Mapped[str] = mapped_column(String(500), nullable=False)
    content_type: Mapped[str] = mapped_column(String(120), nullable=False)
    # 上传时由用户从 8018 支持目录中选择的材料类型，只在本次上传内有效：
    # 替换文件时可以改成另一个类型，历史上传版本各自保留当时声明的值。
    # 它是提交给 8018 /validate 的 doc_type（见 services/audit/quality._document_prepare）。
    declared_material_type: Mapped[str] = mapped_column(String(80), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sha256: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False)
    is_current: Mapped[bool] = mapped_column(Boolean, nullable=False)
    current_task_document_id: Mapped[int | None] = mapped_column(
        BigInteger,
        Computed("IF(is_current = 1, task_document_id, NULL)", persisted=True),
        nullable=True,
    )


class StoredArtifact(Base, TimestampMixin):
    __tablename__ = "stored_artifact"
    __table_args__ = (
        UniqueConstraint("document_upload_id", "artifact_type", name="uq_stored_artifact_upload_type"),
        Index("idx_stored_artifact_content", "document_upload_id", "artifact_type", "content_sha256"),
        UniqueConstraint("id", "audit_task_id"),
        ForeignKeyConstraint(
            ["document_upload_id", "audit_task_id"],
            ["document_upload.id", "document_upload.audit_task_id"],
            name="fk_stored_artifact_upload_task",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        Index("idx_stored_artifact_upload_status", "document_upload_id", "status"),
        Index("idx_stored_artifact_status", "status", "updated_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    artifact_no: Mapped[str] = mapped_column(String(40), unique=True, nullable=False)
    document_upload_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    audit_task_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    artifact_type: Mapped[str] = mapped_column(String(40), nullable=False)
    relative_path: Mapped[str] = mapped_column(String(500), nullable=False)
    # 产物内容版本：产物实际内容的哈希（JSON 规范序列化或二进制字节）。
    # 同一份上传重新识别后内容可能不同，内容哈希用于判断产物是否更新。
    content_sha256: Mapped[str | None] = mapped_column(CHAR(64))
    status: Mapped[str] = mapped_column(String(30), nullable=False)
    # 外部作业号与截止时间；结果未知时保留最后一次已受理的作业。
    external_job_id: Mapped[str | None] = mapped_column(String(120))
    deadline_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    failure_code: Mapped[str | None] = mapped_column(String(80))
    failure_message: Mapped[str | None] = mapped_column(Text)
    # 只保存 processing_attempts / submit_attempts 等调用摘要；产物正文在 relative_path。
    metadata_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)


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


class RuleResult(Base, TimestampMixin):
    """一次规则执行的身份、状态与诊断；业务结论存在本次任务的 CSV。

    普通规则的 `rule_key` 是配置中的稳定键，计算结果的 `rule_key` 是计算业务编号。
    两者可能重名，结果须结合所属节点执行定位；数据库唯一键为
    (`node_execution_id`, `rule_key`)，不能按裸 `rule_key` 跨节点合并。

    `status`、`message`、`risk_level`、`rule_role` 作为列表与风险筛选的索引列；
    CSV 只保存处置、建议、依据与展示协议，不重复保存这几项。
    """

    __tablename__ = "rule_result"
    __table_args__ = (
        UniqueConstraint("node_execution_id", "rule_key"),
        UniqueConstraint("id", "audit_task_id"),
        ForeignKeyConstraint(
            ["node_execution_id", "audit_task_id"],
            ["node_execution.id", "node_execution.audit_task_id"],
            name="fk_rule_result_execution_task",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        Index("idx_rule_result_task_status", "audit_task_id", "status"),
        Index("idx_rule_result_task_rule", "audit_task_id", "rule_key"),
        Index("idx_rule_result_task_code", "audit_task_id", "rule_code"),
        Index("idx_rule_result_task_risk_level", "audit_task_id", "risk_level"),
        # 风险结果列表跨任务筛选角色，规则角色必须做前导列。
        Index("idx_rule_result_role_task", "rule_role", "audit_task_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    audit_task_id: Mapped[int] = mapped_column(ForeignKey("audit_task.id", name="fk_rule_result_task", ondelete="RESTRICT", onupdate="RESTRICT"), nullable=False)
    node_execution_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    rule_key: Mapped[str] = mapped_column(String(80), nullable=False)
    rule_code: Mapped[str | None] = mapped_column(String(80))
    rule_name: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(40), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    # 风险等级：风险结果列表按它筛选与排序，因此单独成列而不是压进 JSON。
    risk_level: Mapped[str | None] = mapped_column(String(40))
    # 规则角色 RISK / CHECK：取自任务清单里该条规则的同一判定。
    # 风险结果列表按它过滤，不必为每一行解析整份 manifest_json。
    rule_role: Mapped[str] = mapped_column(String(20), nullable=False)
    # 单条规则的模型循环结局与轮次。
    loop_status: Mapped[str | None] = mapped_column(String(30))
    turn_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default=text("0"))
    # 模型原始响应文件，相对 data 根目录。
    # 小体积执行诊断：审核标准、输入引用、未解决事项；不含模型原文与业务取值。
    result_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)


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




class NodeExecution(Base, TimestampMixin):
    __tablename__ = "node_execution"
    __table_args__ = (
        UniqueConstraint("audit_task_id", "sequence_no"),
        UniqueConstraint("id", "audit_task_id"),
        ForeignKeyConstraint(
            ["audit_task_id", "scenario_version_id"],
            ["audit_task.id", "audit_task.scenario_version_id"],
            name="fk_node_execution_task_version",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["scenario_version_id", "node_code"],
            ["scenario_node.scenario_version_id", "scenario_node.node_code"],
            name="fk_node_execution_version_node",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        CheckConstraint("sequence_no >= 1"),
        Index("idx_node_execution_task_node", "audit_task_id", "node_code"),
        Index("idx_node_execution_task_status", "audit_task_id", "status"),
        Index("idx_node_execution_display_status", "status", "invalidated_from"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    audit_task_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    scenario_version_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    node_code: Mapped[str] = mapped_column(String(80), nullable=False)
    sequence_no: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    result_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    failure_code: Mapped[str | None] = mapped_column(String(80))
    failure_message: Mapped[str | None] = mapped_column(Text)
    # 被重试或换版取代时保留原状态与原因。
    invalidated_from: Mapped[str | None] = mapped_column(String(30))
    invalidated_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    invalidation_reason: Mapped[str | None] = mapped_column(String(200))

