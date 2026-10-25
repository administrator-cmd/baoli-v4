-- ============================================================================
-- baoli-v4 数据库结构
--
-- 前置条件：MySQL >= 8.0.16（CHECK、生成列、JSON 列）。
-- 用法：空库执行一次；CREATE TABLE 遇到同名表直接报错。
-- 结构以本文件为准，应用启动只读核对，不自动建表或迁移。
--
-- 数据边界：
--   数据库：身份与关系、状态与版本、配置快照、审核结果与日志。
--   文件：上传原件、外部服务产物正文、任务取值 CSV、模型原始响应。
--   业务取值只存 CSV；数据库不得保存其副本。
--
-- 配置 JSON：scenario_draft.config_json、scenario_version.manifest_json、
-- audit_plan.config_json、audit_task.manifest_json。
-- 查询、排序和唯一键使用真实列，不使用 JSON 路径。
-- ============================================================================


-- 1. 场景身份与后台操作

-- 场景身份与版本计数器；latest_version_no 原子分配。
CREATE TABLE scenario_template (
    id BIGINT NOT NULL AUTO_INCREMENT,
    scenario_code VARCHAR(80) NOT NULL,
    scenario_name VARCHAR(255) NOT NULL,
    schema_key VARCHAR(80) NOT NULL,
    schema_version VARCHAR(40) NOT NULL,
    schema_json JSON NOT NULL,
    category VARCHAR(80) NULL,
    description TEXT NULL,
    enabled TINYINT(1) NOT NULL DEFAULT 1,
    latest_version_no INT NOT NULL DEFAULT 0,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
    PRIMARY KEY (id),
    CONSTRAINT uq_scenario_template_code UNIQUE (scenario_code),
    CONSTRAINT chk_scenario_template_enabled CHECK (enabled IN (0, 1)),
    CONSTRAINT chk_scenario_template_version CHECK (latest_version_no >= 0)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- 后台长操作：先认领再执行，接口只返回受理状态。
CREATE TABLE background_operation (
    id BIGINT NOT NULL AUTO_INCREMENT,
    operation_no VARCHAR(40) NOT NULL,
    object_type VARCHAR(30) NOT NULL,
    object_no VARCHAR(40) NOT NULL,
    operation_type VARCHAR(40) NOT NULL,
    idempotency_key VARCHAR(120) NOT NULL,
    target_node VARCHAR(40) NULL,
    input_version VARCHAR(120) NULL,
    request_hash CHAR(64) NULL,
    phase VARCHAR(40) NULL,
    external_job_ref VARCHAR(120) NULL,
    status VARCHAR(30) NOT NULL,
    request_json JSON NOT NULL,
    result_json JSON NULL,
    raw_response_path VARCHAR(500) NULL,
    failure_code VARCHAR(80) NULL,
    failure_message TEXT NULL,
    started_at DATETIME(6) NULL,
    finished_at DATETIME(6) NULL,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
    PRIMARY KEY (id),
    CONSTRAINT uq_background_operation_no UNIQUE (operation_no),
    CONSTRAINT uq_background_operation_idempotency
        UNIQUE (object_type, object_no, operation_type, idempotency_key),
    KEY idx_background_operation_object (object_type, object_no, status),
    KEY idx_background_operation_status (status, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;


-- 2. 规则库导入

CREATE TABLE import_job (
    id BIGINT NOT NULL AUTO_INCREMENT,
    job_no VARCHAR(40) NOT NULL,
    source_filename VARCHAR(255) NOT NULL,
    source_path VARCHAR(500) NOT NULL,
    source_sha256 CHAR(64) NOT NULL,
    source_size BIGINT NOT NULL,
    profile_version VARCHAR(40) NOT NULL,
    schema_key VARCHAR(80) NOT NULL,
    schema_version VARCHAR(40) NOT NULL,
    schema_json JSON NOT NULL,
    scenario_code VARCHAR(80) NULL,
    profile_display_version VARCHAR(40) NULL,
    status VARCHAR(40) NOT NULL,
    -- 两轮模型解析后的业务配置候选。
    proposals_json JSON NULL,
    draft_revision INT NOT NULL DEFAULT 0,
    failure_code VARCHAR(80) NULL,
    failure_message TEXT NULL,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
    PRIMARY KEY (id),
    CONSTRAINT uq_import_job_job_no UNIQUE (job_no),
    CONSTRAINT chk_import_job_source_size CHECK (source_size >= 0),
    KEY idx_import_job_status_created_at (status, created_at),
    -- 大 JSON 行按时间分页时避免 filesort。
    KEY idx_import_job_created_at_id (created_at, id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;


-- 3. 已发布场景版本

CREATE TABLE scenario_version (
    id BIGINT NOT NULL AUTO_INCREMENT,
    import_job_id BIGINT NULL,
    scenario_code VARCHAR(80) NOT NULL,
    scenario_name VARCHAR(255) NOT NULL,
    schema_key VARCHAR(80) NOT NULL,
    schema_version VARCHAR(40) NOT NULL,
    schema_json JSON NOT NULL,
    version_no INT NOT NULL,
    status VARCHAR(30) NOT NULL,
    source VARCHAR(20) NOT NULL DEFAULT 'IMPORT',
    base_version_id BIGINT NULL,
    change_summary VARCHAR(500) NULL,
    -- 发布时刻的不可变完整配置。
    manifest_json JSON NOT NULL,
    published_at DATETIME(6) NOT NULL,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
    PRIMARY KEY (id),
    CONSTRAINT uq_scenario_version_code_no UNIQUE (scenario_code, version_no),
    CONSTRAINT uq_scenario_version_import_job UNIQUE (import_job_id),
    CONSTRAINT chk_scenario_version_no CHECK (version_no >= 1),
    CONSTRAINT fk_scenario_version_import_job
        FOREIGN KEY (import_job_id) REFERENCES import_job (id)
        ON DELETE RESTRICT ON UPDATE RESTRICT,
    CONSTRAINT fk_scenario_version_base
        FOREIGN KEY (base_version_id) REFERENCES scenario_version (id)
        ON DELETE RESTRICT ON UPDATE RESTRICT,
    KEY idx_scenario_version_code_status (scenario_code, status, published_at),
    KEY idx_scenario_version_base (base_version_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- 执行容器节点；audit_task.current_node 通过复合外键指向这里。
CREATE TABLE scenario_node (
    id BIGINT NOT NULL AUTO_INCREMENT,
    scenario_version_id BIGINT NOT NULL,
    node_code VARCHAR(80) NOT NULL,
    node_type VARCHAR(40) NOT NULL,
    name VARCHAR(120) NOT NULL,
    sequence_no INT NOT NULL,
    depends_on JSON NOT NULL,
    config_json JSON NULL,
    enabled TINYINT(1) NOT NULL,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
    PRIMARY KEY (id),
    CONSTRAINT uq_scenario_node_version_code UNIQUE (scenario_version_id, node_code),
    CONSTRAINT uq_scenario_node_version_sequence UNIQUE (scenario_version_id, sequence_no),
    CONSTRAINT chk_scenario_node_sequence CHECK (sequence_no >= 1),
    CONSTRAINT chk_scenario_node_enabled CHECK (enabled IN (0, 1)),
    CONSTRAINT fk_scenario_node_version
        FOREIGN KEY (scenario_version_id) REFERENCES scenario_version (id)
        ON DELETE RESTRICT ON UPDATE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;


-- 4. 场景草稿、方案、操作日志

CREATE TABLE scenario_draft (
    id BIGINT NOT NULL AUTO_INCREMENT,
    scenario_code VARCHAR(80) NOT NULL,
    schema_key VARCHAR(80) NOT NULL,
    schema_version VARCHAR(40) NOT NULL,
    schema_json JSON NOT NULL,
    revision_no INT NOT NULL DEFAULT 0,
    source VARCHAR(20) NOT NULL DEFAULT 'FORM',
    base_version_no INT NULL,
    profile_version VARCHAR(40) NULL,
    -- 最近一次保存时的编译失败原因；NULL 表示可发布。
    sync_error JSON NULL,
    -- 节点、材料、字段、规则、计算与报告配置。
    config_json JSON NOT NULL,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
    PRIMARY KEY (id),
    CONSTRAINT uq_scenario_draft_code UNIQUE (scenario_code),
    CONSTRAINT chk_scenario_draft_revision CHECK (revision_no >= 0),
    CONSTRAINT chk_scenario_draft_base CHECK (base_version_no IS NULL OR base_version_no >= 1)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- 方案：场景版本的可编辑副本；manifest_json 是 config_json 的编译结果。
CREATE TABLE audit_plan (
    id BIGINT NOT NULL AUTO_INCREMENT,
    plan_code VARCHAR(80) NOT NULL,
    plan_name VARCHAR(255) NOT NULL,
    scenario_code VARCHAR(80) NOT NULL,
    scenario_version_id BIGINT NULL,
    description TEXT NULL,
    enabled TINYINT(1) NOT NULL DEFAULT 1,
    deleted_at DATETIME(6) NULL,
    config_json JSON NOT NULL,
    manifest_json JSON NULL,
    -- 最近一次编译失败原因；NULL 表示范围已生效。
    sync_error JSON NULL,
    -- 范围指纹，创建任务时校验预览范围与提交范围一致。
    scope_hash CHAR(64) NULL,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
    PRIMARY KEY (id),
    CONSTRAINT uq_audit_plan_code UNIQUE (plan_code),
    CONSTRAINT fk_audit_plan_scenario_version
        FOREIGN KEY (scenario_version_id) REFERENCES scenario_version (id)
        ON DELETE RESTRICT ON UPDATE RESTRICT,
    KEY idx_audit_plan_scenario (scenario_code, deleted_at),
    KEY idx_audit_plan_deleted (deleted_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- 已提交的同步业务写操作；后台长操作见 background_operation。
CREATE TABLE operation_log (
    id BIGINT NOT NULL AUTO_INCREMENT,
    log_no VARCHAR(40) NOT NULL,
    object_type VARCHAR(30) NOT NULL,
    object_no VARCHAR(40) NOT NULL,
    object_name VARCHAR(255) NOT NULL,
    operation_type VARCHAR(40) NOT NULL,
    result VARCHAR(20) NOT NULL DEFAULT 'SUCCEEDED',
    idempotency_key VARCHAR(120) NULL,
    request_hash CHAR(64) NULL,
    duration_ms INT NULL,
    command_no VARCHAR(40) NULL,
    -- 是否永久删除。
    permanent TINYINT(1) NOT NULL DEFAULT 0,
    detail_json JSON NULL,
    failure_code VARCHAR(80) NULL,
    failure_message TEXT NULL,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
    PRIMARY KEY (id),
    CONSTRAINT uq_operation_log_no UNIQUE (log_no),
    CONSTRAINT uq_operation_log_idempotency UNIQUE (object_type, operation_type, idempotency_key),
    CONSTRAINT chk_operation_log_permanent CHECK (permanent IN (0, 1)),
    KEY idx_operation_log_object (object_type, object_no, created_at),
    KEY idx_operation_log_created (created_at),
    KEY idx_operation_log_delete (object_type, object_no, operation_type, permanent)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;


-- 5. 审核任务与报告

CREATE TABLE audit_task (
    id BIGINT NOT NULL AUTO_INCREMENT,
    task_no VARCHAR(40) NOT NULL,
    scenario_version_id BIGINT NOT NULL,
    plan_id BIGINT NULL,
    task_name VARCHAR(255) NOT NULL,
    remark TEXT NULL,
    status VARCHAR(40) NOT NULL,
    current_node VARCHAR(80) NOT NULL,
    -- 当前阶段；节点序号由 current_node 推算。
    current_phase VARCHAR(40) NOT NULL DEFAULT 'AWAITING_CONFIRMATION',
    -- 任务冻结的执行配置，执行期只读。
    manifest_json JSON NOT NULL,
    -- 本次生效范围：启用规则与字段清单。
    task_schema_json JSON NOT NULL,
    -- 任务 CSV 清单：格式版本、current 四表、记录数；旧格式读取时明确报错。
    value_file_json JSON NULL,
    -- 输入修订号；替换材料或人工改值时递增。
    input_revision INT NOT NULL DEFAULT 0,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
    PRIMARY KEY (id),
    CONSTRAINT uq_audit_task_task_no UNIQUE (task_no),
    CONSTRAINT uq_audit_task_id_scenario_version UNIQUE (id, scenario_version_id),
    CONSTRAINT fk_audit_task_scenario_version
        FOREIGN KEY (scenario_version_id) REFERENCES scenario_version (id)
        ON DELETE RESTRICT ON UPDATE RESTRICT,
    CONSTRAINT fk_audit_task_plan
        FOREIGN KEY (plan_id) REFERENCES audit_plan (id)
        ON DELETE RESTRICT ON UPDATE RESTRICT,
    CONSTRAINT fk_audit_task_current_node
        FOREIGN KEY (scenario_version_id, current_node)
        REFERENCES scenario_node (scenario_version_id, node_code)
        ON DELETE RESTRICT ON UPDATE RESTRICT,
    KEY idx_audit_task_version_status (scenario_version_id, status),
    KEY idx_audit_task_status_phase (status, current_phase),
    KEY idx_audit_task_plan (plan_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- 报告快照；重新生成是新快照，历史保留。
CREATE TABLE audit_report (
    id BIGINT NOT NULL AUTO_INCREMENT,
    report_no VARCHAR(40) NOT NULL,
    audit_task_id BIGINT NOT NULL,
    node_execution_id BIGINT NULL,
    report_name VARCHAR(255) NOT NULL,
    status VARCHAR(30) NOT NULL DEFAULT 'GENERATED',
    content_type VARCHAR(60) NOT NULL DEFAULT 'text/markdown',
    content_md MEDIUMTEXT NULL,
    report_generated TINYINT(1) NOT NULL DEFAULT 0,
    scope_execution_complete TINYINT(1) NOT NULL DEFAULT 0,
    risk_conclusion_available TINYINT(1) NOT NULL DEFAULT 0,
    -- 未完成项与格式支持说明。
    reasons_json JSON NULL,
    -- 生成时引用的结果 id 与输入修订号。
    snapshot_json JSON NULL,
    input_revision INT NULL,
    remark TEXT NULL,
    archived_at DATETIME(6) NULL,
    generated_at DATETIME(6) NOT NULL,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
    PRIMARY KEY (id),
    CONSTRAINT uq_audit_report_no UNIQUE (report_no),
    CONSTRAINT fk_audit_report_task
        FOREIGN KEY (audit_task_id) REFERENCES audit_task (id)
        ON DELETE RESTRICT ON UPDATE RESTRICT,
    KEY idx_audit_report_task_generated (audit_task_id, generated_at),
    KEY idx_audit_report_generated (generated_at),
    KEY idx_audit_report_status (status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;


-- 6. 材料位、上传与处理产物

CREATE TABLE task_document (
    id BIGINT NOT NULL AUTO_INCREMENT,
    document_no VARCHAR(40) NOT NULL,
    audit_task_id BIGINT NOT NULL,
    material_key VARCHAR(40) NOT NULL,
    material_name VARCHAR(255) NOT NULL,
    required TINYINT(1) NOT NULL,
    current_upload_id BIGINT NULL,
    status VARCHAR(30) NOT NULL,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
    PRIMARY KEY (id),
    CONSTRAINT uq_task_document_no UNIQUE (document_no),
    CONSTRAINT uq_task_document_task_material UNIQUE (audit_task_id, material_key),
    CONSTRAINT uq_task_document_id_task UNIQUE (id, audit_task_id),
    CONSTRAINT chk_task_document_required CHECK (required IN (0, 1)),
    CONSTRAINT fk_task_document_task
        FOREIGN KEY (audit_task_id) REFERENCES audit_task (id)
        ON DELETE RESTRICT ON UPDATE RESTRICT,
    KEY idx_task_document_current_upload (current_upload_id, id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- 上传版本：追加写入，current_task_document_id 保证每个材料位只有一条当前记录。
CREATE TABLE document_upload (
    id BIGINT NOT NULL AUTO_INCREMENT,
    upload_no VARCHAR(40) NOT NULL,
    task_document_id BIGINT NOT NULL,
    audit_task_id BIGINT NOT NULL,
    original_filename VARCHAR(255) NOT NULL,
    relative_path VARCHAR(500) NOT NULL,
    content_type VARCHAR(120) NOT NULL,
    declared_material_type VARCHAR(80) NOT NULL,
    size_bytes BIGINT NOT NULL,
    sha256 CHAR(64) NOT NULL,
    status VARCHAR(30) NOT NULL,
    is_current TINYINT(1) NOT NULL,
    current_task_document_id BIGINT
        GENERATED ALWAYS AS (IF(is_current = 1, task_document_id, NULL)) STORED,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
    PRIMARY KEY (id),
    CONSTRAINT uq_document_upload_document_no UNIQUE (task_document_id, upload_no),
    CONSTRAINT uq_document_upload_id_document UNIQUE (id, task_document_id),
    CONSTRAINT uq_document_upload_id_task UNIQUE (id, audit_task_id),
    CONSTRAINT uq_document_upload_one_current UNIQUE (current_task_document_id),
    CONSTRAINT chk_document_upload_size CHECK (size_bytes >= 0),
    CONSTRAINT chk_document_upload_current CHECK (is_current IN (0, 1)),
    CONSTRAINT fk_document_upload_task_document_task
        FOREIGN KEY (task_document_id, audit_task_id)
        REFERENCES task_document (id, audit_task_id)
        ON DELETE RESTRICT ON UPDATE RESTRICT,
    KEY idx_document_upload_document_current (task_document_id, is_current),
    KEY idx_document_upload_sha256 (sha256)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- 外部处理产物索引；正文在 relative_path 指向的文件，不在数据库。
CREATE TABLE stored_artifact (
    id BIGINT NOT NULL AUTO_INCREMENT,
    artifact_no VARCHAR(40) NOT NULL,
    document_upload_id BIGINT NOT NULL,
    audit_task_id BIGINT NOT NULL,
    artifact_type VARCHAR(40) NOT NULL,
    relative_path VARCHAR(500) NOT NULL,
    upload_sha256 CHAR(64) NOT NULL,
    content_sha256 CHAR(64) NULL,
    status VARCHAR(30) NOT NULL,
    -- 外部作业号与截止时间，结果未知时保留最后一次已受理的作业。
    external_job_id VARCHAR(120) NULL,
    -- UTC 时间；ORM 写入前转 UTC，读取后恢复 UTC 时区。
    deadline_at DATETIME(6) NULL,
    failure_code VARCHAR(80) NULL,
    failure_message TEXT NULL,
    -- 只保存 processing_attempts / submit_attempts 等调用摘要。
    metadata_json JSON NULL,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
    PRIMARY KEY (id),
    CONSTRAINT uq_stored_artifact_no UNIQUE (artifact_no),
    CONSTRAINT uq_stored_artifact_upload_type_hash UNIQUE (document_upload_id, artifact_type, upload_sha256),
    CONSTRAINT uq_stored_artifact_id_task UNIQUE (id, audit_task_id),
    CONSTRAINT fk_stored_artifact_upload_task
        FOREIGN KEY (document_upload_id, audit_task_id)
        REFERENCES document_upload (id, audit_task_id)
        ON DELETE RESTRICT ON UPDATE RESTRICT,
    KEY idx_stored_artifact_upload_status (document_upload_id, status),
    KEY idx_stored_artifact_content (document_upload_id, artifact_type, content_sha256),
    -- 启动时按状态恢复中断的提交。
    KEY idx_stored_artifact_status (status, updated_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;


-- 7. 执行批次、取值索引与规则结果

-- 节点执行批次；result_json 只存批次状态与进度汇总。
CREATE TABLE node_execution (
    id BIGINT NOT NULL AUTO_INCREMENT,
    audit_task_id BIGINT NOT NULL,
    scenario_version_id BIGINT NOT NULL,
    node_code VARCHAR(80) NOT NULL,
    sequence_no INT NOT NULL,
    status VARCHAR(30) NOT NULL,
    started_at DATETIME(6) NULL,
    finished_at DATETIME(6) NULL,
    result_json JSON NULL,
    failure_code VARCHAR(80) NULL,
    failure_message TEXT NULL,
    -- 被重试或换版取代时保留原状态与原因。
    invalidated_from VARCHAR(30) NULL,
    invalidated_at DATETIME(6) NULL,
    invalidation_reason VARCHAR(200) NULL,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
    PRIMARY KEY (id),
    CONSTRAINT uq_node_execution_task_sequence UNIQUE (audit_task_id, sequence_no),
    CONSTRAINT uq_node_execution_id_task UNIQUE (id, audit_task_id),
    CONSTRAINT chk_node_execution_sequence CHECK (sequence_no >= 1),
    CONSTRAINT fk_node_execution_task_version
        FOREIGN KEY (audit_task_id, scenario_version_id)
        REFERENCES audit_task (id, scenario_version_id)
        ON DELETE RESTRICT ON UPDATE RESTRICT,
    CONSTRAINT fk_node_execution_version_node
        FOREIGN KEY (scenario_version_id, node_code)
        REFERENCES scenario_node (scenario_version_id, node_code)
        ON DELETE RESTRICT ON UPDATE RESTRICT,
    KEY idx_node_execution_task_node (audit_task_id, node_code),
    KEY idx_node_execution_task_status (audit_task_id, status),
    KEY idx_node_execution_display_status (status, invalidated_from)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- 字段候选版本索引；取值（原文、候选值、规范值）在任务 CSV。
CREATE TABLE task_field_value (
    id BIGINT NOT NULL AUTO_INCREMENT,
    audit_task_id BIGINT NOT NULL,
    scenario_version_id BIGINT NOT NULL,
    field_key VARCHAR(40) NOT NULL,
    material_key VARCHAR(40) NOT NULL,
    document_upload_id BIGINT NOT NULL,
    revision_no INT NOT NULL DEFAULT 1,
    is_current TINYINT(1) NOT NULL DEFAULT 1,
    status VARCHAR(40) NOT NULL,
    extraction_execution_id BIGINT NULL,
    origin VARCHAR(30) NOT NULL DEFAULT 'DOCUMENT_EXTRACTION',
    -- 只有当前行有值，唯一键保证同一身份只有一条当前记录。
    current_fact_scope VARCHAR(120) GENERATED ALWAYS AS (
        IF(
            is_current = 1,
            CONCAT(
                CHAR_LENGTH(field_key), ':', field_key,
                CHAR_LENGTH(material_key), ':', material_key
            ),
            NULL
        )
    ) VIRTUAL,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
    PRIMARY KEY (id),
    CONSTRAINT uq_task_field_value_revision
        UNIQUE (audit_task_id, field_key, material_key, document_upload_id, revision_no),
    CONSTRAINT uq_task_field_value_current
        UNIQUE (audit_task_id, current_fact_scope),
    CONSTRAINT uq_task_field_value_id_task UNIQUE (id, audit_task_id),
    CONSTRAINT chk_task_field_value_revision CHECK (revision_no >= 1),
    CONSTRAINT chk_task_field_value_current CHECK (is_current IN (0, 1)),
    CONSTRAINT fk_task_field_value_task_version
        FOREIGN KEY (audit_task_id, scenario_version_id)
        REFERENCES audit_task (id, scenario_version_id)
        ON DELETE RESTRICT ON UPDATE RESTRICT,
    CONSTRAINT fk_task_field_value_task_material
        FOREIGN KEY (audit_task_id, material_key)
        REFERENCES task_document (audit_task_id, material_key)
        ON DELETE RESTRICT ON UPDATE RESTRICT,
    CONSTRAINT fk_task_field_value_upload_task
        FOREIGN KEY (document_upload_id, audit_task_id)
        REFERENCES document_upload (id, audit_task_id)
        ON DELETE RESTRICT ON UPDATE RESTRICT,
    CONSTRAINT fk_task_field_value_execution_task
        FOREIGN KEY (extraction_execution_id, audit_task_id)
        REFERENCES node_execution (id, audit_task_id)
        ON DELETE RESTRICT ON UPDATE RESTRICT,
    KEY idx_task_field_lookup (audit_task_id, field_key, material_key, document_upload_id),
    KEY idx_task_field_version (audit_task_id, scenario_version_id),
    KEY idx_task_field_material (audit_task_id, material_key),
    KEY idx_task_field_current (audit_task_id, is_current)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- 统一事实索引（字段抽取与计算结果）；取值在任务 CSV。
CREATE TABLE task_fact (
    id BIGINT NOT NULL AUTO_INCREMENT,
    audit_task_id BIGINT NOT NULL,
    scenario_version_id BIGINT NOT NULL,
    -- EXTRACTED 用字段编号，DERIVED 用计算结果编号。
    fact_code VARCHAR(80) NOT NULL,
    fact_kind VARCHAR(20) NOT NULL,
    material_key VARCHAR(40) NULL,
    entity_scope VARCHAR(20) NOT NULL DEFAULT 'SELF',
    period_scope VARCHAR(30) NOT NULL,
    statement_scope VARCHAR(20) NOT NULL,
    value_type VARCHAR(20) NOT NULL,
    source_execution_id BIGINT NULL,
    calculation_status VARCHAR(30) NULL,
    calculation_rule_code VARCHAR(80) NULL,
    revision_no INT NOT NULL DEFAULT 1,
    is_current TINYINT(1) NOT NULL DEFAULT 1,
    status VARCHAR(40) NOT NULL,
    -- 唯一键不比较 NULL，用空串代替以覆盖无材料归属的计算事实。
    material_scope VARCHAR(40) GENERATED ALWAYS AS (IFNULL(material_key, '')) VIRTUAL,
    -- 五个身份维度长度前缀编码；NULL 用 -1: 表示。
    current_fact_scope VARCHAR(255) GENERATED ALWAYS AS (
        IF(
            is_current = 1,
            CONCAT(
                CHAR_LENGTH(fact_code), ':', fact_code,
                IF(material_key IS NULL, '-1:', CONCAT(CHAR_LENGTH(material_key), ':', material_key)),
                CHAR_LENGTH(period_scope), ':', period_scope,
                CHAR_LENGTH(statement_scope), ':', statement_scope,
                CHAR_LENGTH(entity_scope), ':', entity_scope
            ),
            NULL
        )
    ) VIRTUAL,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
    PRIMARY KEY (id),
    CONSTRAINT uq_task_fact_revision UNIQUE (
        audit_task_id, fact_code, material_scope, period_scope, statement_scope, entity_scope, revision_no
    ),
    CONSTRAINT uq_task_fact_current UNIQUE (audit_task_id, current_fact_scope),
    CONSTRAINT uq_task_fact_id_task UNIQUE (id, audit_task_id),
    CONSTRAINT chk_task_fact_revision CHECK (revision_no >= 1),
    CONSTRAINT chk_task_fact_current CHECK (is_current IN (0, 1)),
    CONSTRAINT chk_task_fact_material CHECK (material_key IS NULL OR material_key <> ''),
    CONSTRAINT fk_task_fact_task
        FOREIGN KEY (audit_task_id, scenario_version_id)
        REFERENCES audit_task (id, scenario_version_id)
        ON DELETE RESTRICT ON UPDATE RESTRICT,
    CONSTRAINT fk_task_fact_material
        FOREIGN KEY (audit_task_id, material_key)
        REFERENCES task_document (audit_task_id, material_key)
        ON DELETE RESTRICT ON UPDATE RESTRICT,
    CONSTRAINT fk_task_fact_execution_task
        FOREIGN KEY (source_execution_id, audit_task_id)
        REFERENCES node_execution (id, audit_task_id)
        ON DELETE RESTRICT ON UPDATE RESTRICT,
    KEY idx_task_fact_lookup (audit_task_id, fact_code, period_scope, statement_scope, entity_scope),
    KEY idx_task_fact_kind (audit_task_id, fact_kind, is_current),
    KEY idx_task_fact_current (audit_task_id, is_current),
    KEY idx_task_fact_rule (audit_task_id, calculation_rule_code),
    KEY idx_task_fact_material (audit_task_id, material_key)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- 规则结论索引；产出、依据、缺项在任务 CSV。
CREATE TABLE rule_result (
    id BIGINT NOT NULL AUTO_INCREMENT,
    audit_task_id BIGINT NOT NULL,
    node_execution_id BIGINT NOT NULL,
    rule_key VARCHAR(80) NOT NULL,
    rule_code VARCHAR(80) NULL,
    rule_name VARCHAR(255) NOT NULL,
    status VARCHAR(40) NOT NULL,
    message TEXT NOT NULL,
    risk_level VARCHAR(40) NULL,
    loop_status VARCHAR(30) NULL,
    turn_count INT NOT NULL DEFAULT 0,
    -- 小体积诊断：审核标准、输入引用、未解决事项。
    result_json JSON NULL,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
    PRIMARY KEY (id),
    CONSTRAINT uq_rule_result_execution_rule UNIQUE (node_execution_id, rule_key),
    CONSTRAINT uq_rule_result_id_task UNIQUE (id, audit_task_id),
    CONSTRAINT chk_rule_result_turn_count CHECK (turn_count >= 0),
    CONSTRAINT fk_rule_result_task
        FOREIGN KEY (audit_task_id) REFERENCES audit_task (id)
        ON DELETE RESTRICT ON UPDATE RESTRICT,
    CONSTRAINT fk_rule_result_execution_task
        FOREIGN KEY (node_execution_id, audit_task_id)
        REFERENCES node_execution (id, audit_task_id)
        ON DELETE RESTRICT ON UPDATE RESTRICT,
    KEY idx_rule_result_task_status (audit_task_id, status),
    KEY idx_rule_result_task_rule (audit_task_id, rule_key),
    KEY idx_rule_result_task_code (audit_task_id, rule_code),
    KEY idx_rule_result_task_risk_level (audit_task_id, risk_level)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- 人工复核与纠错记录。
CREATE TABLE manual_review (
    id BIGINT NOT NULL AUTO_INCREMENT,
    review_no VARCHAR(40) NOT NULL,
    audit_task_id BIGINT NOT NULL,
    document_upload_id BIGINT NULL,
    rule_result_id BIGINT NULL,
    field_value_id BIGINT NULL,
    review_type VARCHAR(40) NOT NULL,
    status VARCHAR(30) NOT NULL,
    system_judgment_json JSON NOT NULL,
    decision VARCHAR(40) NULL,
    business_note TEXT NULL,
    proposed_value TEXT NULL,
    before_value TEXT NULL,
    after_value TEXT NULL,
    basis_type VARCHAR(30) NULL,
    decision_command_no VARCHAR(40) NULL,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
    PRIMARY KEY (id),
    CONSTRAINT uq_manual_review_no UNIQUE (review_no),
    CONSTRAINT fk_manual_review_task
        FOREIGN KEY (audit_task_id) REFERENCES audit_task (id)
        ON DELETE RESTRICT ON UPDATE RESTRICT,
    CONSTRAINT fk_manual_review_upload_task
        FOREIGN KEY (document_upload_id, audit_task_id)
        REFERENCES document_upload (id, audit_task_id)
        ON DELETE RESTRICT ON UPDATE RESTRICT,
    CONSTRAINT fk_manual_review_rule_result_task
        FOREIGN KEY (rule_result_id, audit_task_id)
        REFERENCES rule_result (id, audit_task_id)
        ON DELETE RESTRICT ON UPDATE RESTRICT,
    CONSTRAINT fk_manual_review_field_value_task
        FOREIGN KEY (field_value_id, audit_task_id)
        REFERENCES task_field_value (id, audit_task_id)
        ON DELETE RESTRICT ON UPDATE RESTRICT,
    KEY idx_manual_review_task_status (audit_task_id, status),
    KEY idx_manual_review_upload (document_upload_id),
    KEY idx_manual_review_rule_result (rule_result_id),
    KEY idx_manual_review_field_value (field_value_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;


-- 8. 延迟添加的复合外键（材料位与上传互相引用）

ALTER TABLE task_document
    ADD CONSTRAINT fk_task_document_current_upload
    FOREIGN KEY (current_upload_id, id) REFERENCES document_upload (id, task_document_id)
    ON DELETE RESTRICT ON UPDATE RESTRICT;


-- 9. 结构确认：预期不返回任何行。

SELECT expected.table_name AS missing_table
FROM (
SELECT 'scenario_template' AS table_name
    UNION ALL SELECT 'background_operation'
    UNION ALL SELECT 'import_job'
    UNION ALL SELECT 'scenario_version'
    UNION ALL SELECT 'scenario_node'
    UNION ALL SELECT 'scenario_draft'
    UNION ALL SELECT 'audit_plan'
    UNION ALL SELECT 'audit_task'
    UNION ALL SELECT 'audit_report'
    UNION ALL SELECT 'operation_log'
    UNION ALL SELECT 'task_document'
    UNION ALL SELECT 'document_upload'
    UNION ALL SELECT 'stored_artifact'
    UNION ALL SELECT 'node_execution'
    UNION ALL SELECT 'task_field_value'
    UNION ALL SELECT 'task_fact'
    UNION ALL SELECT 'rule_result'
    UNION ALL SELECT 'manual_review'
) AS expected
LEFT JOIN information_schema.tables AS actual
       ON actual.table_schema = DATABASE()
      AND actual.table_name = expected.table_name
WHERE actual.table_name IS NULL
ORDER BY expected.table_name;


-- 10. 状态取值（取值以代码常量为准）
--
-- audit_task.status            READY / RUNNING / BLOCKED / FAILED / COMPLETED
-- audit_task.current_phase     AWAITING_CONFIRMATION / RUNNING / COMPLETED
-- background_operation.status  PENDING / RUNNING / SUCCEEDED / FAILED / INTERRUPTED
-- import_job.status            PARSED / PROPOSED / APPLIED / EXTERNAL_FAILED
-- task_document.status         WAITING_UPLOAD / UPLOADED / PREPARED
-- document_upload.status       UPLOADED / SUPERSEDED / REMOVED
-- stored_artifact.status       PROCESSING / SUBMITTING / PENDING / READY / FAILED / INVALIDATED
-- node_execution.status        RUNNING / SUCCEEDED / BLOCKED / FAILED / SKIPPED / INVALIDATED
-- task_field_value.origin      DOCUMENT_EXTRACTION / HUMAN_CORRECTION
-- task_field_value.status      PRESENT / NOT_PRESENT / EVIDENCE_INSUFFICIENT / REVIEWED
-- task_fact.fact_kind          EXTRACTED / DERIVED
-- rule_result.status           COMPUTED / PASSED / ABNORMAL / EVIDENCE_INSUFFICIENT /
--                              NOT_APPLICABLE / NOT_EXECUTED / PENDING_REVIEW / REVIEWED /
--                              SKIPPED / RUNNING
-- rule_result.loop_status      RUNNING / DONE / BLOCKED / NEEDS_HUMAN
-- audit_report.status          GENERATED / ARCHIVED
-- manual_review.status         PENDING / RESOLVED / INVALIDATED
