-- baoli-v4 结构设计稿
--
-- 相对 schema.sql 的取舍：
--   1. 身份只用 id，不再额外维护 *_no / *_code 这类公开编号列。
--   2. 能从已有列算出来的不存：哈希指纹、恒定的布尔标记、可推导的状态。
--   3. 同一份内容只存一次；编译产物（manifest）与源配置（config）分开。
--   4. 后台操作与操作日志的请求摘要改用 request_json 现算，不再单列哈希。
-- 时间列统一 DATETIME(6)，由 ORM 按 UTC 存取。

-- 场景：身份、工作副本与版本号分配器。
-- 原 scenario_template 与 scenario_draft 按 scenario_code 一一对应，合并成一张。
CREATE TABLE scenario (
    id BIGINT NOT NULL AUTO_INCREMENT,
    scenario_name VARCHAR(255) NOT NULL,
    category VARCHAR(80) NULL,
    description TEXT NULL,
    enabled TINYINT(1) NOT NULL DEFAULT 1,

    -- 场景版本身份：schema_key / schema_version / profile_version 都在这份 JSON 里。
    schema_json JSON NOT NULL,
    -- 工作副本配置：节点、材料、字段、规则与报告章节。
    config_json JSON NOT NULL,
    -- 最近一次保存时的编译失败原因；为空表示可以发布。
    sync_error JSON NULL,
    revision_no INT NOT NULL DEFAULT 0,
    source VARCHAR(20) NOT NULL DEFAULT 'FORM',
    base_version_no INT NULL,

    -- 版本号分配器：发布时在同一事务内自增，避免并发取到同一个版本号。
    latest_version_no INT NOT NULL DEFAULT 0,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
    PRIMARY KEY (id),
    CONSTRAINT chk_scenario_enabled CHECK (enabled IN (0, 1)),
    CONSTRAINT chk_scenario_revision CHECK (revision_no >= 0)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;


-- 后台长操作：先认领再执行，接口只返回受理状态。
CREATE TABLE background_operation (
    id BIGINT NOT NULL AUTO_INCREMENT,
    -- 被操作对象的类型与编号；对象可能属于多张表，只用类型区分，不建外键。
    object_type VARCHAR(30) NOT NULL,
    object_id BIGINT NOT NULL,
    operation_type VARCHAR(40) NOT NULL,
    -- 页面生成的幂等标识；同一对象同一操作下，标识相同才算同一次操作。
    idempotency_key VARCHAR(120) NOT NULL,
    target_node VARCHAR(40) NULL,
    input_version VARCHAR(120) NULL,
    -- 命令停在哪一步；恢复时按它决定唯一推进动作。
    phase VARCHAR(40) NULL,
    -- 外部作业引用（如 OCR jobId）：本地超时后只能续查同一个作业。
    external_job_ref VARCHAR(120) NULL,
    status VARCHAR(30) NOT NULL,
    -- 请求原文与结果；幂等冲突时用请求原文现算摘要比对。
    request_json JSON NOT NULL,
    result_json JSON NULL,
    failure_code VARCHAR(80) NULL,
    failure_message TEXT NULL,
    started_at DATETIME(6) NULL,
    finished_at DATETIME(6) NULL,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
    PRIMARY KEY (id),
    CONSTRAINT uq_background_operation_idempotency
        UNIQUE (object_type, object_id, operation_type, idempotency_key),
    KEY idx_background_operation_object (object_type, object_id, status),
    KEY idx_background_operation_status (status, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;


-- 规则库导入：一份工作簿一次解析作业。
CREATE TABLE import_job (
    id BIGINT NOT NULL AUTO_INCREMENT,
    scenario_id BIGINT NULL,
    source_filename VARCHAR(255) NOT NULL,
    source_path VARCHAR(500) NOT NULL,
    source_size BIGINT NOT NULL,
    -- 源文件哈希：续跑前核对文件没被换过。
    source_sha256 CHAR(64) NOT NULL,
    -- 上传时的场景 schema 快照，决定工作簿怎么解释。
    schema_json JSON NOT NULL,
    status VARCHAR(40) NOT NULL,
    -- 两轮模型解析后的业务配置候选。
    proposals_json JSON NULL,
    draft_revision INT NOT NULL DEFAULT 0,
    failure_code VARCHAR(80) NULL,
    failure_message TEXT NULL,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
    PRIMARY KEY (id),
    CONSTRAINT fk_import_job_scenario FOREIGN KEY (scenario_id) REFERENCES scenario (id)
        ON DELETE RESTRICT ON UPDATE RESTRICT,
    CONSTRAINT chk_import_job_source_size CHECK (source_size >= 0),
    KEY idx_import_job_status_created (status, created_at),
    KEY idx_import_job_created (created_at, id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;


-- 已发布场景版本：发布后不再修改，任务冻结的就是这里的 manifest_json。
CREATE TABLE scenario_version (
    id BIGINT NOT NULL AUTO_INCREMENT,
    scenario_id BIGINT NOT NULL,
    version_no INT NOT NULL,
    status VARCHAR(30) NOT NULL,
    source VARCHAR(20) NOT NULL DEFAULT 'IMPORT',
    -- 从哪一版改出来的；首个版本为空。
    base_version_id BIGINT NULL,
    import_job_id BIGINT NULL,
    change_summary VARCHAR(500) NULL,
    -- 发布时刻的完整配置，执行与报告渲染都读它。
    manifest_json JSON NOT NULL,
    published_at DATETIME(6) NOT NULL,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
    PRIMARY KEY (id),
    CONSTRAINT uq_scenario_version_no UNIQUE (scenario_id, version_no),
    CONSTRAINT uq_scenario_version_job UNIQUE (import_job_id),
    CONSTRAINT fk_scenario_version_scenario FOREIGN KEY (scenario_id) REFERENCES scenario (id)
        ON DELETE RESTRICT ON UPDATE RESTRICT,
    CONSTRAINT fk_scenario_version_base FOREIGN KEY (base_version_id) REFERENCES scenario_version (id)
        ON DELETE RESTRICT ON UPDATE RESTRICT,
    CONSTRAINT fk_scenario_version_job FOREIGN KEY (import_job_id) REFERENCES import_job (id)
        ON DELETE RESTRICT ON UPDATE RESTRICT,
    CONSTRAINT chk_scenario_version_no CHECK (version_no >= 1),
    KEY idx_scenario_version_list (scenario_id, status, published_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;


-- 执行容器节点：一个场景版本对应固定的一组节点。
CREATE TABLE scenario_node (
    id BIGINT NOT NULL AUTO_INCREMENT,
    scenario_version_id BIGINT NOT NULL,
    -- 节点实例码：同一能力类型可以有多个实例，manifest 按它引用。
    node_code VARCHAR(80) NOT NULL,
    node_type VARCHAR(40) NOT NULL,
    name VARCHAR(120) NOT NULL,
    sequence_no INT NOT NULL,
    depends_on JSON NOT NULL,
    config_json JSON NULL,
    enabled TINYINT(1) NOT NULL DEFAULT 1,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
    PRIMARY KEY (id),
    CONSTRAINT uq_scenario_node_code UNIQUE (scenario_version_id, node_code),
    CONSTRAINT uq_scenario_node_sequence UNIQUE (scenario_version_id, sequence_no),
    CONSTRAINT fk_scenario_node_version FOREIGN KEY (scenario_version_id) REFERENCES scenario_version (id)
        ON DELETE RESTRICT ON UPDATE RESTRICT,
    CONSTRAINT chk_scenario_node_sequence CHECK (sequence_no >= 1),
    CONSTRAINT chk_scenario_node_enabled CHECK (enabled IN (0, 1))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;


-- 审核方案：已发布场景版本的一份可编辑副本。
-- 用户改的是 config_json；保存时编译成 manifest_json；编译失败原因记在 sync_error。
CREATE TABLE audit_plan (
    id BIGINT NOT NULL AUTO_INCREMENT,
    plan_name VARCHAR(255) NOT NULL,
    scenario_version_id BIGINT NOT NULL,
    description TEXT NULL,
    enabled TINYINT(1) NOT NULL DEFAULT 1,
    deleted_at DATETIME(6) NULL,

    -- 用户在方案页编辑的配置：材料、字段、规则、报告章节。
    config_json JSON NOT NULL,
    -- 保存时由 config_json 编译出的执行格式：报告渲染与任务冻结都读它。
    -- 为空表示还没有编译成功。
    manifest_json JSON NULL,
    -- 最近一次编译失败的原因；为空表示当前范围已生效。
    sync_error JSON NULL,

    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
    PRIMARY KEY (id),
    CONSTRAINT fk_audit_plan_version FOREIGN KEY (scenario_version_id) REFERENCES scenario_version (id)
        ON DELETE RESTRICT ON UPDATE RESTRICT,
    KEY idx_audit_plan_version (scenario_version_id, deleted_at),
    KEY idx_audit_plan_deleted (deleted_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;


-- 已提交的同步业务写操作；后台长操作见 background_operation。
CREATE TABLE operation_log (
    id BIGINT NOT NULL AUTO_INCREMENT,
    object_type VARCHAR(30) NOT NULL,
    object_id BIGINT NOT NULL,
    -- 记录当时的对象名称，对象后来改名或删除也不影响这条历史。
    object_name VARCHAR(255) NOT NULL,
    operation_type VARCHAR(40) NOT NULL,
    result VARCHAR(20) NOT NULL DEFAULT 'SUCCEEDED',
    idempotency_key VARCHAR(120) NULL,
    duration_ms INT NULL,
    -- 受理了后台执行时指向那条命令。
    command_id BIGINT NULL,
    permanent TINYINT(1) NOT NULL DEFAULT 0,
    detail_json JSON NULL,
    failure_code VARCHAR(80) NULL,
    failure_message TEXT NULL,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
    PRIMARY KEY (id),
    CONSTRAINT uq_operation_log_idempotency UNIQUE (object_type, operation_type, idempotency_key),
    CONSTRAINT fk_operation_log_command FOREIGN KEY (command_id) REFERENCES background_operation (id)
        ON DELETE RESTRICT ON UPDATE RESTRICT,
    CONSTRAINT chk_operation_log_permanent CHECK (permanent IN (0, 1)),
    KEY idx_operation_log_object (object_type, object_id, created_at),
    KEY idx_operation_log_created (created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;


-- 审核任务：冻结方案范围后逐节点执行。
CREATE TABLE audit_task (
    id BIGINT NOT NULL AUTO_INCREMENT,
    scenario_version_id BIGINT NOT NULL,
    plan_id BIGINT NULL,
    task_name VARCHAR(255) NOT NULL,
    remark TEXT NULL,
    status VARCHAR(40) NOT NULL,
    current_node VARCHAR(80) NOT NULL,
    -- 任务停在确认点还是已经跑完；只表达阶段，不重复 status。
    current_phase VARCHAR(40) NOT NULL DEFAULT 'AWAITING_CONFIRMATION',

    -- 任务冻结的完整配置，执行期只读。
    manifest_json JSON NOT NULL,
    -- 本任务实际启用的规则；字段与依赖由 manifest 现算，不再冻结第二份 schema。
    enabled_rule_keys JSON NOT NULL,
    -- 任务取值 CSV 的格式版本；旧格式读取时明确报错。
    value_format_version SMALLINT NOT NULL DEFAULT 4,

    -- 输入修订号：补传材料、替换材料或人工改值都会递增。
    input_revision INT NOT NULL DEFAULT 0,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
    PRIMARY KEY (id),
    CONSTRAINT fk_audit_task_version FOREIGN KEY (scenario_version_id) REFERENCES scenario_version (id)
        ON DELETE RESTRICT ON UPDATE RESTRICT,
    CONSTRAINT fk_audit_task_plan FOREIGN KEY (plan_id) REFERENCES audit_plan (id)
        ON DELETE RESTRICT ON UPDATE RESTRICT,
    KEY idx_audit_task_version_status (scenario_version_id, status),
    KEY idx_audit_task_status_phase (status, current_phase),
    KEY idx_audit_task_plan (plan_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;


-- 审核报告：一个任务可以有多次生成，每次是一条新的快照记录。
-- 正文生成后固定；任务输入变化后旧记录仍可读，只是不再是当前报告。
CREATE TABLE audit_report (
    id BIGINT NOT NULL AUTO_INCREMENT,
    audit_task_id BIGINT NOT NULL,
    report_name VARCHAR(255) NOT NULL,

    -- 正文与媒体类型；下载按 content_type 决定文件后缀。
    content_type VARCHAR(60) NOT NULL DEFAULT 'text/markdown',
    content_md MEDIUMTEXT NOT NULL,

    -- 生成那一刻的执行完成度，是快照而非实时状态。
    scope_execution_complete TINYINT(1) NOT NULL DEFAULT 0,
    risk_conclusion_available TINYINT(1) NOT NULL DEFAULT 0,

    -- 未完成项清单（哪个规则、什么状态、什么原因）与可导出格式。
    reasons_json JSON NOT NULL,
    -- 报告引用了哪些规则结果。
    snapshot_json JSON NOT NULL,

    -- 生成时的任务输入修订号；与任务当前值不同即过期。
    input_revision INT NOT NULL,
    remark TEXT NULL,
    -- 归档时间；为空表示未归档。
    archived_at DATETIME(6) NULL,
    generated_at DATETIME(6) NOT NULL,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
    PRIMARY KEY (id),
    CONSTRAINT fk_audit_report_task FOREIGN KEY (audit_task_id) REFERENCES audit_task (id)
        ON DELETE RESTRICT ON UPDATE RESTRICT,
    KEY idx_audit_report_task_generated (audit_task_id, generated_at),
    KEY idx_audit_report_generated (generated_at),
    KEY idx_audit_report_archived (archived_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;


-- 材料位：任务里每个被要求的材料一行。
CREATE TABLE task_document (
    id BIGINT NOT NULL AUTO_INCREMENT,
    audit_task_id BIGINT NOT NULL,
    -- 材料位身份，来自 manifest；不用中文名匹配材料。
    material_key VARCHAR(40) NOT NULL,
    material_name VARCHAR(255) NOT NULL,
    required TINYINT(1) NOT NULL,
    -- 当前生效的那次上传；未上传为空。
    current_upload_id BIGINT NULL,
    status VARCHAR(30) NOT NULL,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
    PRIMARY KEY (id),
    CONSTRAINT uq_task_document_material UNIQUE (audit_task_id, material_key),
    CONSTRAINT fk_task_document_task FOREIGN KEY (audit_task_id) REFERENCES audit_task (id)
        ON DELETE RESTRICT ON UPDATE RESTRICT,
    CONSTRAINT chk_task_document_required CHECK (required IN (0, 1))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;


-- 上传版本：追加写入，每个材料位只允许一条 is_current。
CREATE TABLE document_upload (
    id BIGINT NOT NULL AUTO_INCREMENT,
    task_document_id BIGINT NOT NULL,
    original_filename VARCHAR(255) NOT NULL,
    relative_path VARCHAR(500) NOT NULL,
    content_type VARCHAR(120) NOT NULL,
    declared_material_type VARCHAR(80) NOT NULL,
    size_bytes BIGINT NOT NULL,
    -- 文件内容哈希：文件质量规则用它判断同任务内是否重复。
    sha256 CHAR(64) NOT NULL,
    status VARCHAR(30) NOT NULL,
    is_current TINYINT(1) NOT NULL,
    -- 只有当前行有值，唯一键让每个材料位只能有一条当前记录。
    current_task_document_id BIGINT
        GENERATED ALWAYS AS (IF(is_current = 1, task_document_id, NULL)) STORED,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
    PRIMARY KEY (id),
    CONSTRAINT uq_document_upload_one_current UNIQUE (current_task_document_id),
    CONSTRAINT fk_document_upload_document FOREIGN KEY (task_document_id) REFERENCES task_document (id)
        ON DELETE RESTRICT ON UPDATE RESTRICT,
    CONSTRAINT chk_document_upload_size CHECK (size_bytes >= 0),
    CONSTRAINT chk_document_upload_current CHECK (is_current IN (0, 1)),
    KEY idx_document_upload_current (task_document_id, is_current),
    KEY idx_document_upload_sha256 (sha256)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;


-- 外部处理产物索引；正文在 relative_path 指向的文件里，不在数据库。
CREATE TABLE stored_artifact (
    id BIGINT NOT NULL AUTO_INCREMENT,
    document_upload_id BIGINT NOT NULL,
    artifact_type VARCHAR(40) NOT NULL,
    relative_path VARCHAR(500) NOT NULL,
    status VARCHAR(30) NOT NULL,
    -- 结果未知时保留最后一次已受理的作业号，续查同一个作业。
    external_job_id VARCHAR(120) NULL,
    deadline_at DATETIME(6) NULL,
    failure_code VARCHAR(80) NULL,
    failure_message TEXT NULL,
    -- 已提交或已处理的次数；超过上限时不再自动重试。
    attempt_count INT NOT NULL DEFAULT 0,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
    PRIMARY KEY (id),
    -- 同一上传同一类型只调一次外部服务。
    CONSTRAINT uq_stored_artifact_upload_type UNIQUE (document_upload_id, artifact_type),
    CONSTRAINT fk_stored_artifact_upload FOREIGN KEY (document_upload_id) REFERENCES document_upload (id)
        ON DELETE RESTRICT ON UPDATE RESTRICT,
    CONSTRAINT chk_stored_artifact_attempts CHECK (attempt_count >= 0),
    KEY idx_stored_artifact_upload_status (document_upload_id, status),
    -- 启动时按状态恢复中断的提交。
    KEY idx_stored_artifact_status (status, updated_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;


-- 节点执行批次；result_json 只存批次状态与进度汇总。
CREATE TABLE node_execution (
    id BIGINT NOT NULL AUTO_INCREMENT,
    audit_task_id BIGINT NOT NULL,
    node_code VARCHAR(80) NOT NULL,
    sequence_no INT NOT NULL,
    status VARCHAR(30) NOT NULL,
    started_at DATETIME(6) NULL,
    finished_at DATETIME(6) NULL,
    result_json JSON NULL,
    failure_code VARCHAR(80) NULL,
    failure_message TEXT NULL,
    -- 被重试或换版取代时，保留原状态与原因。
    invalidated_from VARCHAR(30) NULL,
    invalidated_at DATETIME(6) NULL,
    invalidation_reason VARCHAR(200) NULL,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
    PRIMARY KEY (id),
    CONSTRAINT uq_node_execution_task_sequence UNIQUE (audit_task_id, sequence_no),
    CONSTRAINT fk_node_execution_task FOREIGN KEY (audit_task_id) REFERENCES audit_task (id)
        ON DELETE RESTRICT ON UPDATE RESTRICT,
    CONSTRAINT chk_node_execution_sequence CHECK (sequence_no >= 1),
    KEY idx_node_execution_task_node (audit_task_id, node_code),
    KEY idx_node_execution_task_status (audit_task_id, status),
    KEY idx_node_execution_display_status (status, invalidated_from)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;


-- 字段候选版本索引；取值（原文、候选值、规范值）在任务 CSV。
CREATE TABLE task_field_value (
    id BIGINT NOT NULL AUTO_INCREMENT,
    audit_task_id BIGINT NOT NULL,
    field_key VARCHAR(40) NOT NULL,
    material_key VARCHAR(40) NOT NULL,
    -- 这个值是哪一次上传读出来的；人工纠错时为空。
    document_upload_id BIGINT NULL,
    revision_no INT NOT NULL DEFAULT 1,
    is_current TINYINT(1) NOT NULL DEFAULT 1,
    status VARCHAR(40) NOT NULL,
    -- 由哪一次抽取产生；人工纠错时为空。
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
    CONSTRAINT uq_task_field_value_current UNIQUE (audit_task_id, current_fact_scope),
    CONSTRAINT fk_task_field_value_task FOREIGN KEY (audit_task_id) REFERENCES audit_task (id)
        ON DELETE RESTRICT ON UPDATE RESTRICT,
    CONSTRAINT fk_task_field_value_upload FOREIGN KEY (document_upload_id) REFERENCES document_upload (id)
        ON DELETE RESTRICT ON UPDATE RESTRICT,
    CONSTRAINT fk_task_field_value_execution FOREIGN KEY (extraction_execution_id) REFERENCES node_execution (id)
        ON DELETE RESTRICT ON UPDATE RESTRICT,
    CONSTRAINT chk_task_field_value_revision CHECK (revision_no >= 1),
    CONSTRAINT chk_task_field_value_current CHECK (is_current IN (0, 1)),
    KEY idx_task_field_value_lookup (audit_task_id, field_key, material_key),
    KEY idx_task_field_value_current (audit_task_id, is_current)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;


-- 统一事实索引（字段抽取与计算结果）；取值在任务 CSV。
CREATE TABLE task_fact (
    id BIGINT NOT NULL AUTO_INCREMENT,
    audit_task_id BIGINT NOT NULL,
    -- EXTRACTED 用字段编号，DERIVED 用计算结果编号；计算参数按它引用。
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
    -- 五个身份维度长度前缀编码；NULL 用 -1: 表示，避免分隔符与内容混淆。
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
    CONSTRAINT fk_task_fact_task FOREIGN KEY (audit_task_id) REFERENCES audit_task (id)
        ON DELETE RESTRICT ON UPDATE RESTRICT,
    CONSTRAINT fk_task_fact_execution FOREIGN KEY (source_execution_id) REFERENCES node_execution (id)
        ON DELETE RESTRICT ON UPDATE RESTRICT,
    CONSTRAINT chk_task_fact_revision CHECK (revision_no >= 1),
    CONSTRAINT chk_task_fact_current CHECK (is_current IN (0, 1)),
    CONSTRAINT chk_task_fact_material CHECK (material_key IS NULL OR material_key <> ''),
    KEY idx_task_fact_lookup (audit_task_id, fact_code, period_scope, statement_scope, entity_scope),
    KEY idx_task_fact_kind (audit_task_id, fact_kind, is_current),
    KEY idx_task_fact_current (audit_task_id, is_current),
    KEY idx_task_fact_rule (audit_task_id, calculation_rule_code)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;


-- 规则结论索引；产出、依据、缺项在任务 CSV。
CREATE TABLE rule_result (
    id BIGINT NOT NULL AUTO_INCREMENT,
    audit_task_id BIGINT NOT NULL,
    node_execution_id BIGINT NOT NULL,
    -- 规则稳定身份，manifest 里按它对上规则定义。
    rule_key VARCHAR(80) NOT NULL,
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
    CONSTRAINT fk_rule_result_task FOREIGN KEY (audit_task_id) REFERENCES audit_task (id)
        ON DELETE RESTRICT ON UPDATE RESTRICT,
    CONSTRAINT fk_rule_result_execution FOREIGN KEY (node_execution_id) REFERENCES node_execution (id)
        ON DELETE RESTRICT ON UPDATE RESTRICT,
    CONSTRAINT chk_rule_result_turn_count CHECK (turn_count >= 0),
    KEY idx_rule_result_task_status (audit_task_id, status),
    KEY idx_rule_result_task_rule (audit_task_id, rule_key),
    KEY idx_rule_result_task_risk (audit_task_id, risk_level)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;


-- 人工复核与纠错记录：一条记录只针对一个对象。
CREATE TABLE manual_review (
    id BIGINT NOT NULL AUTO_INCREMENT,
    audit_task_id BIGINT NOT NULL,
    document_upload_id BIGINT NULL,
    rule_result_id BIGINT NULL,
    field_value_id BIGINT NULL,
    review_type VARCHAR(40) NOT NULL,
    status VARCHAR(30) NOT NULL,
    -- 提交时系统当时的结论快照；人工结论与它分开保存。
    system_judgment_json JSON NOT NULL,
    decision VARCHAR(40) NULL,
    business_note TEXT NULL,
    before_value TEXT NULL,
    after_value TEXT NULL,
    basis_type VARCHAR(30) NULL,
    decision_command_id BIGINT NULL,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
    PRIMARY KEY (id),
    CONSTRAINT fk_manual_review_task FOREIGN KEY (audit_task_id) REFERENCES audit_task (id)
        ON DELETE RESTRICT ON UPDATE RESTRICT,
    CONSTRAINT fk_manual_review_upload FOREIGN KEY (document_upload_id) REFERENCES document_upload (id)
        ON DELETE RESTRICT ON UPDATE RESTRICT,
    CONSTRAINT fk_manual_review_rule_result FOREIGN KEY (rule_result_id) REFERENCES rule_result (id)
        ON DELETE RESTRICT ON UPDATE RESTRICT,
    CONSTRAINT fk_manual_review_field_value FOREIGN KEY (field_value_id) REFERENCES task_field_value (id)
        ON DELETE RESTRICT ON UPDATE RESTRICT,
    KEY idx_manual_review_task_status (audit_task_id, status),
    KEY idx_manual_review_upload (document_upload_id),
    KEY idx_manual_review_rule_result (rule_result_id),
    KEY idx_manual_review_field_value (field_value_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;


-- 材料位与上传互相引用，最后补这条外键。
ALTER TABLE task_document
    ADD CONSTRAINT fk_task_document_current_upload
    FOREIGN KEY (current_upload_id) REFERENCES document_upload (id)
    ON DELETE RESTRICT ON UPDATE RESTRICT;


-- 状态取值（取值以代码常量为准）
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
-- manual_review.status         PENDING / RESOLVED / INVALIDATED
