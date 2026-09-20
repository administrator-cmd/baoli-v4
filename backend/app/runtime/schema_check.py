"""启动期的只读数据库结构校验。

应用不会执行任何 DDL：这里只查 information_schema，把「结构与代码不一致」变成
一个明确的启动错误，而不是等到某个请求运行时才报 SQL 错误，或者带着缺失的
唯一约束继续跑出不一致数据。

校验范围：
1. 必需的表、列、唯一键/索引、复合外键、触发器是否存在；
2. 本轮新增的迁移记录是否齐全（空库初始化与增量脚本都会写入）；
3. LangGraph checkpoint 表与迁移版本（由 runtime.checkpoint 负责，见 check_checkpoint_tables）。

发现缺口时抛 RuntimeError，消息里带精确的对象名与需要执行哪一个脚本。
"""

import logging
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Engine

from ..database import engine as default_engine


logger = logging.getLogger(__name__)


# 结构建立时写入的迁移版本。backend/database/schema.sql 在同一次执行里写入这些记录；
# 本仓库只保留这一份结构脚本，已不再维护增量脚本。
REQUIRED_MIGRATIONS: tuple[tuple[str, str], ...] = (
    ("2026-09-11-field-fact-revisions", "字段事实不可变版本"),
    ("2026-09-11-evidence-locator-status", "证据定位核对状态"),
    ("2026-09-11-manual-review-decisions", "人工复核真实决定与依据类型"),
    ("2026-09-11-task-input-revision", "任务输入修订号"),
    ("2026-09-11-import-draft-and-precheck", "导入草稿修订与发布意图绑定"),
    ("2026-09-11-operation-command-record", "后台命令记录"),
    ("2026-09-11-schema-migration-registry", "迁移记录表与字段事实不可变触发器"),
    ("2026-09-11-scenario-admin-and-drafts", "场景基本资料与配置草稿"),
    ("2026-09-11-audit-plans", "审核方案与方案版本、任务方案绑定"),
    ("2026-09-11-audit-reports-and-operation-log", "报告快照与操作日志"),
    ("2026-09-11-review-drafts", "复核草稿持久化暂存"),
    ("2026-09-14-node-rulebook-and-task-rag", "节点版规则库格式与审核任务 RAG 索引/检索记录"),
    (
        "2026-09-15-draft-config-fidelity",
        "草稿保真：报告分组属性、字段产物来源与草稿来源格式版本",
    ),
    (
        "2026-09-15-import-job-created-at-index",
        "规则库导入列表：import_job 增加 (created_at, id) 索引，消除分页排序的 1038 内存不足",
    ),
    (
        "2026-09-16-scenario-config-single-source",
        "场景配置单一来源：scenario_draft 增加 sync_error，记录配置未生效的真实原因",
    ),
)

REQUIRED_TABLES: tuple[str, ...] = (
    "schema_migration",
    "scenario_template",
    "background_operation",
    "import_job",
    "import_sheet",
    "import_issue",
    "field_candidate",
    "scenario_version",
    "scenario_material",
    "scenario_node",
    "scenario_rule",
    "scenario_draft",
    "scenario_draft_material",
    "scenario_draft_field",
    "scenario_draft_rule",
    "scenario_draft_section",
    "audit_plan",
    "audit_plan_rule",
    "audit_task",
    "workflow_checkpoint",
    "audit_report",
    "operation_log",
    "review_draft",
    "task_document",
    "document_upload",
    "stored_artifact",
    "node_execution",
    "task_field_value",
    "rule_result",
    "evidence",
    "manual_review",
    # 审核任务材料证据索引与检索：可重建的派生数据，不替代原始产物与人工决定。
    "evidence_index_version",
    "evidence_chunk",
    "evidence_retrieval",
)

# 只列出「缺失会导致业务错误或数据不一致」的列，不追求与 ORM 逐列比对。
REQUIRED_COLUMNS: dict[str, tuple[str, ...]] = {
    "background_operation": (
        "operation_no",
        "object_type",
        "object_no",
        "operation_type",
        "idempotency_key",
        "input_version",
        "request_hash",
        "phase",
        "external_job_ref",
        "status",
        "request_json",
        "result_json",
    ),
    "import_job": (
        "job_no",
        "profile_version",
        "status",
        "workbook_ir_json",
        "proposals_json",
        "decisions_json",
        "manifest_draft_json",
        "coverage_json",
        "draft_revision",
        "precheck_json",
    ),
    "audit_task": (
        "task_no",
        "scenario_version_id",
        "plan_id",
        "remark",
        "status",
        "current_node",
        "thread_id",
        "task_schema_json",
        "checkpoint_json",
        "pending_review_hash",
        "input_revision",
        "deleted_at",
    ),
    "scenario_template": (
        "scenario_code",
        "scenario_name",
        "category",
        "description",
        "enabled",
        "deleted_at",
        "latest_version_no",
    ),
    "scenario_draft": ("scenario_code", "revision_no", "source", "base_version_no", "profile_version", "sync_error"),
    "scenario_draft_material": ("draft_id", "material_key", "name", "material_type", "required", "source_ref"),
    # 币种与产物来源是金额口径与主体核对的配置事实：缺列会让页面保存一次就丢掉它们。
    "scenario_draft_field": (
        "draft_id",
        "field_key",
        "material_key",
        "value_type",
        "unit",
        "currency",
        "artifact_source",
        "instruction",
        "source_ref",
    ),
    "scenario_draft_rule": ("draft_id", "rule_key", "node_code", "operator_code", "enabled", "rule_json", "source_ref"),
    # 节点版报告的分组属性：缺列时草稿保存会把分组报告退回旧的附加展示章节。
    "scenario_draft_section": (
        "draft_id",
        "section_key",
        "title",
        "field_keys",
        "report_code",
        "report_name",
        "material_keys",
        "formats",
        "section_order",
        "content_type",
        "result_sort",
        "config_status",
    ),
    "audit_plan": (
        "plan_code",
        "plan_name",
        "scenario_code",
        "scenario_version_id",
        "enabled",
        "deleted_at",
        # 保存即生效：范围快照与未生效原因都落在这里。
        "sync_error",
        "scope_hash",
        "scope_json",
    ),
    "audit_plan_rule": ("plan_id", "rule_key"),
    "audit_report": (
        "report_no",
        "audit_task_id",
        "node_execution_id",
        "report_name",
        "status",
        "content_md",
        "report_generated",
        "scope_execution_complete",
        "risk_conclusion_available",
        "input_revision",
        "origin",
        "generated_at",
    ),
    "review_draft": ("audit_task_id", "review_no", "pending_hash", "decision", "business_note", "field_value", "basis_type"),
    "operation_log": (
        "log_no",
        "object_type",
        "object_no",
        "object_name",
        "operation_type",
        "result",
        "idempotency_key",
        "request_hash",
        "command_no",
    ),
    "task_field_value": (
        "audit_task_id",
        "scenario_version_id",
        "field_key",
        "material_key",
        "document_upload_id",
        "revision_no",
        "is_current",
        "status",
        "raw_value",
        "normalized_value_json",
        "evidence_id",
        "extraction_execution_id",
        "origin",
        "current_fact_scope",
    ),
    "evidence": (
        "evidence_no",
        "audit_task_id",
        "document_upload_id",
        "rule_result_id",
        "field_value_id",
        "page_no",
        "quote_text",
        "bbox_json",
        "artifact_id",
        "locator_status",
        "locator_note",
    ),
    "manual_review": (
        "review_no",
        "audit_task_id",
        "document_upload_id",
        "rule_result_id",
        "field_value_id",
        "review_type",
        "status",
        "system_judgment_json",
        "decision",
        "business_note",
        "proposed_value",
        "before_value",
        "after_value",
        "basis_type",
        "decision_command_no",
    ),
    "document_upload": (
        "id",
        "task_document_id",
        "audit_task_id",
        "is_current",
        "current_task_document_id",
    ),
    "scenario_version": ("scenario_code", "version_no", "source", "base_version_id", "manifest_hash", "manifest_json"),
    "stored_artifact": (
        "artifact_no",
        "document_upload_id",
        "artifact_type",
        "upload_sha256",
        # 产物内容版本：索引据此判断旧索引是否仍适用于当前产物。
        "content_sha256",
        "status",
    ),
    "evidence_index_version": (
        "index_no",
        "audit_task_id",
        "document_upload_id",
        "artifact_id",
        "artifact_type",
        # 上传身份与产物内容版本必须都在：只用上传哈希无法识别“同一 PDF 重新识别”。
        "artifact_sha256",
        "artifact_content_sha256",
        "index_version",
        "model_id",
        "model_revision",
        "embedding_dim",
        "chunk_version",
        "status",
        "chunk_count",
        "built_at",
    ),
    "evidence_chunk": (
        "audit_task_id",
        "index_version_id",
        "chunk_no",
        "chunk_text",
        "context_derived",
        "char_start",
        "char_end",
        "page_no",
        "page_no_source",
        "source_entry_no",
        "embedding_json",
        "embedding_norm",
        "token_count",
    ),
    "evidence_retrieval": (
        "retrieval_no",
        "audit_task_id",
        "scenario_version_id",
        "rule_key",
        "node_code",
        "query_text",
        "scope_json",
        "model_id",
        "model_revision",
        "embedding_dim",
        "chunk_version",
        "index_version",
        "index_versions",
        "candidates_json",
        "adopted_json",
        # 人工结论必须与候选分开存在：相似度不能代替审核结论。
        "human_decision",
        # 结论的理由、所依据的候选身份与裁决人：缺少这三列时人工结论只剩一个状态值，
        # 「依据哪一条候选」「由谁依据什么判断」都无法追溯。
        "human_decision_note",
        "human_selected",
        "human_decided_at",
        "human_decided_by",
        "decision_input_revision",
        "top_k",
        "status",
        "input_revision",
    ),
}

# 唯一键与索引名（唯一约束在 information_schema.statistics 里同样可见）。
REQUIRED_INDEXES: dict[str, tuple[str, ...]] = {
    "background_operation": ("uq_background_operation_idempotency",),
    # 规则库导入列表按创建时间倒序分页；缺这个索引会退回 filesort 并把整行塞进排序缓冲，
    # 大 JSON 列（workbook_ir_json 等）会让列表接口直接 500。
    "import_job": ("idx_import_job_created_at_id",),
    "scenario_version": ("uq_scenario_version_code_no",),
    "task_field_value": (
        "uq_task_field_value_revision",
        "uq_task_field_value_current",
        "idx_task_field_current",
    ),
    "document_upload": ("uq_document_upload_one_current",),
    "rule_result": ("uq_rule_result_execution_rule",),
    # 管理模块的唯一键：幂等命令重放与方案归属都靠它们兜底。
    "scenario_draft": ("uq_scenario_draft_code",),
    "audit_plan": ("uq_audit_plan_code",),
    "audit_report": ("uq_audit_report_no",),
    "operation_log": ("uq_operation_log_no", "uq_operation_log_idempotency"),
    "review_draft": ("uq_review_draft_task_review",),
    # RAG 索引与检索：同一产物同一索引版本只能有一条；分块号在索引内唯一。
    "evidence_index_version": ("uq_evidence_index_version_no", "uq_evidence_index_scope"),
    "evidence_chunk": ("uq_evidence_chunk_no", "idx_evidence_chunk_index"),
    "evidence_retrieval": ("uq_evidence_retrieval_no", "idx_evidence_retrieval_task"),
}

# 复合外键：这些约束是防跨任务串联的关键，缺一个就会允许串数据。
REQUIRED_FOREIGN_KEYS: dict[str, tuple[str, ...]] = {
    "task_document": ("fk_task_document_current_upload",),
    "task_field_value": (
        "fk_task_field_value_upload_task",
        "fk_task_field_value_evidence_task",
        "fk_task_field_value_execution_task",
    ),
    "document_upload": ("fk_document_upload_task_document_task",),
    "evidence": (
        "fk_evidence_upload_task",
        "fk_evidence_field_value_task",
    ),
    "manual_review": (
        "fk_manual_review_field_value_task",
        "fk_manual_review_rule_result_task",
    ),
    # 管理模块的归属外键：任务必须指向真实方案，方案必须指向真实场景版本。
    "audit_task": ("fk_audit_task_plan",),
    "audit_plan": ("fk_audit_plan_scenario_version",),
    "audit_report": ("fk_audit_report_task",),
    "scenario_version": ("fk_scenario_version_base",),
    "audit_plan_rule": ("fk_audit_plan_rule_plan",),
    "scenario_draft_material": ("fk_scenario_draft_material_draft",),
    "scenario_draft_field": ("fk_scenario_draft_field_draft",),
    "scenario_draft_rule": ("fk_scenario_draft_rule_draft",),
    "scenario_draft_section": ("fk_scenario_draft_section_draft",),
    "review_draft": ("fk_review_draft_task",),
    # 索引与检索必须绑定到具体的上传版本、产物版本与任务场景版本：
    # 缺任一条外键都会允许跨任务或跨上传版本串用检索结果。
    "evidence_index_version": ("fk_evidence_index_upload_task", "fk_evidence_index_artifact_task"),
    "evidence_chunk": ("fk_evidence_chunk_index_task",),
    "evidence_retrieval": ("fk_evidence_retrieval_task",),
}

# 不可变性与快照保护触发器：缺少它们就等于允许改写已发布内容与历史事实。
REQUIRED_TRIGGERS: tuple[str, ...] = (
    "trg_manual_review_system_judgment_immutable_r2",
    "trg_task_field_value_immutable_r2",
    "trg_audit_task_snapshot_immutable_r3",
    "trg_document_upload_identity_immutable_r2",
    "trg_scenario_version_no_update_r2",
    # 报告快照不可改写、操作日志只追加。
    "trg_audit_report_content_immutable_r2",
    "trg_operation_log_immutable_r2",
)


def _query_rows(engine: Engine, statement: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    with engine.connect() as connection:
        result = connection.execute(text(statement), params or {})
        # MySQL 8.4 的 information_schema 返回大写列名（TABLE_NAME/COLUMN_NAME），
        # 这里统一归一成小写键：调用方只按小写键读取，不受服务器版本差异影响。
        return [{str(key).lower(): value for key, value in row._mapping.items()} for row in result]


def _missing_tables(engine: Engine) -> list[str]:
    rows = _query_rows(
        engine,
        """
        SELECT table_name FROM information_schema.tables
        WHERE table_schema = DATABASE()
        """,
    )
    present = {row["table_name"] for row in rows}
    return [name for name in REQUIRED_TABLES if name not in present]


def _missing_columns(engine: Engine) -> list[str]:
    rows = _query_rows(
        engine,
        """
        SELECT table_name, column_name FROM information_schema.columns
        WHERE table_schema = DATABASE()
        """,
    )
    present = {(row["table_name"], row["column_name"]) for row in rows}
    return [
        f"{table}.{column}"
        for table, columns in REQUIRED_COLUMNS.items()
        for column in columns
        if (table, column) not in present
    ]


def _missing_indexes(engine: Engine) -> list[str]:
    rows = _query_rows(
        engine,
        """
        SELECT table_name, index_name FROM information_schema.statistics
        WHERE table_schema = DATABASE()
        """,
    )
    present = {(row["table_name"], row["index_name"]) for row in rows}
    return [
        f"{table}.{index}"
        for table, indexes in REQUIRED_INDEXES.items()
        for index in indexes
        if (table, index) not in present
    ]


def _missing_foreign_keys(engine: Engine) -> list[str]:
    rows = _query_rows(
        engine,
        """
        SELECT table_name, constraint_name FROM information_schema.table_constraints
        WHERE table_schema = DATABASE() AND constraint_type = 'FOREIGN KEY'
        """,
    )
    present = {(row["table_name"], row["constraint_name"]) for row in rows}
    return [
        f"{table}.{constraint}"
        for table, constraints in REQUIRED_FOREIGN_KEYS.items()
        for constraint in constraints
        if (table, constraint) not in present
    ]


def _missing_triggers(engine: Engine) -> list[str]:
    rows = _query_rows(
        engine,
        """
        SELECT trigger_name FROM information_schema.triggers
        WHERE trigger_schema = DATABASE()
        """,
    )
    present = {row["trigger_name"] for row in rows}
    return [name for name in REQUIRED_TRIGGERS if name not in present]


def _missing_migrations(engine: Engine) -> list[str]:
    # schema_migration 表本身可能不存在（全新或旧版初始化的库）：
    # 先查存在性，缺表时按“全部迁移都未应用”处理，给出明确的初始化指引，
    # 而不是让原始 SQL 错误（1146）在启动日志里裸奔。
    rows = _query_rows(
        engine,
        """
        SELECT table_name FROM information_schema.tables
        WHERE table_schema = DATABASE() AND table_name = 'schema_migration'
        """,
    )
    if not rows:
        return [version for version, _ in REQUIRED_MIGRATIONS]
    present = {row["version"] for row in _query_rows(engine, "SELECT version FROM schema_migration")}
    return [version for version, _ in REQUIRED_MIGRATIONS if version not in present]


def verify_schema(engine: Engine | None = None) -> dict[str, Any]:
    """只读校验业务结构；缺口不为空时抛 RuntimeError。

    返回一份可供日志与系统状态接口使用的摘要；不执行任何 DDL，也不自动修表。
    """
    target = engine or default_engine
    missing_migrations = _missing_migrations(target)
    if missing_migrations:
        # 迁移记录缺失时先报这一项：它说明当前库不是由本仓库的 schema.sql 建立的，
        # 后面的列/键缺口只是同一原因的派生结果。
        raise RuntimeError(
            "数据库结构迁移记录不完整，缺少："
            + "、".join(missing_migrations)
            + "。请执行 backend/database/schema.sql 重建空库"
            "（本仓库只保留这一份结构脚本，已不含增量脚本；"
            "它在同一次执行里写入全部迁移记录）。"
        )

    report = {
        "missing_tables": _missing_tables(target),
        "missing_columns": _missing_columns(target),
        "missing_indexes": _missing_indexes(target),
        "missing_foreign_keys": _missing_foreign_keys(target),
        "missing_triggers": _missing_triggers(target),
    }
    if any(report.values()):
        parts: list[str] = []
        for key, label in (
            ("missing_tables", "缺表"),
            ("missing_columns", "缺列"),
            ("missing_indexes", "缺唯一键/索引"),
            ("missing_foreign_keys", "缺外键"),
            ("missing_triggers", "缺触发器"),
        ):
            if report[key]:
                parts.append(f"{label}：{'、'.join(report[key])}")
        raise RuntimeError(
            "数据库结构与代码不一致（"
            + "；".join(parts)
            + "）。应用不会自动修表；请执行 backend/database/schema.sql 重建空库"
            "（本仓库只保留这一份结构脚本，已不含增量脚本）。"
        )
    logger.info("数据库结构校验通过：表、列、唯一键、外键与触发器均齐全")
    return report
