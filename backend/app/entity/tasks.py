from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CHAR,
    CheckConstraint,
    Computed,
    Date,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    JSON,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.mysql import DATETIME as MySQLDateTime
from sqlalchemy.orm import Mapped, mapped_column

from ..database import Base
from .base import TimestampMixin


class AuditTask(Base, TimestampMixin):
    __tablename__ = "audit_task"
    __table_args__ = (
        UniqueConstraint("id", "scenario_version_id"),
        ForeignKeyConstraint(
            ["scenario_version_id", "current_node"],
            ["scenario_node.scenario_version_id", "scenario_node.node_code"],
        ),
        Index("idx_audit_task_version_status", "scenario_version_id", "status"),
        Index("idx_audit_task_version_node", "scenario_version_id", "current_node"),
        Index("idx_audit_task_status_current_node", "status", "current_node"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    task_no: Mapped[str] = mapped_column(String(40), unique=True, nullable=False)
    scenario_version_id: Mapped[int] = mapped_column(ForeignKey("scenario_version.id"), nullable=False)
    # 任务创建时使用的审核方案。旧任务没有方案关联时为空，页面显示「历史任务配置」，
    # 不猜测迁移、不改写历史结果。
    plan_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("audit_plan.id"))
    task_name: Mapped[str] = mapped_column(String(255), nullable=False)
    # 任务备注：属于可编辑的基本资料，不进入执行快照。
    remark: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(40), nullable=False)
    current_node: Mapped[str] = mapped_column(String(80), nullable=False)
    thread_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    # 本任务的执行快照：方案的编译产物整份冻结（材料/字段/规则/报告章节/节点/Sheet）。
    # 执行期只读它，不读场景版本、也不读方案当前配置——方案后来怎么改都不影响已建任务。
    manifest_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    # 本任务的规则范围（启用/未启用规则与字段清单）。从上面的 manifest 算出。
    task_schema_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    checkpoint_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    pending_review_hash: Mapped[str | None] = mapped_column(CHAR(64))
    # 业务中间值 CSV 的已提交文件指针；事务回滚不覆盖上一份文件。
    intermediate_csv_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    # 任务输入修订号：首次补传、替换材料和人工有效值变化都会递增。
    # 写入口在对象锁内核对命令携带的修订号，过期命令不得覆盖新输入。
    input_revision: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default=text("0"))
    # 逻辑删除：历史事实、报告与文件保留；有活跃后台操作时拒绝删除。
    deleted_at: Mapped[datetime | None] = mapped_column(MySQLDateTime(fsp=6))


class WorkflowCheckpoint(Base, TimestampMixin):
    __tablename__ = "workflow_checkpoint"
    __table_args__ = (
        UniqueConstraint("audit_task_id", "checkpoint_ns"),
        Index("idx_workflow_checkpoint_thread_status", "thread_id", "status"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    audit_task_id: Mapped[int] = mapped_column(ForeignKey("audit_task.id"), nullable=False)
    thread_id: Mapped[str] = mapped_column(String(64), nullable=False)
    checkpoint_ns: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False)
    state_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)


class TaskDocument(Base, TimestampMixin):
    __tablename__ = "task_document"
    __table_args__ = (
        UniqueConstraint("audit_task_id", "material_key"),
        UniqueConstraint("id", "audit_task_id"),
        ForeignKeyConstraint(
            ["current_upload_id", "id"],
            ["document_upload.id", "document_upload.task_document_id"],
        ),
        Index("idx_task_document_current_upload", "current_upload_id", "id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    document_no: Mapped[str] = mapped_column(String(40), unique=True, nullable=False)
    audit_task_id: Mapped[int] = mapped_column(ForeignKey("audit_task.id"), nullable=False)
    material_key: Mapped[str] = mapped_column(String(40), nullable=False)
    material_name: Mapped[str] = mapped_column(String(255), nullable=False)
    required: Mapped[bool] = mapped_column(Boolean, nullable=False)
    current_upload_id: Mapped[int | None] = mapped_column(BigInteger)
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
        UniqueConstraint("document_upload_id", "artifact_type", "upload_sha256"),
        Index("idx_stored_artifact_content", "document_upload_id", "artifact_type", "content_sha256"),
        UniqueConstraint("id", "audit_task_id"),
        ForeignKeyConstraint(
            ["document_upload_id", "audit_task_id"],
            ["document_upload.id", "document_upload.audit_task_id"],
        ),
        Index("idx_stored_artifact_upload_status", "document_upload_id", "status"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    artifact_no: Mapped[str] = mapped_column(String(40), unique=True, nullable=False)
    document_upload_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    audit_task_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    artifact_type: Mapped[str] = mapped_column(String(40), nullable=False)
    relative_path: Mapped[str] = mapped_column(String(500), nullable=False)
    # 上传身份：这份产物是由哪个上传文件产生的（同一次上传重跑同一产物时不变）。
    upload_sha256: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    # 产物内容版本：产物实际内容的哈希（JSON 规范序列化或二进制字节）。
    # 它与上传哈希是两件事：同一份 PDF 重新识别后上传哈希不变，但内容可能不同；
    # 索引必须绑定内容哈希，否则旧主体向量会继续冒充当前产物。
    content_sha256: Mapped[str | None] = mapped_column(CHAR(64))
    status: Mapped[str] = mapped_column(String(30), nullable=False)
    metadata_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)


class TaskFieldValue(Base, TimestampMixin):
    """一次抽取（或一次人工纠错）产生的字段事实版本。

    事实不可变：重跑不覆盖旧行，而是新增一个 revision_no 并把它置为当前；
    旧行保留为历史，旧复核和旧证据仍然指向它们当时依据的那一版事实。
    同一任务同一字段同一材料位最多只有一个当前事实，由生成列唯一键在数据库层保证。
    """

    __tablename__ = "task_field_value"
    __table_args__ = (
        UniqueConstraint(
            "audit_task_id",
            "scenario_version_id",
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
        ),
        ForeignKeyConstraint(
            ["audit_task_id", "material_key"],
            ["task_document.audit_task_id", "task_document.material_key"],
        ),
        ForeignKeyConstraint(
            ["document_upload_id", "audit_task_id"],
            ["document_upload.id", "document_upload.audit_task_id"],
        ),
        ForeignKeyConstraint(
            ["evidence_id", "audit_task_id"],
            ["evidence.id", "evidence.audit_task_id"],
        ),
        ForeignKeyConstraint(
            ["extraction_execution_id", "audit_task_id"],
            ["node_execution.id", "node_execution.audit_task_id"],
        ),
        Index("idx_task_field_lookup", "audit_task_id", "scenario_version_id", "field_key", "material_key", "document_upload_id"),
        Index("idx_task_field_evidence", "evidence_id"),
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
    raw_value: Mapped[str | None] = mapped_column(Text)
    normalized_value_json: Mapped[Any | None] = mapped_column(JSON)
    evidence_id: Mapped[int | None] = mapped_column(BigInteger)
    # 产生这条事实的执行批次；人工纠错产生的版本没有抽取执行，记为 NULL。
    extraction_execution_id: Mapped[int | None] = mapped_column(BigInteger)
    # 事实来源：DOCUMENT_EXTRACTION=模型抽取；HUMAN_CORRECTION=人工主动纠错。
    origin: Mapped[str] = mapped_column(String(30), nullable=False, default="DOCUMENT_EXTRACTION", server_default=text("'DOCUMENT_EXTRACTION'"))
    # 当前事实的唯一定位：只有 is_current=1 的行才有值，因此唯一键对历史版本不生效。
    current_fact_scope: Mapped[str | None] = mapped_column(
        String(120),
        Computed("IF(is_current = 1, CONCAT(field_key, ':', material_key), NULL)", persisted=True),
        nullable=True,
    )


class TaskFact(Base, TimestampMixin):
    """统一事实：字段抽取事实与计算派生事实。

    与 `task_field_value` 的抽取候选分开，统一表达字段事实和计算结果。
    事实身份包含来源编号、材料、期间、报表口径与主体范围，定位口径来自业务配置，
    不涉及时显式声明不适用，不根据场景名称补值。

    事实不可变：重跑不覆盖旧行，而是新增 `revision_no` 并置为当前；旧行保留为历史，
    旧复核与旧证据仍指向它们当时依据的那一版事实。同一 (任务, 事实编号, 材料位, 期间,
    报表口径, 主体范围) 最多只有一条当前事实，由生成列唯一键在数据库层保证。

    计算派生事实的追溯：`input_snapshot_json` 记录本次计算用到的每一版事实
    （fact_id + revision_no）及实际输入信息；`judgement_json` 记录模型判断与业务结论。

    RAG 只保存证据、原文与检索信息，不是 `decimal_value` 的权威来源。
    """

    __tablename__ = "task_fact"
    __table_args__ = (
        UniqueConstraint(
            "audit_task_id",
            "fact_code",
            "material_key",
            "period_scope",
            "statement_scope",
            "entity_scope",
            "revision_no",
        ),
        UniqueConstraint("audit_task_id", "current_fact_scope"),
        UniqueConstraint("id", "audit_task_id"),
        CheckConstraint("revision_no >= 1"),
        ForeignKeyConstraint(
            ["audit_task_id", "scenario_version_id"],
            ["audit_task.id", "audit_task.scenario_version_id"],
        ),
        ForeignKeyConstraint(
            ["source_execution_id", "audit_task_id"],
            ["node_execution.id", "node_execution.audit_task_id"],
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
    # 数值事实用 DECIMAL(38, 10) 定点保存，不用浮点：float 的二进制误差会让
    # 「恰好等于阈值」的判定随机翻转。
    decimal_value: Mapped[Decimal | None] = mapped_column(Numeric(38, 10))
    text_value: Mapped[str | None] = mapped_column(Text)
    date_value: Mapped[date | None] = mapped_column(Date)
    boolean_value: Mapped[bool | None] = mapped_column(Boolean)
    unit: Mapped[str | None] = mapped_column(String(40))
    currency: Mapped[str | None] = mapped_column(String(40))
    source_execution_id: Mapped[int | None] = mapped_column(BigInteger)
    input_snapshot_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    judgement_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    calculation_status: Mapped[str | None] = mapped_column(String(30))
    calculation_rule_code: Mapped[str | None] = mapped_column(String(80))
    revision_no: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default=text("1"))
    is_current: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default=text("1"))
    status: Mapped[str] = mapped_column(String(40), nullable=False)
    # 当前事实的唯一定位：只有 is_current=1 的行才有值。material_key 可空，
    # CONCAT 会把 NULL 吞掉，因此用 COALESCE 归一成固定串。
    current_fact_scope: Mapped[str | None] = mapped_column(
        String(255),
        Computed(
            "IF(is_current = 1, CONCAT(fact_code, ':', COALESCE(material_key, '-'), ':',"
            " period_scope, ':', statement_scope, ':', entity_scope), NULL)",
            persisted=False,
        ),
        nullable=True,
    )


class RuleResult(Base, TimestampMixin):
    """一次规则执行的结果。

    普通规则的 `rule_key` 是配置中的稳定键，计算结果的 `rule_key` 是计算业务编号。
    两者可能重名，结果须结合所属节点执行定位；数据库唯一键为
    (`node_execution_id`, `rule_key`)，不能按裸 `rule_key` 跨节点合并。
    """

    __tablename__ = "rule_result"
    __table_args__ = (
        UniqueConstraint("node_execution_id", "rule_key"),
        UniqueConstraint("id", "audit_task_id"),
        ForeignKeyConstraint(
            ["node_execution_id", "audit_task_id"],
            ["node_execution.id", "node_execution.audit_task_id"],
        ),
        Index("idx_rule_result_task_status", "audit_task_id", "status"),
        Index("idx_rule_result_task_rule", "audit_task_id", "rule_key"),
        Index("idx_rule_result_task_code", "audit_task_id", "rule_code"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    audit_task_id: Mapped[int] = mapped_column(ForeignKey("audit_task.id"), nullable=False)
    node_execution_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    rule_key: Mapped[str] = mapped_column(String(80), nullable=False)
    rule_code: Mapped[str | None] = mapped_column(String(80))
    legacy_rule_key: Mapped[str | None] = mapped_column(String(40))
    rule_name: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(40), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    result_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)


class Evidence(Base, TimestampMixin):
    """一条证据引用。

    引用原文及所属材料/产物供内容核对使用。page_no、bbox_json、locator_status、
    locator_note 仅保留历史存储兼容；新证据不生成定位信息，接口不返回这些历史列。
    """

    __tablename__ = "evidence"
    __table_args__ = (
        UniqueConstraint("id", "audit_task_id"),
        ForeignKeyConstraint(
            ["document_upload_id", "audit_task_id"],
            ["document_upload.id", "document_upload.audit_task_id"],
        ),
        ForeignKeyConstraint(
            ["rule_result_id", "audit_task_id"],
            ["rule_result.id", "rule_result.audit_task_id"],
        ),
        ForeignKeyConstraint(
            ["field_value_id", "audit_task_id"],
            ["task_field_value.id", "task_field_value.audit_task_id"],
        ),
        ForeignKeyConstraint(
            ["artifact_id", "audit_task_id"],
            ["stored_artifact.id", "stored_artifact.audit_task_id"],
        ),
        CheckConstraint("document_upload_id IS NOT NULL OR rule_result_id IS NOT NULL OR field_value_id IS NOT NULL"),
        CheckConstraint("page_no IS NULL OR page_no >= 1"),
        CheckConstraint("field_path IS NULL OR field_value_id IS NOT NULL"),
        Index("idx_evidence_task", "audit_task_id"),
        Index("idx_evidence_upload", "document_upload_id"),
        Index("idx_evidence_rule_result", "rule_result_id"),
        Index("idx_evidence_field_value", "field_value_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    evidence_no: Mapped[str] = mapped_column(String(40), unique=True, nullable=False)
    audit_task_id: Mapped[int] = mapped_column(ForeignKey("audit_task.id"), nullable=False)
    document_upload_id: Mapped[int | None] = mapped_column(BigInteger)
    rule_result_id: Mapped[int | None] = mapped_column(BigInteger)
    field_value_id: Mapped[int | None] = mapped_column(BigInteger)
    # 结构化字段的顶层属性路径；普通字段与规则证据为空。
    field_path: Mapped[str | None] = mapped_column(String(255))
    page_no: Mapped[int | None] = mapped_column(Integer)
    quote_text: Mapped[str] = mapped_column(Text, nullable=False)
    bbox_json: Mapped[list[float] | None] = mapped_column(JSON)
    artifact_id: Mapped[int | None] = mapped_column(BigInteger)
    locator_status: Mapped[str] = mapped_column(
        String(30), nullable=False, default="UNVERIFIED", server_default=text("'UNVERIFIED'")
    )
    locator_note: Mapped[str | None] = mapped_column(Text)


class ManualReview(Base, TimestampMixin):
    """一条人工裁决记录。

    system_judgment_json 是提交时的不可变系统快照；人工结论与依据分开保存：
    - decision / business_note / before_value / after_value：真实决定了什么、改了什么；
    - basis_type：这条人工结论的依据类型（人工说明 / 文档证据 / 上传记录），
      人工值不以旧引文冒充新值证明，因此依据类型必须与文档证据分开记录。
    """

    __tablename__ = "manual_review"
    __table_args__ = (
        ForeignKeyConstraint(
            ["document_upload_id", "audit_task_id"],
            ["document_upload.id", "document_upload.audit_task_id"],
        ),
        ForeignKeyConstraint(
            ["rule_result_id", "audit_task_id"],
            ["rule_result.id", "rule_result.audit_task_id"],
        ),
        ForeignKeyConstraint(
            ["field_value_id", "audit_task_id"],
            ["task_field_value.id", "task_field_value.audit_task_id"],
        ),
        Index("idx_manual_review_task_status", "audit_task_id", "status"),
        Index("idx_manual_review_upload", "document_upload_id"),
        Index("idx_manual_review_rule_result", "rule_result_id"),
        Index("idx_manual_review_field_value", "field_value_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    review_no: Mapped[str] = mapped_column(String(40), unique=True, nullable=False)
    audit_task_id: Mapped[int] = mapped_column(ForeignKey("audit_task.id"), nullable=False)
    document_upload_id: Mapped[int | None] = mapped_column(BigInteger)
    rule_result_id: Mapped[int | None] = mapped_column(BigInteger)
    field_value_id: Mapped[int | None] = mapped_column(BigInteger)
    review_type: Mapped[str] = mapped_column(String(40), nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False)
    system_judgment_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    decision: Mapped[str | None] = mapped_column(String(40))
    business_note: Mapped[str | None] = mapped_column(Text)
    proposed_value: Mapped[str | None] = mapped_column(Text)
    # 修改前后的真实值：只保存数量无法说明人工到底改了什么。
    before_value: Mapped[str | None] = mapped_column(Text)
    after_value: Mapped[str | None] = mapped_column(Text)
    # HUMAN_STATEMENT=人工业务判断；DOCUMENT_EVIDENCE=人工指定了文档证据；
    # UPLOAD_RECORD=依据真实上传记录（例如材料存在）。
    basis_type: Mapped[str | None] = mapped_column(String(30))
    # 产生这次裁决的后台命令编号，用于把裁决与命令对账。
    decision_command_no: Mapped[str | None] = mapped_column(String(40))


class ReviewDraft(Base, TimestampMixin):
    """复核草稿：未提交的复核答案按“待办版本”持久化暂存。

    pending_hash 是保存草稿时任务的待办版本（audit_task.pending_review_hash）；
    读取只返回与当前待办版本一致的草稿——旧草稿不会套用到变化后的待办，
    提交（正式裁决）仍走后台命令，与草稿互不替代。
    """

    __tablename__ = "review_draft"
    __table_args__ = (
        UniqueConstraint("audit_task_id", "review_no"),
        Index("idx_review_draft_task_hash", "audit_task_id", "pending_hash"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    audit_task_id: Mapped[int] = mapped_column(ForeignKey("audit_task.id"), nullable=False)
    review_no: Mapped[str] = mapped_column(String(40), nullable=False)
    pending_hash: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    decision: Mapped[str | None] = mapped_column(String(40))
    business_note: Mapped[str | None] = mapped_column(Text)
    field_value: Mapped[str | None] = mapped_column(Text)
    basis_type: Mapped[str | None] = mapped_column(String(30))


class NodeExecution(Base, TimestampMixin):
    __tablename__ = "node_execution"
    __table_args__ = (
        UniqueConstraint("audit_task_id", "sequence_no"),
        UniqueConstraint("id", "audit_task_id"),
        ForeignKeyConstraint(
            ["audit_task_id", "scenario_version_id"],
            ["audit_task.id", "audit_task.scenario_version_id"],
        ),
        ForeignKeyConstraint(
            ["scenario_version_id", "node_code"],
            ["scenario_node.scenario_version_id", "scenario_node.node_code"],
        ),
        CheckConstraint("sequence_no >= 1"),
        Index("idx_node_execution_task_node", "audit_task_id", "node_code"),
        Index("idx_node_execution_task_status", "audit_task_id", "status"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    audit_task_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    scenario_version_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    node_code: Mapped[str] = mapped_column(String(80), nullable=False)
    sequence_no: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(MySQLDateTime(fsp=6))
    finished_at: Mapped[datetime | None] = mapped_column(MySQLDateTime(fsp=6))
    result_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    failure_code: Mapped[str | None] = mapped_column(String(80))
    failure_message: Mapped[str | None] = mapped_column(Text)

