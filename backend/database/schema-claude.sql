-- ============================================================================
-- baoli-v4 数据库结构（设计稿：schema-claude.sql）
--
-- 这份文件是**设计建议**，不是可直接替换 schema.sql 的运行脚本。
-- 它假设应用实体同步调整后再执行；当前仓库的 entity 仍按 schema.sql 编写。
--
--
-- 一、数据放在哪里（先定边界，再定表）
--
--   数据库只放四类东西：
--     1. 身份与关系：编号、外键、任务/版本/材料/字段之间的归属。
--     2. 状态与版本：谁当前有效、哪一版是当前、失效原因、执行批次。
--     3. 配置快照：发布版本、任务冻结配置（形状由业务 Excel 决定，只能整份存）。
--     4. 证据与裁量：证据引用、人工裁决、操作日志。
--
--   文件放四类东西，数据库只存路径与哈希：
--     1. 上传原件与 8010/8011 等二进制产物。
--     2. 外部服务产物正文（OCR Markdown、印章 JSON、8018 校验结果）。
--     3. 任务业务取值 CSV（字段值、事实、规则结论、依据、产出、缺项）。
--     4. 模型原始响应。
--
--   一条事实只存一处：业务取值在 CSV，产物正文在文件；数据库里的 JSON 不得再存副本。
--
--
-- 二、JSON 的使用边界
--
--   允许：
--     - 整份配置快照：scenario_draft.config_json、scenario_version.manifest_json、
--       audit_plan.config_json、audit_task.manifest_json。
--     - 幂等请求/结果：background_operation.request_json / result_json。
--     - 人工裁决当时看到的系统判断：manual_review.system_judgment_json。
--     - 小体积批次汇总：node_execution.result_json、rule_result.result_json。
--
--   禁止：
--     - 出现在 WHERE / ORDER BY / UNIQUE 里的值（必须是真实列）。
--     - 能由其他列算出的值（例如节点序号、任务范围）。
--     - 文件里已经有的内容副本（例如 OCR 正文、完整模型响应）。
--
--
-- 三、命名与类型约定
--
--   表名：单数 snake_case，按业务域前缀（scenario_ / audit_ / task_ / document_ / stored_）。
--   主键：id BIGINT AUTO_INCREMENT，全表统一。
--   业务编号：xxx_no VARCHAR(40) + UNIQUE，对外展示与接口使用。
--   外键：xxx_id；跨任务子表统一使用 (id, audit_task_id) 复合外键，防止跨任务引用。
--   时间：DATETIME(6)，相对时间点；created_at / updated_at 每表都有。
--   状态：VARCHAR(30/40)，不用 ENUM（新增状态不应要求 DDL）；取值见文末附录。
--   布尔：TINYINT(1) + CHECK (col IN (0,1))。
--   约束命名：uq_表_含义 / fk_表_目标 / chk_表_含义 / idx_表_用途。
--   金额与十进制不有数据库计算，精度由任务 CSV 的 decimal 列保证。
--
--
-- 四、表的两类生命周期
--
--   追加型（历史保留，新写入新增一行，靠 is_current/批次序号表达当前）：
--     document_upload、stored_artifact、node_execution、task_field_value、
--     task_fact、audit_report、evidence、manual_review、operation_log、
--     background_operation。
--
--   覆盖型（同一身份只维护一行）：
--     scenario_template、scenario_draft、scenario_version、audit_plan、audit_task。
--
--   删除策略：只有 audit_plan 软删除（deleted_at 可恢复）；审核任务按业务要求永久删除，
--   不留 deleted_at。其余表随父记录永久删除，不做软删除。
--
--
-- 五、与现有 schema.sql 的差异（对应应用需同步修改）
--
--   A. 去掉 audit_plan.scope_json：它是 manifest_json 的纯派生结果（全员生效规则 +
--      字段/材料计数），创建任务时现算即可；保留 scope_hash 供并发校验。
--   B. 去掉 audit_task.deleted_at：任务已是永久删除，这一列不再读写。
--   C. audit_task.checkpoint_json 拆成 current_phase 列：node_index 可由 current_node
--      在冻结节点顺序中的位置算出，不再单独存。
--   D. task_field_value.current_fact_scope 改成长度前缀，与 task_fact 同一口径。
--   E. task_fact 补 (audit_task_id, material_key) → task_document 外键（NULL 允许）。
--   F. node_execution 增加 invalidated_from / invalidated_at / invalidation_reason：
--      执行失效后仍能按「原状态」过滤，不必查询 result_json。
--   G. operation_log 增加 permanent 列：替代 detail_json["permanent"] 的路径过滤。
--   H. stored_artifact 把 external_job_id / deadline_at / failure_code / failure_message
--      提成真实列；metadata_json 只保留逐次尝试摘要，不再保存产物全文。
--   I. rule_result 增加 turn_count / raw_response_path：模型原始响应写文件，
--      result_json 不再保存完整 model_response（业务值不落库两次）。
--   J. rule_result 增加 loop_status 真实列。
--   K. task_fact 的 revision 唯一键改用 material_scope 生成列：
--      MySQL 唯一键不比较 NULL，原写法允许「无材料归属的计算事实」在同一版本重复插入。
--
--
-- 六、明确不动的部分（属于设计取舍，不是遗漏）
--
--   - scenario_draft.config_json / scenario_version.manifest_json / audit_plan.config_json /
--     audit_task.manifest_json：配置形状由业务 Excel 决定，拆成明细表只能走 EAV。
--   - scenario_draft_* / plan_* 没有对应表：它们只是 JSON 的列形状描述（ConfigurationShape）。
--   - 人工复核、证据、执行记录继续保留完整审计链，不做软删除。
--
--
-- 前置条件：MySQL >= 8.0.16（CHECK 约束、生成列、JSON 列）。
-- 用法：空库执行一次；CREATE TABLE 遇到同名表直接报错。
-- ============================================================================


-- ---------------------------------------------------------------------------
-- 1. 场景身份与后台操作
-- ---------------------------------------------------------------------------

-- 场景身份与版本计数器。scenario_code 是稳定标识，不是中文名。
-- latest_version_no 由本表原子分配，避免无保护的 max()+1 并发冲突。
CREATE TABLE scenario_template (
    id BIGINT NOT NULL AUTO_INCREMENT,
    scenario_code VARCHAR(80) NOT NULL,
    scenario_name VARCHAR(255) NOT NULL,
    -- 场景协议：schema_json 是整份协议配置，随场景冻结。
    schema_key VARCHAR(80) NOT NULL,
    schema_version VARCHAR(40) NOT NULL,
    schema_json JSON NOT NULL,
    -- 展示与筛选属性，不参与执行语义。
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

-- 后台长操作：先认领，再由独立 Session 执行；接口只返回受理状态。
-- object_no 是业务编号（任务号/作业号），跨对象类型，因此不设外键。
CREATE TABLE background_operation (
    id BIGINT NOT NULL AUTO_INCREMENT,
    operation_no VARCHAR(40) NOT NULL,
    object_type VARCHAR(30) NOT NULL,
    object_no VARCHAR(40) NOT NULL,
    operation_type VARCHAR(40) NOT NULL,
    -- 幂等键：同一对象同一操作类型下重复提交必须复用第一次的结果。
    idempotency_key VARCHAR(120) NOT NULL,
    target_node VARCHAR(40) NULL,
    input_version VARCHAR(120) NULL,
    request_hash CHAR(64) NULL,
    -- 命令停在哪一步；恢复时据此决定唯一推进动作。
    phase VARCHAR(40) NULL,
    -- 外部作业引用（OCR jobId 等）：本地超时后只能续查同一作业。
    external_job_ref VARCHAR(120) NULL,
    status VARCHAR(30) NOT NULL,
    -- 请求与结果是命令自身的输入输出，不是业务取值；保留以便幂等回放。
    request_json JSON NOT NULL,
    result_json JSON NULL,
    -- 规则库模型原始响应文件，相对 data 根目录。
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


-- ---------------------------------------------------------------------------
-- 2. 规则库导入
-- ---------------------------------------------------------------------------

CREATE TABLE import_job (
    id BIGINT NOT NULL AUTO_INCREMENT,
    job_no VARCHAR(40) NOT NULL,
    source_filename VARCHAR(255) NOT NULL,
    source_path VARCHAR(500) NOT NULL,
    source_sha256 CHAR(64) NOT NULL,
    source_size BIGINT NOT NULL,
    profile_version VARCHAR(40) NOT NULL,
    -- 导入时冻结场景协议；后续绑定与转换只读这份快照。
    schema_key VARCHAR(80) NOT NULL,
    schema_version VARCHAR(40) NOT NULL,
    schema_json JSON NOT NULL,
    -- 上传入口的场景身份；不由 Excel 表头推断。
    scenario_code VARCHAR(80) NULL,
    profile_display_version VARCHAR(40) NULL,
    status VARCHAR(40) NOT NULL,
    -- 两轮模型解析后的业务配置候选。这是导入阶段的唯一业务产物。
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
    -- 大 JSON 行按 (created_at, id) 分页时若 filesort 会触发 1038；这个索引是必需的。
    KEY idx_import_job_created_at_id (created_at, id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;


-- ---------------------------------------------------------------------------
-- 3. 已发布场景版本
-- ---------------------------------------------------------------------------

CREATE TABLE scenario_version (
    id BIGINT NOT NULL AUTO_INCREMENT,
    -- 页面表单/复制来源没有导入作业，保持 NULL，不伪造来源。
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
    -- 编译后的不可变快照：材料/字段/规则/计算/报告/节点。
    -- 这是「发布时刻的完整配置」，不是可重建缓存，必须保留。
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

-- 执行容器节点。当前只有 RULE_EXECUTION 一种技术容器，用于关联执行记录；
-- audit_task.current_node 通过它做复合外键，保证任务节点一定存在于冻结版本中。
CREATE TABLE scenario_node (
    id BIGINT NOT NULL AUTO_INCREMENT,
    scenario_version_id BIGINT NOT NULL,
    node_code VARCHAR(80) NOT NULL,
    node_type VARCHAR(40) NOT NULL,
    name VARCHAR(120) NOT NULL,
    sequence_no INT NOT NULL,
    -- 技术容器依赖为空；业务先后由规则显式引用表达。
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


-- ---------------------------------------------------------------------------
-- 4. 场景草稿、方案、操作日志
-- ---------------------------------------------------------------------------

-- 场景草稿：每个场景一条，可编辑、不生效。发布时编译成 scenario_version。
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
    -- 最近一次保存时的编译失败原因；NULL 表示当前可通过发布检查。
    sync_error JSON NULL,
    -- 节点、材料、字段、规则、计算与报告配置。形状由业务 Excel 决定，整份保存。
    config_json JSON NOT NULL,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
    PRIMARY KEY (id),
    CONSTRAINT uq_scenario_draft_code UNIQUE (scenario_code),
    CONSTRAINT chk_scenario_draft_revision CHECK (revision_no >= 0),
    CONSTRAINT chk_scenario_draft_base CHECK (base_version_no IS NULL OR base_version_no >= 1)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- 审核方案：对已发布场景版本的一份可编辑副本。
-- 设计变化：不再保存 scope_json。范围 = build_task_schema(manifest) 的纯函数结果，
-- 创建任务时现算；scope_hash 保留，用于「页面预览的范围」与「提交时范围」一致性校验。
CREATE TABLE audit_plan (
    id BIGINT NOT NULL AUTO_INCREMENT,
    plan_code VARCHAR(80) NOT NULL,
    plan_name VARCHAR(255) NOT NULL,
    scenario_code VARCHAR(80) NOT NULL,
    -- 溯源自哪一版场景；执行与范围都不再读它。
    scenario_version_id BIGINT NULL,
    description TEXT NULL,
    enabled TINYINT(1) NOT NULL DEFAULT 1,
    deleted_at DATETIME(6) NULL,
    -- 方案自己的可编辑配置（含 context、材料、字段、规则、计算、报告）。
    config_json JSON NOT NULL,
    -- 编译产物：报告渲染与任务冻结读取。它是 config_json 的确定性派生结果，
    -- 保留为缓存以免每次创建任务重新编译；丢失时可按 config_json 重建。
    manifest_json JSON NULL,
    -- 最近一次保存的范围编译失败原因；NULL 表示范围已生效。
    sync_error JSON NULL,
    -- 编译产物的业务范围指纹（忽略 compiled_at 等非业务字段）。
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

-- 已提交的同步业务写操作。后台长操作在 background_operation，两者按 command_no 关联。
-- 设计变化：permanent 提成真实列，删除命令的幂等检查不再查询 detail_json 路径。
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
    -- 是否为永久删除。旧实现从 detail_json["permanent"] 过滤，无法用索引。
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


-- ---------------------------------------------------------------------------
-- 5. 审核任务
-- ---------------------------------------------------------------------------

CREATE TABLE audit_task (
    id BIGINT NOT NULL AUTO_INCREMENT,
    task_no VARCHAR(40) NOT NULL,
    scenario_version_id BIGINT NOT NULL,
    plan_id BIGINT NULL,
    task_name VARCHAR(255) NOT NULL,
    remark TEXT NULL,
    status VARCHAR(40) NOT NULL,
    current_node VARCHAR(80) NOT NULL,
    -- 设计变化：原位 checkpoint_json 拆分。
    -- 当前阶段（AWAITING_CONFIRMATION / RUNNING / COMPLETED 等）。
    -- 节点序号可由 current_node 在冻结节点顺序中的位置算出，不再单独保存。
    current_phase VARCHAR(40) NOT NULL DEFAULT 'AWAITING_CONFIRMATION',
    -- 任务冻结的执行快照：方案的编译产物整份复制。执行期只读它。
    -- 场景或方案后来怎么改都不影响已建任务；这是必须保留的 JSON。
    manifest_json JSON NOT NULL,
    -- 本任务生效范围：enabled_rule_keys / unenabled_rule_keys / fields。
    -- 只应包含范围与字段快照；node_plan、场景名称等可从 manifest_json 读取，不要重复。
    task_schema_json JSON NOT NULL,
    -- 业务 CSV 清单：format_version、batches（当前登记 current/files/record_count）、
    -- legacy_files。字段值、计算结果与规则结论的取值都在 CSV，不在数据库。
    value_file_json JSON NULL,
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

-- 报告快照：由报告执行项生成，正文不可改写；重新生成是新快照。
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
    -- 未完成项、格式支持等结构化说明；体量小，适合随报告保存。
    reasons_json JSON NULL,
    -- 生成时引用的规则结果 id、输入修订号等，用于判断报告是否仍对应当前批次。
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


-- ---------------------------------------------------------------------------
-- 6. 材料位、上传与处理产物
-- ---------------------------------------------------------------------------

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

CREATE TABLE document_upload (
    id BIGINT NOT NULL AUTO_INCREMENT,
    upload_no VARCHAR(40) NOT NULL,
    task_document_id BIGINT NOT NULL,
    audit_task_id BIGINT NOT NULL,
    original_filename VARCHAR(255) NOT NULL,
    relative_path VARCHAR(500) NOT NULL,
    content_type VARCHAR(120) NOT NULL,
    -- 用户本次上传时声明的材料类型（提交给 8018 的 doc_type）；替换文件可重选。
    declared_material_type VARCHAR(80) NOT NULL,
    size_bytes BIGINT NOT NULL,
    sha256 CHAR(64) NOT NULL,
    status VARCHAR(30) NOT NULL,
    is_current TINYINT(1) NOT NULL,
    -- 生成列把「每个材料位只有一个当前文件」变成数据库可约束的唯一键。
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

-- 外部处理产物索引。
-- 设计变化：原先 metadata_json 保存整份产物内容（OCR 正文可达数十万字节），
-- 与磁盘上的 ocr.json 重复。这里只保存状态与逐次调用摘要；正文按 relative_path 读取。
-- 查询/重试需要的作业号、截止时间、失败原因提成真实列。
CREATE TABLE stored_artifact (
    id BIGINT NOT NULL AUTO_INCREMENT,
    artifact_no VARCHAR(40) NOT NULL,
    document_upload_id BIGINT NOT NULL,
    audit_task_id BIGINT NOT NULL,
    artifact_type VARCHAR(40) NOT NULL,
    relative_path VARCHAR(500) NOT NULL,
    -- 上传身份：同一上传重跑同一产物时不变。
    upload_sha256 CHAR(64) NOT NULL,
    -- 产物内容版本：同一 PDF 重新识别后上传哈希不变、内容可能变化。
    content_sha256 CHAR(64) NULL,
    status VARCHAR(30) NOT NULL,
    -- 外部作业号（OCR jobId 等）。结果未知时保留最后一次已受理的作业号。
    external_job_id VARCHAR(120) NULL,
    deadline_at DATETIME(6) NULL,
    failure_code VARCHAR(80) NULL,
    failure_message TEXT NULL,
    -- 只保存 processing_attempts / submit_attempts 等小体积调用历史，
    -- 以及状态机需要的 submission_state；不保存产物正文。
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
    -- 启动恢复：按状态扫描 SUBMITTING / PROCESSING 的产物。
    KEY idx_stored_artifact_status (status, updated_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;


-- ---------------------------------------------------------------------------
-- 7. 执行批次、字段事实、规则结果、证据
-- ---------------------------------------------------------------------------

CREATE TABLE node_execution (
    id BIGINT NOT NULL AUTO_INCREMENT,
    audit_task_id BIGINT NOT NULL,
    scenario_version_id BIGINT NOT NULL,
    node_code VARCHAR(80) NOT NULL,
    sequence_no INT NOT NULL,
    status VARCHAR(30) NOT NULL,
    started_at DATETIME(6) NULL,
    finished_at DATETIME(6) NULL,
    -- 本批次汇总：rule_outcomes、preprocessing 进度等。
    -- 这些是批次状态与进度，不是业务取值；业务取值在任务 CSV。
    result_json JSON NULL,
    failure_code VARCHAR(80) NULL,
    failure_message TEXT NULL,
    -- 设计变化：失效信息提成真实列。
    -- 被重试/纠错/换版取代时，status 置 INVALIDATED，原状态保留在这里。
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
    -- 执行列表按「展示状态」筛选：INVALIDATED 行用 invalidated_from 代替真实状态。
    KEY idx_node_execution_display_status (status, invalidated_from)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- 字段候选版本索引。取值（原文、候选值、规范值）在任务 CSV。
-- 设计变化：current_fact_scope 改成长度前缀，与 task_fact 同一编码口径，
-- 避免 field_key/material_key 含分隔符时的身份碰撞。
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
    evidence_id BIGINT NULL,
    -- 人工纠错产生的版本没有抽取执行，记为 NULL。
    extraction_execution_id BIGINT NULL,
    -- DOCUMENT_EXTRACTION / HUMAN_CORRECTION
    origin VARCHAR(30) NOT NULL DEFAULT 'DOCUMENT_EXTRACTION',
    -- 只有 is_current=1 的行才有值；唯一键对历史版本不生效。
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
    -- scenario_version_id 已由任务固定，不再进入版本唯一键；
    -- 复合外键需要它时由下面的索引覆盖。
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
    -- 复合外键 (audit_task_id, material_key) 需要以这两列为前缀的索引。
    KEY idx_task_field_material (audit_task_id, material_key),
    KEY idx_task_field_evidence (evidence_id),
    KEY idx_task_field_current (audit_task_id, is_current)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- 统一事实索引：字段抽取结果与计算结果的身份、口径与当前位。
-- 取值在任务 CSV；这里只回答「哪个身份哪一版是当前」。
CREATE TABLE task_fact (
    id BIGINT NOT NULL AUTO_INCREMENT,
    audit_task_id BIGINT NOT NULL,
    scenario_version_id BIGINT NOT NULL,
    -- EXTRACTED 用字段编号，DERIVED 用计算结果编号。
    fact_code VARCHAR(80) NOT NULL,
    fact_kind VARCHAR(20) NOT NULL,
    -- DERIVED 事实可能跨材料，允许 NULL；设计变化：补到 task_document 的外键。
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
    -- 唯一键里 NULL 不参与比较（MySQL 允许多个 NULL），直接把 material_key 放进
    -- revision 唯一键会让「无材料归属的计算事实」在同一版本上重复插入。
    -- 这里统一替换成空串；内部材料键不允许为空串，因此不会与真实值混淆。
    material_scope VARCHAR(40) GENERATED ALWAYS AS (IFNULL(material_key, '')) VIRTUAL,
    -- 五个身份维度用长度前缀编码；NULL 用 -1:，不与真实材料键混淆。
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
    -- 复合外键 (audit_task_id, material_key) 需要以这两列为前缀的索引。
    KEY idx_task_fact_material (audit_task_id, material_key)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- 规则结论索引。业务结论（产出、依据、缺项、风险上下文）在任务 CSV；
-- 这里只留可筛选的状态与必要诊断。
-- 模型原始响应写文件，由 raw_response_path 指回；result_json 只保留小体积诊断。
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
    -- 单条规则的模型循环结局：DONE / BLOCKED / RUNNING / NEEDS_HUMAN。
    loop_status VARCHAR(30) NULL,
    -- 本条规则实际发生的模型调用轮次。
    turn_count INT NOT NULL DEFAULT 0,
    -- 模型消息与原始响应的记录文件，相对 data 根目录。
    raw_response_path VARCHAR(500) NULL,
    -- 小体积执行诊断：standard、inputs 引用、unresolved_items、execution_issues。
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

CREATE TABLE evidence (
    id BIGINT NOT NULL AUTO_INCREMENT,
    evidence_no VARCHAR(40) NOT NULL,
    audit_task_id BIGINT NOT NULL,
    document_upload_id BIGINT NULL,
    rule_result_id BIGINT NULL,
    field_value_id BIGINT NULL,
    -- 结构化字段的顶层属性路径；普通字段与规则证据为空。
    field_path VARCHAR(255) NULL,
    quote_text TEXT NOT NULL,
    artifact_id BIGINT NULL,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
    PRIMARY KEY (id),
    CONSTRAINT uq_evidence_no UNIQUE (evidence_no),
    CONSTRAINT uq_evidence_id_task UNIQUE (id, audit_task_id),
    -- 证据至少要能指回一处真实来源。
    CONSTRAINT chk_evidence_reference CHECK (
        document_upload_id IS NOT NULL OR rule_result_id IS NOT NULL OR field_value_id IS NOT NULL
    ),
    CONSTRAINT chk_evidence_field_path CHECK (field_path IS NULL OR field_value_id IS NOT NULL),
    CONSTRAINT fk_evidence_task
        FOREIGN KEY (audit_task_id) REFERENCES audit_task (id)
        ON DELETE RESTRICT ON UPDATE RESTRICT,
    CONSTRAINT fk_evidence_upload_task
        FOREIGN KEY (document_upload_id, audit_task_id)
        REFERENCES document_upload (id, audit_task_id)
        ON DELETE RESTRICT ON UPDATE RESTRICT,
    CONSTRAINT fk_evidence_rule_result_task
        FOREIGN KEY (rule_result_id, audit_task_id)
        REFERENCES rule_result (id, audit_task_id)
        ON DELETE RESTRICT ON UPDATE RESTRICT,
    CONSTRAINT fk_evidence_field_value_task
        FOREIGN KEY (field_value_id, audit_task_id)
        REFERENCES task_field_value (id, audit_task_id)
        ON DELETE RESTRICT ON UPDATE RESTRICT,
    CONSTRAINT fk_evidence_artifact_task
        FOREIGN KEY (artifact_id, audit_task_id)
        REFERENCES stored_artifact (id, audit_task_id)
        ON DELETE RESTRICT ON UPDATE RESTRICT,
    KEY idx_evidence_task (audit_task_id),
    KEY idx_evidence_upload (document_upload_id),
    KEY idx_evidence_rule_result (rule_result_id),
    KEY idx_evidence_field_value (field_value_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- 人工复核/纠错记录。system_judgment_json 是裁决当时的系统判断快照，
-- 属于审计证据，必须原样保留。
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


-- ---------------------------------------------------------------------------
-- 8. 延迟添加的复合外键（两张表互相引用）
-- ---------------------------------------------------------------------------

ALTER TABLE task_document
    ADD CONSTRAINT fk_task_document_current_upload
    FOREIGN KEY (current_upload_id, id) REFERENCES document_upload (id, task_document_id)
    ON DELETE RESTRICT ON UPDATE RESTRICT;

ALTER TABLE task_field_value
    ADD CONSTRAINT fk_task_field_value_evidence_task
    FOREIGN KEY (evidence_id, audit_task_id) REFERENCES evidence (id, audit_task_id)
    ON DELETE RESTRICT ON UPDATE RESTRICT;


-- ---------------------------------------------------------------------------
-- 9. 结构确认
-- 预期不返回任何行；返回的每一行表示该表没有被创建。
-- 列、索引与外键的实际结构由应用启动时只读核对。
-- ---------------------------------------------------------------------------

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
    UNION ALL SELECT 'evidence'
    UNION ALL SELECT 'manual_review'
) AS expected
LEFT JOIN information_schema.tables AS actual
       ON actual.table_schema = DATABASE()
      AND actual.table_name = expected.table_name
WHERE actual.table_name IS NULL
ORDER BY expected.table_name;


-- ---------------------------------------------------------------------------
-- 10. 状态取值附录
--
-- 状态列一律 VARCHAR，不使用 ENUM：新增状态不需要 DDL，迁移到只读库也安全。
-- 取值以代码常量为唯一来源（domain/audit_vocab.py、各服务内常量），这里只做索引。
--
--   scenario_version.status        PUBLISHED
--   scenario_draft.source          FORM / EXCEL / COPY / REVISION
--   scenario_version.source        IMPORT / FORM / COPY / REVISION
--   import_job.status              PARSED / PROPOSED / APPLIED / EXTERNAL_FAILED
--   audit_task.status              READY / RUNNING / BLOCKED / FAILED / COMPLETED
--                                  （WAITING_REVIEW 为历史等待态；当前人工审核未启用）
--   audit_task.current_phase       AWAITING_CONFIRMATION / RUNNING / COMPLETED
--   background_operation.status    PENDING / RUNNING / SUCCEEDED / FAILED / INTERRUPTED
--   operation_log.result           SUCCEEDED / FAILED / ACCEPTED
--   task_document.status           WAITING_UPLOAD / UPLOADED / PREPARED
--   document_upload.status         UPLOADED / SUPERSEDED / REMOVED
--   stored_artifact.status         PROCESSING / SUBMITTING / PENDING / READY /
--                                  FAILED / INVALIDATED
--   node_execution.status          RUNNING / SUCCEEDED / BLOCKED / FAILED /
--                                  SKIPPED / INVALIDATED
--   node_execution.invalidated_from  失效前的真实状态（RUNNING 除外）
--   task_field_value.origin        DOCUMENT_EXTRACTION / HUMAN_CORRECTION
--   task_field_value.status        PRESENT / NOT_PRESENT / EVIDENCE_INSUFFICIENT /
--                                  REVIEWED
--   task_fact.fact_kind            EXTRACTED / DERIVED
--   task_fact.status               PRESENT / NOT_PRESENT / COMPUTED / REVIEWED
--   rule_result.status             COMPUTED / PASSED / ABNORMAL / EVIDENCE_INSUFFICIENT /
--                                  NOT_APPLICABLE / NOT_EXECUTED / PENDING_REVIEW /
--                                  REVIEWED / SKIPPED / RUNNING
--   rule_result.loop_status        RUNNING / DONE / BLOCKED / NEEDS_HUMAN
--   audit_report.status            GENERATED / ARCHIVED
--   manual_review.status           PENDING / RESOLVED / INVALIDATED
--   manual_review.basis_type       HUMAN_STATEMENT / DOCUMENT_EVIDENCE / UPLOAD_RECORD
--
-- 状态与业务结论是两件事：任务 COMPLETED 只表示执行结束，不代表规则结论通过；
-- 批次 BLOCKED 表示本条/本批未完成，不代表业务判定为异常。两者不得互相替代。
-- ---------------------------------------------------------------------------

