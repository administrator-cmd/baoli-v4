"""启动期的只读数据库结构校验。

应用不会执行任何 DDL：这里只查 information_schema，把「结构与代码不一致」变成
一个明确的启动错误，而不是等到某个请求运行时才报 SQL 错误，或者带着缺失的
唯一约束继续跑出不一致数据。

校验范围：
1. 必需的表、列、唯一键/索引、复合外键、触发器是否存在；
2. LangGraph checkpoint 表与迁移版本（由 runtime.checkpoint 负责，见 check_checkpoint_tables）。

触发器检查覆盖的是**业务主流程里那些一经产生就不可改写的事实**：
场景草稿 → 显式发布 → 方案钉版本 → 任务冻结。已发布场景版本及其全部关系投影
（版本 / 材料 / 节点 / 规则）的改与删都在检查范围内——缺一条删除触发器，
一次误删就能让引用它的方案与历史任务无从解释，而应用层看起来一切正常。

发现缺口时抛 RuntimeError，消息里带精确的对象名与需要执行哪一个脚本。
"""

import logging
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Engine

from ..database import engine as default_engine


logger = logging.getLogger(__name__)


REQUIRED_TABLES: tuple[str, ...] = (
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
    # 用户配置的节点实例：节点存在、顺序与依赖由用户配置，不由系统自动生成。
    "scenario_draft_node",
    # 场景选用的 Sheet：模板下载与绑定步骤按它取值，不再从草稿内容反推。
    "scenario_draft_sheet",
    "scenario_draft_material",
    "scenario_draft_field",
    "scenario_draft_rule",
    "scenario_draft_section",
    "audit_plan",
    # 方案自己的配置（场景版本的可编辑副本）：形状与 scenario_draft_* 对应。
    "plan_material",
    "plan_field",
    "plan_rule",
    "plan_section",
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
    # 统一事实：字段抽取事实与计算派生事实（期间 / 报表口径 / 主体范围）。
    "task_fact",
    "rule_result",
    "evidence",
    "manual_review",
    # 审核任务材料证据索引与检索：可重建的派生数据，不替代原始产物与人工决定。
    "evidence_index_version",
    "evidence_chunk",
    "evidence_retrieval",
    # Agent 工具调用账本：请求与结果，tool_call_id 唯一（不重复执行的数据库级保证）。
    "agent_tool_call",
    # Agent 规则评估的冻结事实内容：工具执行只从它解析事实，不再读「当前数据」。
    "evaluation_snapshot",
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
        "schema_key",
        "schema_version",
        "schema_json",
        # 场景身份：缺列会让导入作业无法回答「属于哪个场景」，页面只能按表头猜。
        "scenario_code",
        "profile_display_version",
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
        # 执行快照：方案的编译产物整份冻结，执行期只读它。
        "manifest_json",
        "task_schema_json",
        "checkpoint_json",
        "pending_review_hash",
        "input_revision",
        "deleted_at",
    ),
    "scenario_template": (
        "scenario_code",
        "scenario_name",
        "schema_key",
        "schema_version",
        "schema_json",
        "category",
        "description",
        "enabled",
        "deleted_at",
        "latest_version_no",
    ),
    "scenario_draft": (
        "scenario_code",
        "schema_key",
        "schema_version",
        "schema_json",
        "revision_no",
        "source",
        "base_version_no",
        "profile_version",
        "sync_error",
        "calculation_json",
        "disabled_source_rows",
    ),
    # 节点实例：实例码、能力类型、顺序、依赖与配置缺一不可——
    # 缺 depends_on 会让发布期无法校验依赖，缺 node_type 会让分发退回按实例码猜能力。
    "scenario_draft_node": ("draft_id", "node_code", "node_type", "name", "sequence_no", "depends_on", "config_json", "enabled"),
    # 场景选用哪些 Sheet、每张表归哪个节点：缺列会让「本场景用哪些表」没有权威来源。
    "scenario_draft_sheet": ("draft_id", "sheet_role", "node_code"),
    # Sheet → 节点实例的绑定列：缺它就只能按表头自动推断用途，而系统不允许自动推断。
    "import_sheet": ("import_job_id", "sheet_key", "sheet_name", "bound_role", "bound_node_code", "header_row"),
    "scenario_draft_material": ("draft_id", "material_key", "name", "required", "source_ref", "origin_json"),
    # 币种与产物来源是金额口径与主体核对的配置事实：缺列会让页面保存一次就丢掉它们。
    "scenario_draft_field": (
        "draft_id",
        "field_key",
        "material_key",
        "value_type",
        "extraction_strategy",
        "structure_schema_json",
        "unit",
        "currency",
        "artifact_source",
        # 取数期间与报表口径：计算参数按这两项定位字段事实，缺列则计算配置无法发布。
        "period_scope",
        "statement_scope",
        "instruction",
        "source_ref",
        "origin_json",
    ),
    "scenario_draft_rule": ("draft_id", "rule_key", "node_code", "operator_code", "enabled", "rule_json", "source_ref", "origin_json"),
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
        "origin_json",
    ),
    "audit_plan": (
        "plan_code",
        "plan_name",
        "scenario_code",
        "scenario_version_id",
        "enabled",
        "deleted_at",
        # 只读上下文（节点/Sheet）与方案的编译产物：缺 manifest_json 任务就冻结不到配置。
        "context_json",
        "manifest_json",
        "manifest_hash",
        # 保存即生效：范围快照与未生效原因都落在这里。
        "sync_error",
        "scope_hash",
        "scope_json",
    ),
    # 方案自己的配置：形状与 scenario_draft_* 一致，归属是 plan_id。
    # 列清单只列「缺了会导致业务错误或数据不一致」的那些。
    "plan_material": ("plan_id", "material_key", "name", "required", "source_ref", "origin_json"),
    "plan_field": (
        "plan_id",
        "field_key",
        "material_key",
        "name",
        "value_type",
        "extraction_strategy",
        "structure_schema_json",
        "unit",
        "currency",
        "artifact_source",
        "period_scope",
        "statement_scope",
        "required",
        "instruction",
        "origin_json",
    ),
    "plan_rule": ("plan_id", "rule_key", "name", "node_code", "operator_code", "enabled", "rule_json", "origin_json"),
    "plan_section": ("plan_id", "section_key", "title", "field_keys", "report_code", "content_type", "origin_json"),
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
        "field_path",
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
        # 上传时声明并经 8018 目录核对过的材料类型：缺列会让材料校验没有 doc_type 可提交。
        "declared_material_type",
    ),
    "scenario_version": (
        "scenario_code",
        "schema_key",
        "schema_version",
        "schema_json",
        "version_no",
        "source",
        "base_version_id",
        "manifest_hash",
        "manifest_json",
    ),
    # 节点实例码、能力类型、依赖 DAG 与节点级配置：缺任一列都会让新调度器
    # 退回线性推进，或把能力类型当成实例码。
    "scenario_node": ("scenario_version_id", "node_code", "node_type", "name", "sequence_no", "depends_on", "enabled"),
    # 规则身份两个口径：rule_key 是历史身份，rule_code 是业务编号。
    "scenario_rule": ("scenario_version_id", "rule_key", "rule_code", "legacy_rule_key", "node_code", "operator_code", "enabled"),
    "rule_result": ("audit_task_id", "node_execution_id", "rule_key", "rule_code", "legacy_rule_key", "status"),
    # 统一事实：期间 / 报表 / 主体三个维度与计算追溯列缺一不可——
    # 缺期间列会让本期与上期互相覆盖，缺 input_snapshot_json 会让计算无法追溯。
    "task_fact": (
        "audit_task_id",
        "scenario_version_id",
        "fact_code",
        "fact_kind",
        "material_key",
        "entity_scope",
        "period_scope",
        "statement_scope",
        "value_type",
        "decimal_value",
        "text_value",
        "date_value",
        "boolean_value",
        "unit",
        "currency",
        "source_execution_id",
        "input_snapshot_json",
        "judgement_json",
        "calculation_status",
        "calculation_rule_code",
        "revision_no",
        "is_current",
        "status",
        "current_fact_scope",
    ),
    # 工具调用账本的关键列：缺任何一列都会让「不重复执行」或「绑定快照」失效。
    # 评估身份、规则实例与快照引用必须非空（只校验存在性，非空由结构脚本保证）。
    "agent_tool_call": (
        "audit_task_id",
        "evaluation_id",
        "rule_instance_id",
        "tool_call_id",
        "retry_of_tool_call_id",
        "tool_name",
        "tool_version",
        "tool_catalog_version",
        "tool_catalog_hash",
        "tool_policy_version",
        "arguments_json",
        "input_snapshot_hash",
        "fact_snapshot_ref",
        "caller_identity",
        "status",
        "result_json",
        "error_json",
        "external_job_id",
        "started_at",
        "completed_at",
        "created_at",
        "updated_at",
    ),
    # 冻结快照的关键列：缺 facts_json 就只剩指纹（工具会退回读当前数据）；
    # 缺 input_revision 就无法判断「这份快照还是不是当前输入」。
    "evaluation_snapshot": (
        "snapshot_ref",
        "audit_task_id",
        "evaluation_id",
        "rule_instance_id",
        "node_code",
        "input_revision",
        "facts_json",
        "fact_snapshot_hash",
        "tool_catalog_version",
        "tool_catalog_hash",
        "tool_policy_version",
        "allowed_tools_json",
        "created_at",
        "updated_at",
    ),
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
    # 统一事实的当前版本唯一键：没有它，本期/上期/期初/期末的事实会互相覆盖。
    "task_fact": ("uq_task_fact_revision", "uq_task_fact_current", "idx_task_fact_current"),
    # 工具调用账本：唯一键是「同一调用不重复执行」的数据库级保证。
    "agent_tool_call": ("uq_agent_tool_call_id", "idx_agent_tool_call_evaluation", "idx_agent_tool_call_retry"),
    # 冻结快照：ref 唯一（恢复入口）；(评估批次, 规则实例, 输入修订) 唯一，
    # 防止同一次评估拿到两份不同的输入。
    "evaluation_snapshot": ("uq_evaluation_snapshot_ref", "uq_evaluation_snapshot_scope"),
    # 管理模块的唯一键：幂等命令重放与方案归属都靠它们兜底。
    "scenario_draft": ("uq_scenario_draft_code",),
    # 节点实例码在同一草稿内唯一：重码会让规则归属与 Sheet 绑定指向不明确的实例。
    "scenario_draft_node": ("uq_scenario_draft_node_code", "idx_scenario_draft_node_draft"),
    # 同一草稿内一个 Sheet 角色只能选用一次：重复会让转换读两次同一角色。
    "scenario_draft_sheet": ("uq_scenario_draft_sheet_role", "idx_scenario_draft_sheet_draft"),
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
        "fk_task_field_value_task_material",
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
    "plan_material": ("fk_plan_material_plan",),
    "plan_field": ("fk_plan_field_plan",),
    "plan_rule": ("fk_plan_rule_plan",),
    "plan_section": ("fk_plan_section_plan",),
    "scenario_draft_material": ("fk_scenario_draft_material_draft",),
    "scenario_draft_node": ("fk_scenario_draft_node_draft",),
    "scenario_draft_sheet": ("fk_scenario_draft_sheet_draft",),
    "scenario_draft_field": ("fk_scenario_draft_field_draft",),
    "scenario_draft_rule": ("fk_scenario_draft_rule_draft",),
    "scenario_draft_section": ("fk_scenario_draft_section_draft",),
    "review_draft": ("fk_review_draft_task",),
    # 统一事实必须绑定到真实任务与产生它的执行批次：缺任一条都会允许跨任务串事实。
    "task_fact": ("fk_task_fact_task", "fk_task_fact_execution_task"),
    # 索引与检索必须绑定到具体的上传版本、产物版本与任务场景版本：
    # 缺任一条外键都会允许跨任务或跨上传版本串用检索结果。
    "evidence_index_version": ("fk_evidence_index_upload_task", "fk_evidence_index_artifact_task"),
    "evidence_chunk": ("fk_evidence_chunk_index_task",),
    "evidence_retrieval": ("fk_evidence_retrieval_task",),
    # 冻结快照必须绑定到真实任务：缺它就会允许跨任务串用事实。
    "evaluation_snapshot": ("fk_evaluation_snapshot_task",),
}

# 不可变性与快照保护触发器：缺少它们就等于允许改写已发布内容与历史事实。
REQUIRED_TRIGGERS: tuple[str, ...] = (
    "trg_manual_review_system_judgment_immutable_r2",
    "trg_task_field_value_immutable_r2",
    # 统一事实与字段事实同一口径：事实不可变，重跑新增版本。
    "trg_task_fact_immutable_r2",
    "trg_audit_task_snapshot_immutable_r3",
    "trg_document_upload_identity_immutable_r2",
    # 已发布场景版本**与它的全部关系投影**都是追加的历史事实：
    # 缺少删除/改写触发器时，一次误删就能让引用它的方案与历史任务无从解释，
    # 而应用层看起来一切正常。因此八条（版本 / 材料 / 节点 / 规则 × 改 / 删）逐一检查。
    "trg_scenario_version_no_update_r2",
    "trg_scenario_version_no_delete_r2",
    "trg_scenario_material_no_update_r2",
    "trg_scenario_material_no_delete_r2",
    "trg_scenario_node_no_update_r2",
    "trg_scenario_node_no_delete_r2",
    "trg_scenario_rule_no_update_r2",
    "trg_scenario_rule_no_delete_r2",
    # 任务材料位的快照同样不可改写（它记录任务创建时冻结的材料范围）。
    "trg_task_document_snapshot_immutable_r2",
    # 报告快照不可改写、操作日志只追加。
    "trg_audit_report_content_immutable_r2",
    "trg_operation_log_immutable_r2",
    # 工具调用账本的身份与参数一经写入即不可改；终态不允许改成任何其他状态。
    "trg_agent_tool_call_identity_immutable",
    # 冻结快照不可改写：改写它等于让已经据此执行的工具结果无从解释。
    "trg_evaluation_snapshot_immutable",
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


def verify_schema(engine: Engine | None = None) -> dict[str, Any]:
    """只读校验业务结构；缺口不为空时抛 RuntimeError。

    返回一份可供日志与系统状态接口使用的摘要；不执行任何 DDL，也不自动修表。
    """
    target = engine or default_engine
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
