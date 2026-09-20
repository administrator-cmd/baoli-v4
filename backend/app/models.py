from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CHAR,
    CheckConstraint,
    Computed,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.mysql import DATETIME as MySQLDateTime
from sqlalchemy.dialects.mysql import MEDIUMTEXT
from sqlalchemy.orm import Mapped, mapped_column

from .database import Base


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        MySQLDateTime(fsp=6), server_default=text("CURRENT_TIMESTAMP(6)"), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        MySQLDateTime(fsp=6),
        server_default=text("CURRENT_TIMESTAMP(6)"),
        server_onupdate=text("CURRENT_TIMESTAMP(6)"),
        nullable=False,
    )


class ImportJob(Base, TimestampMixin):
    __tablename__ = "import_job"
    __table_args__ = (
        CheckConstraint("source_size >= 0"),
        Index("idx_import_job_status_created_at", "status", "created_at"),
        # 与 idx_import_job_status_created_at 互补：本表带 workbook_ir_json 等大 JSON 列，
        # 列表按 (created_at, id) 倒序分页时若命中不了索引就会走 filesort，把整行塞进
        # 排序缓冲（默认仅 256KB），直接报 1038 Out of sort memory。有了这个索引，
        # 排序与分页由索引顺序提供，行宽再大也不参与排序。
        Index("idx_import_job_created_at_id", "created_at", "id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    job_no: Mapped[str] = mapped_column(String(40), unique=True, nullable=False)
    source_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    source_path: Mapped[str] = mapped_column(String(500), nullable=False)
    source_sha256: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    source_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    profile_version: Mapped[str] = mapped_column(String(40), nullable=False)
    status: Mapped[str] = mapped_column(String(40), nullable=False)
    workbook_ir_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    proposals_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    decisions_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    manifest_draft_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    # 每条来源业务记录的处置（候选 / 明确排除 / 待处理问题）及合并拆分关系。
    coverage_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    # 草稿修订号：绑定、排除、裁决和候选原地修正都会递增。
    # 预检查与发布都以它为准，旧修订的提交会被明确拒绝。
    draft_revision: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default=text("0"))
    # 上一次有效预检查的绑定记录：profile 版本、草稿修订、manifest 规范化哈希、
    # 发布方式、目标模板身份与名称。发布时必须与当前意图完全一致。
    precheck_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    failure_code: Mapped[str | None] = mapped_column(String(80))
    failure_message: Mapped[str | None] = mapped_column(Text)


class ScenarioTemplate(Base, TimestampMixin):
    """审核场景身份与版本计数器。

    scenario_code 是场景身份，不是中文名称；latest_version_no 是原子计数器，
    版本号由它分配，不依赖无保护的 max(version_no)+1。
    基本资料（名称/业务类型/说明/启停/逻辑删除）直接维护在本表；
    已发布版本保存的是发布时刻的名称与配置快照，不随本表修改而变。
    """

    __tablename__ = "scenario_template"
    __table_args__ = (
        UniqueConstraint("scenario_code"),
        CheckConstraint("latest_version_no >= 0"),
        Index("idx_scenario_template_deleted", "deleted_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    scenario_code: Mapped[str] = mapped_column(String(80), nullable=False)
    scenario_name: Mapped[str] = mapped_column(String(255), nullable=False)
    # 业务类型：页面维护的展示与筛选属性，不参与执行语义。
    category: Mapped[str | None] = mapped_column(String(80))
    description: Mapped[str | None] = mapped_column(Text)
    # 启停：停用后不用于新建方案与任务；已创建任务按原快照继续。
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default=text("1"))
    # 逻辑删除：保留历史版本、任务与报告；已删除场景不出现在默认列表。
    deleted_at: Mapped[datetime | None] = mapped_column(MySQLDateTime(fsp=6))
    latest_version_no: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class BackgroundOperation(Base, TimestampMixin):
    """后台长操作的持久记录：先认领，再由后台用独立 Session 执行。

    请求只返回受理状态；真正的结果写回 result_json / failure_*。
    活跃操作（PENDING/RUNNING）在对象上互斥，重复的幂等标识返回同一条记录。
    """

    __tablename__ = "background_operation"
    __table_args__ = (
        UniqueConstraint("operation_no"),
        # 幂等标识与请求内容绑定：同一对象同一操作类型下，标识不同才是不同的操作。
        UniqueConstraint("object_type", "object_no", "operation_type", "idempotency_key"),
        Index("idx_background_operation_object", "object_type", "object_no", "status"),
        Index("idx_background_operation_status", "status", "created_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    operation_no: Mapped[str] = mapped_column(String(40), nullable=False)
    object_type: Mapped[str] = mapped_column(String(30), nullable=False)
    object_no: Mapped[str] = mapped_column(String(40), nullable=False)
    operation_type: Mapped[str] = mapped_column(String(40), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(120), nullable=False)
    target_node: Mapped[str | None] = mapped_column(String(40))
    input_version: Mapped[str | None] = mapped_column(String(120))
    # 规范化请求摘要：同一幂等标识必须比较它才能判断是重发还是新请求。
    request_hash: Mapped[str | None] = mapped_column(CHAR(64))
    # 命令当前停在哪一步（COMMAND_ACCEPTED / EXTERNAL_SUBMITTED / RESULT_UNKNOWN /
    # BUSINESS_COMMITTED / GRAPH_ADVANCED / INTERRUPT_WAITING）。恢复时按它决定唯一推进动作。
    phase: Mapped[str | None] = mapped_column(String(40))
    # 外部作业引用（例如 OCR jobId）：本地超时后只能续查同一个作业，不能重新提交。
    external_job_ref: Mapped[str | None] = mapped_column(String(120))
    status: Mapped[str] = mapped_column(String(30), nullable=False)
    request_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    result_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    failure_code: Mapped[str | None] = mapped_column(String(80))
    failure_message: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(MySQLDateTime(fsp=6))
    finished_at: Mapped[datetime | None] = mapped_column(MySQLDateTime(fsp=6))


class ImportSheet(Base, TimestampMixin):
    __tablename__ = "import_sheet"
    __table_args__ = (
        UniqueConstraint("import_job_id", "sheet_key"),
        CheckConstraint("header_row IS NULL OR header_row >= 1"),
        Index("idx_import_sheet_job_status", "import_job_id", "status"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    import_job_id: Mapped[int] = mapped_column(ForeignKey("import_job.id"), nullable=False)
    sheet_key: Mapped[str] = mapped_column(String(40), nullable=False)
    sheet_name: Mapped[str] = mapped_column(String(255), nullable=False)
    visible: Mapped[bool] = mapped_column(Boolean, nullable=False)
    status: Mapped[str] = mapped_column(String(40), nullable=False)
    bound_role: Mapped[str | None] = mapped_column(String(40))
    header_row: Mapped[int | None] = mapped_column(Integer)
    snapshot_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)


class ImportIssue(Base, TimestampMixin):
    __tablename__ = "import_issue"
    __table_args__ = (
        UniqueConstraint("import_job_id", "issue_no"),
        Index("idx_import_issue_job_status", "import_job_id", "status"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    import_job_id: Mapped[int] = mapped_column(ForeignKey("import_job.id"), nullable=False)
    issue_no: Mapped[str] = mapped_column(String(40), nullable=False)
    category: Mapped[str] = mapped_column(String(40), nullable=False)
    severity: Mapped[str] = mapped_column(String(20), nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    source_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    options_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False)
    decision_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)


class FieldCandidate(Base, TimestampMixin):
    __tablename__ = "field_candidate"
    __table_args__ = (
        UniqueConstraint("import_job_id", "candidate_ref"),
        Index("idx_field_candidate_job_status", "import_job_id", "status"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    import_job_id: Mapped[int] = mapped_column(ForeignKey("import_job.id"), nullable=False)
    candidate_ref: Mapped[str] = mapped_column(String(120), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    value_type: Mapped[str] = mapped_column(String(30), nullable=False)
    material_ref: Mapped[str] = mapped_column(String(120), nullable=False)
    proposal_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False)


class ScenarioVersion(Base, TimestampMixin):
    """一次发布固化的场景版本；发布后不可覆盖。

    来源（source）：IMPORT=规则库导入；FORM=页面表单草稿发布；COPY=复制场景；
    REVISION=从某一已发布版本创建修订后发布。页面来源没有 Excel 作业，
    import_job_id 为空（不伪造导入作业与工作表行号），但仍然保存完整 manifest。
    """

    __tablename__ = "scenario_version"
    __table_args__ = (
        UniqueConstraint("scenario_code", "version_no"),
        UniqueConstraint("import_job_id"),
        CheckConstraint("version_no >= 1"),
        Index("idx_scenario_version_code_status", "scenario_code", "status", "published_at"),
        Index("idx_scenario_version_base", "base_version_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    # 页面表单/复制来源的版本没有导入作业；旧数据保持原 import_job_id 不变。
    import_job_id: Mapped[int | None] = mapped_column(ForeignKey("import_job.id"))
    scenario_code: Mapped[str] = mapped_column(String(80), nullable=False)
    scenario_name: Mapped[str] = mapped_column(String(255), nullable=False)
    version_no: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False)
    # 来源：IMPORT / FORM / COPY / REVISION。旧数据在迁移中统一标为 IMPORT。
    source: Mapped[str] = mapped_column(String(20), nullable=False, default="IMPORT", server_default=text("'IMPORT'"))
    # 基于哪个版本创建的修订（仅页面修订来源有值）；不指向表示从零编辑。
    base_version_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("scenario_version.id"))
    change_summary: Mapped[str | None] = mapped_column(String(500))
    manifest_hash: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    manifest_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    published_at: Mapped[datetime] = mapped_column(MySQLDateTime(fsp=6), nullable=False)


class ScenarioMaterial(Base, TimestampMixin):
    __tablename__ = "scenario_material"
    __table_args__ = (UniqueConstraint("scenario_version_id", "material_key"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    scenario_version_id: Mapped[int] = mapped_column(ForeignKey("scenario_version.id"), nullable=False)
    material_key: Mapped[str] = mapped_column(String(40), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    material_type: Mapped[str] = mapped_column(String(80), nullable=False)
    required: Mapped[bool] = mapped_column(Boolean, nullable=False)
    source_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)


class ScenarioNode(Base, TimestampMixin):
    __tablename__ = "scenario_node"
    __table_args__ = (
        UniqueConstraint("scenario_version_id", "node_code"),
        UniqueConstraint("scenario_version_id", "sequence_no"),
        CheckConstraint("sequence_no >= 1"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    scenario_version_id: Mapped[int] = mapped_column(ForeignKey("scenario_version.id"), nullable=False)
    node_code: Mapped[str] = mapped_column(String(40), nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    sequence_no: Mapped[int] = mapped_column(Integer, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False)


class ScenarioRule(Base, TimestampMixin):
    __tablename__ = "scenario_rule"
    __table_args__ = (
        UniqueConstraint("scenario_version_id", "rule_key"),
        ForeignKeyConstraint(
            ["scenario_version_id", "node_code"],
            ["scenario_node.scenario_version_id", "scenario_node.node_code"],
        ),
        Index("idx_scenario_rule_version_node_enabled", "scenario_version_id", "node_code", "enabled"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    scenario_version_id: Mapped[int] = mapped_column(ForeignKey("scenario_version.id"), nullable=False)
    rule_key: Mapped[str] = mapped_column(String(40), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    node_code: Mapped[str] = mapped_column(String(40), nullable=False)
    operator_code: Mapped[str] = mapped_column(String(40), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False)
    rule_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    source_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)


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
    # 不猜测迁移、不改写历史结果。方案后续改动不影响本任务：创建时已把范围抓进
    # task_schema_json，那是本任务的执行快照。
    plan_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("audit_plan.id"))
    task_name: Mapped[str] = mapped_column(String(255), nullable=False)
    # 任务备注：属于可编辑的基本资料，不进入执行快照。
    remark: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(40), nullable=False)
    current_node: Mapped[str] = mapped_column(String(40), nullable=False)
    thread_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    task_schema_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    checkpoint_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    pending_review_hash: Mapped[str | None] = mapped_column(CHAR(64))
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
    # 产物内容版本：产物**实际内容**的哈希（JSON 规范序列化或二进制字节）。
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
            ["scenario_version_id", "material_key"],
            ["scenario_material.scenario_version_id", "scenario_material.material_key"],
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


class RuleResult(Base, TimestampMixin):
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
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    audit_task_id: Mapped[int] = mapped_column(ForeignKey("audit_task.id"), nullable=False)
    node_execution_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    rule_key: Mapped[str] = mapped_column(String(40), nullable=False)
    rule_name: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(40), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    result_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)


class Evidence(Base, TimestampMixin):
    """一条证据引用。

    locator_status 记录本次引用是否真的能在所属产物里核对到：
    VERIFIED=引文/页码/坐标已在指定产物页内核对通过；
    UNVERIFIED=产物里没有可定位依据（例如 OCR 只给出文本块，没有坐标）；
    UNLOCATABLE=声明了位置但核对失败（引文不在该页、页码超范围、坐标越界）。
    任何情况下都不伪造高亮：页号或坐标缺失时保持 NULL 并如实标注。
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
    node_code: Mapped[str] = mapped_column(String(40), nullable=False)
    sequence_no: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(MySQLDateTime(fsp=6))
    finished_at: Mapped[datetime | None] = mapped_column(MySQLDateTime(fsp=6))
    result_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    failure_code: Mapped[str | None] = mapped_column(String(80))
    failure_message: Mapped[str | None] = mapped_column(Text)


class ScenarioDraft(Base, TimestampMixin):
    """场景配置草稿（每个场景一条）。

    revision_no 是草稿修订号：材料/字段/规则/报告配置的每次写入都会递增，
    发布命令携带它，过期修订的发布会与保存内容不一致而被明确拒绝。
    source 记录草稿的来源（FORM=页面直接建立；EXCEL=从导入发布派生；COPY=复制；
    REVISION=基于某个已发布版本创建的修订）；base_version_no 记录它基于的版本号，
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
    revision_no: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default=text("0"))
    source: Mapped[str] = mapped_column(String(20), nullable=False, default="FORM", server_default=text("'FORM'"))
    base_version_no: Mapped[int | None] = mapped_column(Integer)
    # 配置草稿的来源格式版本（contract_review_v1 / v2 / v3）。页面表单没有这个版本概念，
    # 因此它由导入发布、版本修订或复制从原始 manifest 继承：发布时按它导出，
    # 不能一律用默认版本给所有草稿打戳，那样会把旧格式配置说成节点版格式。
    profile_version: Mapped[str | None] = mapped_column(String(40))
    # 最近一次保存时配置未能生效的原因（编译未通过：缺业务规则、缺材料识别类型等）。
    # NULL 表示当前配置已生效。页面据此显示“尚未生效 + 具体原因”，
    # 已发布版本保持为上一个可用版本；这是显式状态，不是静默降级。
    sync_error: Mapped[dict[str, Any] | None] = mapped_column(JSON)


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
    material_type: Mapped[str] = mapped_column(String(80), nullable=False)
    required: Mapped[bool] = mapped_column(Boolean, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default=text("('')"))
    # 来源业务编号（Excel 的「材料编号」列）：导出时要原样写回，页面新建的行留空。
    source_ref: Mapped[str | None] = mapped_column(String(80))
    sort_no: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default=text("0"))


class ScenarioDraftField(Base, TimestampMixin):
    __tablename__ = "scenario_draft_field"
    __table_args__ = (
        UniqueConstraint("draft_id", "field_key"),
        CheckConstraint("sort_no >= 0"),
        Index("idx_scenario_draft_field_draft", "draft_id"),
        Index("idx_scenario_draft_field_material", "draft_id", "material_key"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    draft_id: Mapped[int] = mapped_column(ForeignKey("scenario_draft.id"), nullable=False)
    field_key: Mapped[str] = mapped_column(String(40), nullable=False)
    material_key: Mapped[str] = mapped_column(String(40), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    value_type: Mapped[str] = mapped_column(String(30), nullable=False)
    unit: Mapped[str | None] = mapped_column(String(40))
    # 币种是金额比较与加总的口径之一：留空表示未声明，不表示人民币。
    currency: Mapped[str | None] = mapped_column(String(40))
    # 产物来源：该字段的取值由哪种处理产物提供（当前仅印章识别 SEAL）。
    # 主体核对必须按这一声明确定「待核对主体取自哪份材料的哪种产物」，
    # 不能靠字段名猜。留空表示字段由常规字段抽取提供。
    artifact_source: Mapped[str | None] = mapped_column(String(30))
    required: Mapped[bool] = mapped_column(Boolean, nullable=False)
    instruction: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default=text("('')"))
    # 来源业务编号（Excel 的「字段编号」列）：导出时写回，页面新建的字段留空。
    source_ref: Mapped[str | None] = mapped_column(String(80))
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


class AuditPlan(Base, TimestampMixin):
    """审核方案：对某个场景版本的可复用审核范围选择，保存即生效。

    方案只有一份配置，没有草稿/发布之分。每次保存都会重算范围并写入 scope_json
    （显式规则、自动补入依赖、未启用规则、任务 schema 快照）与 scope_hash；
    算不出来时保存照常成功，原因记在 sync_error，页面显示「待核对」。

    已建任务不受方案后续改动影响：任务创建时把 task_schema_json 抓成自己的执行快照。
    方案不自动跟随场景升级；切换场景版本是一次显式的保存。
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
    # 当前绑定的场景版本。未选择版本时为空，此时范围算不出来，sync_error 说明原因。
    scenario_version_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("scenario_version.id"))
    description: Mapped[str | None] = mapped_column(Text)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default=text("1"))
    deleted_at: Mapped[datetime | None] = mapped_column(MySQLDateTime(fsp=6))
    # 最近一次保存时范围未能生效的原因（编译未通过：没选规则、场景未生效等）。
    # NULL 表示当前范围已生效。这是可展示状态，不是静默降级。
    sync_error: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    # 范围指纹：内容没变时不重复写 scope_json。
    scope_hash: Mapped[str | None] = mapped_column(CHAR(64))
    # 保存时刻算好的范围快照；任务创建直接复用它，不重新解释选择。
    scope_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)


class AuditPlanRule(Base, TimestampMixin):
    """方案当前显式选中的规则（按绑定场景版本内的 rule_key 引用）。"""

    __tablename__ = "audit_plan_rule"
    __table_args__ = (
        UniqueConstraint("plan_id", "rule_key"),
        Index("idx_audit_plan_rule_plan", "plan_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    plan_id: Mapped[int] = mapped_column(ForeignKey("audit_plan.id"), nullable=False)
    rule_key: Mapped[str] = mapped_column(String(40), nullable=False)


class AuditReport(Base, TimestampMixin):
    """报告输出节点生成的一份报告快照。

    报告由真实节点生成，不提供空白报告手填结论；重新生成是新快照，历史快照保留。
    名称与备注可编辑，正文、三信号与快照范围不可改写。
    origin=NODE 表示由报告节点写入；LEGACY_BACKFILL 表示增量升级时从历史
    节点结果按原值回填（内容与信号均来自当时的真实结果，未做任何改写）。
    """

    __tablename__ = "audit_report"
    __table_args__ = (
        UniqueConstraint("report_no"),
        Index("idx_audit_report_task_generated", "audit_task_id", "generated_at"),
        Index("idx_audit_report_generated", "generated_at"),
        Index("idx_audit_report_status", "status"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    report_no: Mapped[str] = mapped_column(String(40), nullable=False)
    audit_task_id: Mapped[int] = mapped_column(ForeignKey("audit_task.id"), nullable=False)
    # 产生这份报告的报告输出节点执行；历史回填同样指向当时的执行记录。
    node_execution_id: Mapped[int | None] = mapped_column(BigInteger)
    report_name: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="GENERATED", server_default=text("'GENERATED'"))
    content_type: Mapped[str] = mapped_column(String(60), nullable=False, default="text/markdown", server_default=text("'text/markdown'"))
    content_md: Mapped[str | None] = mapped_column(MEDIUMTEXT)
    report_generated: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=text("0"))
    scope_execution_complete: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=text("0"))
    risk_conclusion_available: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=text("0"))
    # 生成时刻的不可判定原因集合与真实范围快照（输入修订、上传/事实/复核/结果 ID）。
    reasons_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    snapshot_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    # 报告对账用的输入修订；历史回填无法确定时为空，页面显示“修订未知”。
    input_revision: Mapped[int | None] = mapped_column(Integer)
    origin: Mapped[str] = mapped_column(String(20), nullable=False, default="NODE", server_default=text("'NODE'"))
    remark: Mapped[str | None] = mapped_column(Text)
    archived_at: Mapped[datetime | None] = mapped_column(MySQLDateTime(fsp=6))
    generated_at: Mapped[datetime] = mapped_column(MySQLDateTime(fsp=6), nullable=False)


class OperationLog(Base, TimestampMixin):
    """一次被受理并成功完成的业务写操作的持久记录。

    只记录真实发生并已提交的操作（创建、编辑、启停、删除恢复、发布、复制、
    上传/替换/移除、复核提交、报告备注与归档等）；不记录失败的尝试，
    也不复制业务执行记录——节点执行与后台命令分别在 node_execution 与
    background_operation 中，按 command_no 关联查询。
    """

    __tablename__ = "operation_log"
    __table_args__ = (
        UniqueConstraint("log_no"),
        # 同步命令的幂等标识：同一对象同一操作类型下，重复提交必须复用第一次的
        # 结果（响应丢失后的重发不能重复创建）。没有幂等标识的操作记录不受影响
        # （MySQL 唯一键允许多个 NULL）。
        UniqueConstraint("object_type", "operation_type", "idempotency_key"),
        Index("idx_operation_log_object", "object_type", "object_no", "created_at"),
        Index("idx_operation_log_created", "created_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    log_no: Mapped[str] = mapped_column(String(40), nullable=False)
    object_type: Mapped[str] = mapped_column(String(30), nullable=False)
    object_no: Mapped[str] = mapped_column(String(40), nullable=False)
    object_name: Mapped[str] = mapped_column(String(255), nullable=False)
    operation_type: Mapped[str] = mapped_column(String(40), nullable=False)
    result: Mapped[str] = mapped_column(String(20), nullable=False, default="SUCCEEDED", server_default=text("'SUCCEEDED'"))
    idempotency_key: Mapped[str | None] = mapped_column(String(120))
    request_hash: Mapped[str | None] = mapped_column(CHAR(64))
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    # 关联的后台命令编号（若该操作受理了后台执行）。
    command_no: Mapped[str | None] = mapped_column(String(40))
    detail_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    failure_code: Mapped[str | None] = mapped_column(String(80))
    failure_message: Mapped[str | None] = mapped_column(Text)


# ---------------------------------------------------------------------------
# 审核任务材料证据索引与检索（本地 Embedding + 任务范围检索）
#
# 设计边界：
#   - 索引是**可重建的派生数据**：原始产物、字段事实与人工决定各自保存，索引不替代它们；
#   - 检索范围先按任务、材料、当前上传版本、产物版本与索引版本过滤，再在候选内做向量排序，
#     绝不先全库检索再交给前端筛选，也不跨任务或跨上传版本共用结果；
#   - 索引批次成功构建后才标为可检索；部分失败不得标为就绪；
#   - 每次检索保存查询内容、限定范围、模型与索引版本、候选、相似度与被采用的证据。
# ---------------------------------------------------------------------------


class EvidenceIndexVersion(Base, TimestampMixin):
    """一个上传版本某个产物的一份索引版本。

    模型、向量维度或切分方式变化时必须新建索引版本，而不是原地覆盖：
    旧索引保留为历史，旧检索记录仍能指向当时使用的分块与模型。
    """

    __tablename__ = "evidence_index_version"
    __table_args__ = (
        # 一份索引由「上传 + 产物类型 + 产物内容版本 + 索引版本」唯一确定。
        # artifact_content_sha256 是产物**内容**的哈希：同一 PDF 重新识别后内容变了，
        # 新索引就是另一条记录，旧索引不会被当成当前产物的索引。
        UniqueConstraint(
            "document_upload_id", "artifact_type", "artifact_content_sha256", "index_version"
        ),
        UniqueConstraint("id", "audit_task_id"),
        ForeignKeyConstraint(
            ["document_upload_id", "audit_task_id"],
            ["document_upload.id", "document_upload.audit_task_id"],
        ),
        ForeignKeyConstraint(
            ["artifact_id", "audit_task_id"],
            ["stored_artifact.id", "stored_artifact.audit_task_id"],
        ),
        CheckConstraint("chunk_count >= 0"),
        Index("idx_evidence_index_upload_status", "document_upload_id", "status"),
        Index("idx_evidence_index_task_status", "audit_task_id", "status"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    index_no: Mapped[str] = mapped_column(String(40), unique=True, nullable=False)
    audit_task_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    document_upload_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    artifact_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    artifact_type: Mapped[str] = mapped_column(String(40), nullable=False)
    # 上传身份：这份索引是由哪个上传文件产生的。
    artifact_sha256: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    # 产物内容版本：索引实际依据的产物内容哈希。产物重新识别后内容哈希变化，
    # 旧索引即不再属于当前产物，必须重建而不是继续沿用。
    # 可空只用于表达“本次升级之前写入的索引没有内容版本”：这类索引不再可检索，
    # 必须重建后才有内容版本；建索引时非空是硬要求。
    artifact_content_sha256: Mapped[str | None] = mapped_column(CHAR(64))
    # 索引版本标识：格式名 + 向量维度 + 决定因素摘要，长度固定（见 embedding_service）。
    index_version: Mapped[str] = mapped_column(String(80), nullable=False)
    model_id: Mapped[str] = mapped_column(String(160), nullable=False)
    model_revision: Mapped[str] = mapped_column(String(80), nullable=False)
    embedding_dim: Mapped[int] = mapped_column(Integer, nullable=False)
    chunk_version: Mapped[str] = mapped_column(String(80), nullable=False)
    # BUILDING=构建中；READY=整批成功、可检索；FAILED=构建失败（明确记录原因）；
    # INVALIDATED=上传版本被替换或产物重建，不再进入当前检索（历史保留）。
    status: Mapped[str] = mapped_column(String(30), nullable=False)
    chunk_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default=text("0"))
    failure_code: Mapped[str | None] = mapped_column(String(80))
    failure_message: Mapped[str | None] = mapped_column(Text)
    built_at: Mapped[datetime | None] = mapped_column(MySQLDateTime(fsp=6))


class EvidenceChunk(Base, TimestampMixin):
    """一个可检索分块：原文、真实位置与向量。

    分块保留标题/表头上下文与原文位置；没有页码时记录原文行/字符区间并标注缺失，
    绝不把图片序号或 Markdown 图片路径当作 PDF 页码。
    """

    __tablename__ = "evidence_chunk"
    __table_args__ = (
        UniqueConstraint("index_version_id", "chunk_no"),
        UniqueConstraint("id", "audit_task_id"),
        ForeignKeyConstraint(
            ["index_version_id", "audit_task_id"],
            ["evidence_index_version.id", "evidence_index_version.audit_task_id"],
        ),
        CheckConstraint("chunk_no >= 1"),
        CheckConstraint("char_start >= 0 AND char_end >= char_start"),
        Index("idx_evidence_chunk_index", "index_version_id", "chunk_no"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    audit_task_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    index_version_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    chunk_no: Mapped[int] = mapped_column(Integer, nullable=False)
    # 检索用文本（含标题/表头上下文）；原文位置指回它在源文本中的区间。
    chunk_text: Mapped[str] = mapped_column(MEDIUMTEXT, nullable=False)
    # 上下文是否由派生规则补全（例如补齐表头）：派生上下文不得伪装成连续原文。
    context_derived: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=text("0"))
    heading_path: Mapped[str | None] = mapped_column(String(500))
    char_start: Mapped[int] = mapped_column(Integer, nullable=False)
    char_end: Mapped[int] = mapped_column(Integer, nullable=False)
    # 页号与页号来源：PROVIDER=供应商返回的真实页号；SYNTHETIC_SEQUENCE=按结果项顺序的合成序号；
    # ABSENT=没有页码。合成序号只表示产物内的顺序，不等于原 PDF 页码。
    page_no: Mapped[int | None] = mapped_column(Integer)
    page_no_source: Mapped[str] = mapped_column(String(30), nullable=False, default="ABSENT", server_default=text("'ABSENT'"))
    # 印章条目：保留它的原始 JSON 条目引用（序号），可以回到原 JSON 条目，不只保存向量。
    source_entry_no: Mapped[int | None] = mapped_column(Integer)
    # 向量以十进制文本的 JSON 数组保存：不引入新的二进制依赖，也不做有损压缩。
    embedding_json: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    embedding_norm: Mapped[str] = mapped_column(String(80), nullable=False)
    token_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default=text("0"))


class EvidenceRetrieval(Base, TimestampMixin):
    """一次任务内检索的完整记录。

    保存查询内容、限定范围、模型与索引版本、候选与相似度、最终采用的证据，
    以及它服务的规则执行。相似度只用于排序，不构成主体一致或审核通过的证明。
    """

    __tablename__ = "evidence_retrieval"
    __table_args__ = (
        UniqueConstraint("id", "audit_task_id"),
        ForeignKeyConstraint(
            ["audit_task_id", "scenario_version_id"],
            ["audit_task.id", "audit_task.scenario_version_id"],
        ),
        Index("idx_evidence_retrieval_task", "audit_task_id", "created_at"),
        Index("idx_evidence_retrieval_rule", "audit_task_id", "rule_key"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    retrieval_no: Mapped[str] = mapped_column(String(40), unique=True, nullable=False)
    audit_task_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    scenario_version_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    # 发起检索的规则键；程序侧检索没有规则键时为 NULL。
    rule_key: Mapped[str | None] = mapped_column(String(40))
    node_code: Mapped[str | None] = mapped_column(String(40))
    query_text: Mapped[str] = mapped_column(Text, nullable=False)
    # 限定范围（任务、材料、上传版本、产物、索引版本）：范围过滤发生在排序之前。
    scope_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    model_id: Mapped[str] = mapped_column(String(160), nullable=False)
    model_revision: Mapped[str] = mapped_column(String(80), nullable=False)
    embedding_dim: Mapped[int | None] = mapped_column(Integer)
    chunk_version: Mapped[str | None] = mapped_column(String(80))
    index_version: Mapped[str | None] = mapped_column(String(80))
    index_versions: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    # 候选结果：分块、相似度、产物归属与定位。这里保存的是**候选**，不是人工结论。
    candidates_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False)
    # 实际送入后续步骤的正文条目（本次检索的上下文预算），与人工结论是两件事。
    adopted_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False)
    # 人工结论：由工作人员在候选上选择一致 / 不一致 / 证据不足，并写明理由。
    # 与 candidates 分开保存：相似度不能代替审核结论，也不自动把前几条标为已采纳。
    human_decision: Mapped[str | None] = mapped_column(String(40))
    human_decision_note: Mapped[str | None] = mapped_column(Text)
    human_selected: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    human_decided_at: Mapped[datetime | None] = mapped_column(MySQLDateTime(fsp=6))
    human_decided_by: Mapped[str | None] = mapped_column(String(120))
    # 人工结论对应的任务输入修订：修订变化后旧结论不得套用到新的上传/事实。
    decision_input_revision: Mapped[int | None] = mapped_column(Integer)
    top_k: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False)
    message: Mapped[str | None] = mapped_column(Text)
    input_revision: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default=text("0"))
