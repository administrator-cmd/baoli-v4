-- ============================================================================
-- baoli-v4 规则库驱动审核系统：开发验证用完整数据库结构
--
-- 适用数据库：baoli_v4；开发验证可使用 baoli_v4_dev
-- 前置条件：MySQL >= 8.0（用得上生成列与 JSON 列即可）
--   - 业务表使用 utf8mb4_0900_ai_ci 排序规则，需要 MySQL 8.0+
--
-- 用法：
--   本文件是**唯一的结构脚本**：在全新的 baoli_v4 库上执行一次，得到完整结构。
--   1. 库不存在时先建库：
--      CREATE DATABASE baoli_v4 CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;
--   2. 以该库为默认库执行本文件。
--   本仓库只维护本文件的最终结构。结构变更后清空开发库，再执行本文件；
--   不备份开发数据，不提供 Alembic、增量升级、历史结构兼容或迁移记录表。
--   CREATE TABLE 遇到同名表直接报错，不能用重复执行代替重建。
--   文件末尾查询缺表情况；列、索引、外键和触发器由应用启动时只读核对。
--
-- 重要约束：
--   - 应用启动与请求过程只检查连接和表结构，绝不执行 DDL、不调用 create_all()、
--     不调用 saver.setup()、不自动修表。
--   - 表结构以数据库 Base 中的实体为准；编辑器 ConfigurationShape 只描述 JSON 属性，
--     不创建 scenario_draft_* / plan_* 配置明细表。
--
-- 业务主流程（表与状态取值都按这条链组织，任何一步都不得跳过）：
--   场景草稿（scenario_draft.config_json，可编辑、不生效）
--     -> 显式发布（scenario_version 及其关系投影，不可覆盖）
--     -> 方案钉住某个已发布版本号（audit_plan.scenario_version_id）
--     -> 任务冻结方案执行配置与范围（audit_task.manifest_json / task_schema_json）
--   规则库导入只写草稿，不在这里发布；导入预览不保留第二套编译/发布状态机。
--
-- 目录：
--   1. 审核场景身份与后台操作
--   2. 导入作业与候选
--   3. 已发布场景 JSON 版本及关系索引
--   3b. 场景配置草稿、审核方案、报告快照与操作日志
--   4. 审核任务与流程进度
--   5. 材料位、文件上传与处理产物
--   6. 节点执行、字段事实索引、规则结果、证据与人工复核
--   7. 延迟添加的复合外键
--   8. 执行结果确认
-- ============================================================================


-- ---------------------------------------------------------------------------
-- 1. 审核场景身份与后台操作
-- ---------------------------------------------------------------------------

-- 审核场景身份与版本计数器。
-- scenario_code 是场景身份（稳定标识），不是中文名称；版本号由本表原子分配，
-- 不依赖无保护的 max(version_no)+1。
-- 基本资料（名称/业务类型/说明/启停）直接维护在本表；删除实际移除场景和草稿。
-- 已发布版本保存的是
-- 发布时刻的名称与配置快照，不随本表修改而变。
CREATE TABLE scenario_template (
    id BIGINT NOT NULL AUTO_INCREMENT,
    scenario_code VARCHAR(80) NOT NULL,
    scenario_name VARCHAR(255) NOT NULL,
    schema_key VARCHAR(80) NOT NULL,
    schema_version VARCHAR(40) NOT NULL,
    schema_json JSON NOT NULL,
    -- 业务类型是页面维护的展示与筛选属性，不参与执行语义。
    category VARCHAR(80) NULL,
    description TEXT NULL,
    -- 启停：停用后不用于新建方案与任务；已创建任务按原快照继续。
    enabled TINYINT(1) NOT NULL DEFAULT 1,
    latest_version_no INT NOT NULL DEFAULT 0,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
    PRIMARY KEY (id),
    CONSTRAINT uq_scenario_template_code UNIQUE (scenario_code),
    CONSTRAINT chk_scenario_template_version CHECK (latest_version_no >= 0)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- 后台长操作：先认领记录，再由后台用独立 Session 执行；请求只返回受理状态。
-- object_no 是业务编号（作业号或任务号），跨两种对象，因此不设外键。
CREATE TABLE background_operation (
    id BIGINT NOT NULL AUTO_INCREMENT,
    operation_no VARCHAR(40) NOT NULL,
    object_type VARCHAR(30) NOT NULL,
    object_no VARCHAR(40) NOT NULL,
    operation_type VARCHAR(40) NOT NULL,
    idempotency_key VARCHAR(120) NOT NULL,
    target_node VARCHAR(40) NULL,
    input_version VARCHAR(120) NULL,
    -- 规范化请求摘要：同一幂等标识必须比较它，才能判断是重发还是新请求。
    request_hash CHAR(64) NULL,
    -- 命令当前停在哪一步（COMMAND_ACCEPTED / EXTERNAL_SUBMITTED / RESULT_UNKNOWN /
    -- BUSINESS_COMMITTED / GRAPH_ADVANCED / INTERRUPT_WAITING）：恢复时按它决定唯一推进动作。
    phase VARCHAR(40) NULL,
    -- 外部作业引用（例如 OCR jobId）：本地超时后只能续查同一作业，不能重新提交。
    external_job_ref VARCHAR(120) NULL,
    status VARCHAR(30) NOT NULL,
    request_json JSON NOT NULL,
    result_json JSON NULL,
    -- 规则库模型原始返回文件，相对于 data 根目录；解析或结构校验失败也保留。
    raw_response_path VARCHAR(500) NULL,
    failure_code VARCHAR(80) NULL,
    failure_message TEXT NULL,
    started_at DATETIME(6) NULL,
    finished_at DATETIME(6) NULL,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
    PRIMARY KEY (id),
    CONSTRAINT uq_background_operation_no UNIQUE (operation_no),
    -- 幂等标识与请求内容绑定：同一对象同一操作类型下，标识不同才是不同的操作。
    CONSTRAINT uq_background_operation_idempotency
        UNIQUE (object_type, object_no, operation_type, idempotency_key),
    KEY idx_background_operation_object (object_type, object_no, status),
    KEY idx_background_operation_status (status, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;


-- ---------------------------------------------------------------------------
-- 2. 导入作业与候选
-- ---------------------------------------------------------------------------

CREATE TABLE import_job (
    id BIGINT NOT NULL AUTO_INCREMENT,
    job_no VARCHAR(40) NOT NULL,
    source_filename VARCHAR(255) NOT NULL,
    source_path VARCHAR(500) NOT NULL,
    source_sha256 CHAR(64) NOT NULL,
    source_size BIGINT NOT NULL,
    profile_version VARCHAR(40) NOT NULL,
    -- 导入时冻结场景协议，后续绑定与转换读取此快照。
    schema_key VARCHAR(80) NOT NULL,
    schema_version VARCHAR(40) NOT NULL,
    schema_json JSON NOT NULL,
    -- 上传入口的**场景身份**：用户先建场景、再在该场景下上传 Excel。
    -- 场景不由 Excel 表头推断——猜错场景会拿另一套列协议解释它，报出一堆与真实问题
    -- 无关的缺列错误，而用户手里的文件根本没变。历史作业没有这一列，
    -- 读取方按「未记录场景」如实处理，不自动补一个场景。
    scenario_code VARCHAR(80) NULL,
    -- 统一模板协议的版本标识。系统只有一份协议，这里只记录「按哪一版协议解释」。
    profile_display_version VARCHAR(40) NULL,
    status VARCHAR(40) NOT NULL,
    -- 实际业务配置只有这一份：工作表布局、材料、字段、规则、计算与报告都在它里面。
    proposals_json JSON NULL,
    -- 导入修订号：工作表选择和解析结果改变时递增，拒绝过期结果写回。
    draft_revision INT NOT NULL DEFAULT 0,
    failure_code VARCHAR(80) NULL,
    failure_message TEXT NULL,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
    PRIMARY KEY (id),
    CONSTRAINT uq_import_job_job_no UNIQUE (job_no),
    CONSTRAINT chk_import_job_source_size CHECK (source_size >= 0),
    KEY idx_import_job_status_created_at (status, created_at),
    -- 与上面互补：本表带 proposals_json 等大 JSON 列，列表按 (created_at, id) 倒序分页时
    -- 若命不中索引就会 filesort，把整行塞进排序缓冲（默认仅 256KB）并报 1038 Out of sort memory。
    KEY idx_import_job_created_at_id (created_at, id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- ---------------------------------------------------------------------------
-- 3. 已发布场景 JSON 版本及关系索引
-- ---------------------------------------------------------------------------

CREATE TABLE scenario_version (
    id BIGINT NOT NULL AUTO_INCREMENT,
    -- 页面表单 / 复制来源的版本没有导入作业（import_job_id 为 NULL），
    -- 不伪造 Excel 作业与工作表行号；旧数据保持原 import_job_id 不变。
    import_job_id BIGINT NULL,
    scenario_code VARCHAR(80) NOT NULL,
    scenario_name VARCHAR(255) NOT NULL,
    schema_key VARCHAR(80) NOT NULL,
    schema_version VARCHAR(40) NOT NULL,
    schema_json JSON NOT NULL,
    version_no INT NOT NULL,
    status VARCHAR(30) NOT NULL,
    -- 来源：IMPORT=规则库导入；FORM=页面表单发布；COPY=复制场景；REVISION=从某一版本创建的修订。
    source VARCHAR(20) NOT NULL DEFAULT 'IMPORT',
    -- 基于哪个版本创建的修订（仅页面修订来源有值）；不指向表示从零编辑。
    base_version_id BIGINT NULL,
    change_summary VARCHAR(500) NULL,
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

CREATE TABLE scenario_node (
    id BIGINT NOT NULL AUTO_INCREMENT,
    scenario_version_id BIGINT NOT NULL,
    -- RULE_EXECUTION 技术容器，用于关联当前执行记录。
    node_code VARCHAR(80) NOT NULL,
    -- 当前执行类型为 RULE_EXECUTION，不表示业务阶段或工具能力。
    node_type VARCHAR(40) NOT NULL,
    name VARCHAR(120) NOT NULL,
    sequence_no INT NOT NULL,
    -- 技术容器的依赖清单为空；业务先后由规则显式引用决定。
    depends_on JSON NOT NULL,
    -- 规则执行技术容器的配置。
    config_json JSON NULL,
    enabled TINYINT(1) NOT NULL,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
    PRIMARY KEY (id),
    CONSTRAINT uq_scenario_node_version_code UNIQUE (scenario_version_id, node_code),
    CONSTRAINT uq_scenario_node_version_sequence UNIQUE (scenario_version_id, sequence_no),
    CONSTRAINT chk_scenario_node_sequence CHECK (sequence_no >= 1),
    CONSTRAINT fk_scenario_node_version
        FOREIGN KEY (scenario_version_id) REFERENCES scenario_version (id)
        ON DELETE RESTRICT ON UPDATE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- ---------------------------------------------------------------------------
-- 3b. 场景配置草稿、审核方案、报告快照与操作日志
-- ---------------------------------------------------------------------------

-- 场景配置草稿：每个场景一条。revision_no 是草稿修订号（材料/字段/规则/报告配置的
-- 每次写入都会递增），发布必须携带页面看到的修订号；source 记录草稿来源
-- （FORM=页面直接建立；EXCEL=由规则库导入写入；COPY=复制场景；REVISION=基于某个
-- 已发布版本创建的修订），base_version_no 记录它基于的版本号（没有则为 NULL——
-- 不伪造“第 0 版”）。
CREATE TABLE scenario_draft (
    id BIGINT NOT NULL AUTO_INCREMENT,
    scenario_code VARCHAR(80) NOT NULL,
    schema_key VARCHAR(80) NOT NULL,
    schema_version VARCHAR(40) NOT NULL,
    schema_json JSON NOT NULL,
    revision_no INT NOT NULL DEFAULT 0,
    source VARCHAR(20) NOT NULL DEFAULT 'FORM',
    base_version_no INT NULL,
    -- 配置草稿的来源格式版本：由规则库导入 / 版本修订 / 复制从原始 manifest 继承，
    -- 发布时按它导出；NULL 表示页面表单新建、没有历史格式来源。
    profile_version VARCHAR(40) NULL,
    -- 最近一次保存时草稿还不能发布的原因（编译未通过：缺规则、缺识别类型等）。
    -- NULL 表示当前草稿可以通过发布检查。这是可展示状态，不是「已经发布」：
    -- 已发布版本只在管理员显式发布时产生，保存草稿不会改它。
    sync_error JSON NULL,
    -- 计算规则候选（定义、参数、判定区间）。发布时按这份候选重新编译。
    -- NULL 与空数组都表示这份草稿没有计算配置，不是「使用全部计算」。
    -- 历史版本只有编译产物、没有候选时，存为 {"frozen": [...]}，发布前核对字段与节点仍在。
    -- 停用行允许其他业务列未填，仍保留来源供场景核对与版本修订。
    -- 场景业务定义整体保存 JSON：节点、材料、动态字段、规则、计算与报告。
    config_json JSON NOT NULL,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
    PRIMARY KEY (id),
    CONSTRAINT uq_scenario_draft_code UNIQUE (scenario_code),
    CONSTRAINT chk_scenario_draft_revision CHECK (revision_no >= 0),
    CONSTRAINT chk_scenario_draft_base CHECK (base_version_no IS NULL OR base_version_no >= 1)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- 审核方案：对某个场景版本的可复用审核范围选择，保存即生效。
-- 方案只有一份配置，没有草稿/发布之分；scope_json 是每次保存时算好的范围快照
-- （显式规则、自动补入依赖、未启用规则、任务 schema 快照与场景版本引用）。
-- 已建任务不受方案后续改动影响：创建时冻结 manifest_json 与 task_schema_json。
CREATE TABLE audit_plan (
    id BIGINT NOT NULL AUTO_INCREMENT,
    plan_code VARCHAR(80) NOT NULL,
    plan_name VARCHAR(255) NOT NULL,
    scenario_code VARCHAR(80) NOT NULL,
    -- 这份方案从哪一版场景**复制**而来。只是溯源：执行与范围都不再读它，
    -- 场景后来发布新版本也不会改变本方案（要换基准请另建方案）。
    scenario_version_id BIGINT NULL,
    description TEXT NULL,
    enabled TINYINT(1) NOT NULL DEFAULT 1,
    deleted_at DATETIME(6) NULL,
    -- 场景级、方案里只读的那部分上下文：节点实例、选用 Sheet 与协议版本。
    -- 不参与方案编辑，但编译（compile_form_manifest）必须有，因此随方案冻结一份。
    -- 方案的编译产物：报告渲染与任务冻结都读它。编译未成功时为空，原因见 sync_error。
    manifest_json JSON NULL,
    -- 最近一次保存时范围未能生效的原因（编译未通过：没选规则、字段引用缺失等）。
    -- NULL 表示当前范围已生效。这是可展示状态，不是静默降级：页面据此提示
    -- “待核对 + 具体原因”，已建任务保持创建时的快照不变。
    sync_error JSON NULL,
    scope_hash CHAR(64) NULL,
    scope_json JSON NULL,
    -- 场景业务定义整体保存 JSON：节点、材料、动态字段、规则、计算与报告。
    config_json JSON NOT NULL,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
    PRIMARY KEY (id),
    CONSTRAINT uq_audit_plan_code UNIQUE (plan_code),
    CONSTRAINT fk_audit_plan_scenario_version
        FOREIGN KEY (scenario_version_id) REFERENCES scenario_version (id)
        ON DELETE RESTRICT ON UPDATE RESTRICT,
    KEY idx_audit_plan_scenario (scenario_code),
    KEY idx_audit_plan_deleted (deleted_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- 已提交业务写操作的追加日志。后台长操作仍记录在 background_operation；
-- 本表只记录已成功提交的同步写操作，供操作查询与幂等重放使用。
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
    detail_json JSON NULL,
    failure_code VARCHAR(80) NULL,
    failure_message TEXT NULL,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
    PRIMARY KEY (id),
    CONSTRAINT uq_operation_log_no UNIQUE (log_no),
    CONSTRAINT uq_operation_log_idempotency UNIQUE (object_type, operation_type, idempotency_key),
    KEY idx_operation_log_object (object_type, object_no, created_at),
    KEY idx_operation_log_created (created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- 审核任务：业务值文件指针与任务执行元数据。
CREATE TABLE audit_task (
    id BIGINT NOT NULL AUTO_INCREMENT,
    task_no VARCHAR(40) NOT NULL,
    scenario_version_id BIGINT NOT NULL,
    -- 任务创建时使用的审核方案。
    plan_id BIGINT NULL,
    task_name VARCHAR(255) NOT NULL,
    -- 任务备注：属于可编辑的基本资料，不进入执行快照。
    remark TEXT NULL,
    status VARCHAR(40) NOT NULL,
    current_node VARCHAR(80) NOT NULL,
    -- 本任务的执行快照：**方案的编译产物整份冻结**（材料/字段/规则/报告章节/节点）。
    -- 执行期只读它，不读场景版本、也不读方案当前配置——方案后来怎么改都不影响已建任务。
    manifest_json JSON NOT NULL,
    task_schema_json JSON NOT NULL,
    -- 流程进度：当前节点、阶段与节点序号。任务自身的进度标记，不是图状态快照。
    checkpoint_json JSON NULL,
    -- 本次任务的业务取值文件：{"relative_path": ..., "record_count": N}。
    -- 字段值、计算结果与规则结论的取值都在它指向的 CSV 里。
    value_file_json JSON NULL,
    -- 任务输入修订号：首次补传、替换材料和人工有效值变化都会递增。
    -- 写入口在对象锁内核对命令携带的修订号，过期命令不得覆盖新输入。
    input_revision INT NOT NULL DEFAULT 0,
    -- 保留现有删除时间列以免要求重建数据库；审核任务当前永久删除，不再读写此列。
    deleted_at DATETIME(6) NULL,
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
    KEY idx_audit_task_version_status (scenario_version_id, status),
    KEY idx_audit_task_version_node (scenario_version_id, current_node),
    KEY idx_audit_task_plan (plan_id),
    KEY idx_audit_task_deleted (deleted_at),
    CONSTRAINT fk_audit_task_current_node
        FOREIGN KEY (scenario_version_id, current_node)
        REFERENCES scenario_node (scenario_version_id, node_code)
        ON DELETE RESTRICT ON UPDATE RESTRICT,
    KEY idx_audit_task_status_current_node (status, current_node)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- 报告输出节点生成的一份报告快照。报告由真实节点生成，不提供空白报告手填结论；
-- 重新生成是新快照，历史快照保留。名称与备注可编辑，正文、三信号与快照范围不可改写。
-- origin=NODE 表示由报告节点写入。
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
    reasons_json JSON NULL,
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
-- 5. 材料位、文件上传与处理产物
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
    -- 上传时由用户从 8018 支持目录中选择的材料类型，只属于本次上传：
    -- 替换文件可改选，历史上传版本保留各自当时声明的值。它是提交给 8018 的 doc_type。
    declared_material_type VARCHAR(80) NOT NULL,
    size_bytes BIGINT NOT NULL,
    sha256 CHAR(64) NOT NULL,
    status VARCHAR(30) NOT NULL,
    is_current TINYINT(1) NOT NULL,
    -- 生成列把"每个材料位只有一个当前文件"变成数据库层可约束的唯一键。
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

CREATE TABLE stored_artifact (
    id BIGINT NOT NULL AUTO_INCREMENT,
    artifact_no VARCHAR(40) NOT NULL,
    document_upload_id BIGINT NOT NULL,
    audit_task_id BIGINT NOT NULL,
    artifact_type VARCHAR(40) NOT NULL,
    relative_path VARCHAR(500) NOT NULL,
    -- 上传身份：这份产物由哪个上传文件产生（同一次上传重跑同一产物时不变）。
    upload_sha256 CHAR(64) NOT NULL,
    -- 产物内容版本：产物实际内容的哈希。同一 PDF 重新识别后上传哈希不变但内容可能不同，
    -- 索引绑定内容哈希才能识别出“产物已重建”。历史行为 NULL，重建后写入。
    content_sha256 CHAR(64) NULL,
    status VARCHAR(30) NOT NULL,
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
    KEY idx_stored_artifact_content (document_upload_id, artifact_type, content_sha256)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;


-- ---------------------------------------------------------------------------
-- 6. 节点执行、字段事实、规则结果、证据与人工复核
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
    result_json JSON NULL,
    failure_code VARCHAR(80) NULL,
    failure_message TEXT NULL,
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
    KEY idx_node_execution_task_status (audit_task_id, status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE task_field_value (
    id BIGINT NOT NULL AUTO_INCREMENT,
    audit_task_id BIGINT NOT NULL,
    scenario_version_id BIGINT NOT NULL,
    field_key VARCHAR(40) NOT NULL,
    material_key VARCHAR(40) NOT NULL,
    document_upload_id BIGINT NOT NULL,
    -- 事实版本号：每次抽取或人工纠错都新增一版，绝不原地覆盖。
    revision_no INT NOT NULL DEFAULT 1,
    -- 当前选择：同一任务同一字段同一材料位最多一行为 1，由下面的生成列唯一键保证。
    is_current TINYINT(1) NOT NULL DEFAULT 1,
    status VARCHAR(40) NOT NULL,
    evidence_id BIGINT NULL,
    -- 产生这条事实的执行批次；人工纠错产生的版本没有抽取执行，记为 NULL。
    extraction_execution_id BIGINT NULL,
    -- 事实来源：DOCUMENT_EXTRACTION=模型抽取；HUMAN_CORRECTION=人工主动纠错。
    origin VARCHAR(30) NOT NULL DEFAULT 'DOCUMENT_EXTRACTION',
    -- 只有 is_current=1 的行才有值，因此唯一键对历史版本不生效（MySQL 允许多个 NULL）。
    current_fact_scope VARCHAR(120)
        GENERATED ALWAYS AS (IF(is_current = 1, CONCAT(field_key, ':', material_key), NULL)) STORED,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
    PRIMARY KEY (id),
    CONSTRAINT uq_task_field_value_revision
        UNIQUE (audit_task_id, scenario_version_id, field_key, material_key, document_upload_id, revision_no),
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
    KEY idx_task_field_lookup (audit_task_id, scenario_version_id, field_key, material_key, document_upload_id),
    KEY idx_task_field_evidence (evidence_id),
    KEY idx_task_field_current (audit_task_id, is_current)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- ---------------------------------------------------------------------------
-- 6b. 场景字段及计算结果的事实索引与查询投影
-- 业务值的执行读取以任务 CSV 为准；本表仍服务身份、输入口径、依赖及证据关联查询。
-- 期间、报表口径和主体范围来自当前场景配置，不预设财报或合同场景。
-- 重跑使用 revision_no 区分记录；同一作用域只有一个当前事实。
-- ---------------------------------------------------------------------------

CREATE TABLE task_fact (
    id BIGINT NOT NULL AUTO_INCREMENT,
    audit_task_id BIGINT NOT NULL,
    scenario_version_id BIGINT NOT NULL,
    -- 业务事实编号：EXTRACTED 事实是字段编号（field_code），
    -- DERIVED 事实是计算结果编号（result_code）。
    fact_code VARCHAR(80) NOT NULL,
    -- 事实种类：EXTRACTED=字段抽取结果；DERIVED=计算结果；MANUAL=人工修正结果。
    fact_kind VARCHAR(20) NOT NULL,
    -- 材料位。DERIVED 事实没有单一材料归属时为 NULL（计算可能跨材料）。
    material_key VARCHAR(40) NULL,
    entity_scope VARCHAR(20) NOT NULL DEFAULT 'SELF',
    period_scope VARCHAR(30) NOT NULL,
    statement_scope VARCHAR(20) NOT NULL,
    value_type VARCHAR(20) NOT NULL,
    -- 产生这条事实的节点执行批次。
    source_execution_id BIGINT NULL,
    -- 计算结果状态：COMPUTED / DIVISION_BY_ZERO / INPUT_MISSING / UNDECIDABLE。
    calculation_status VARCHAR(30) NULL,
    calculation_rule_code VARCHAR(80) NULL,
    revision_no INT NOT NULL DEFAULT 1,
    is_current TINYINT(1) NOT NULL DEFAULT 1,
    status VARCHAR(40) NOT NULL,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
    PRIMARY KEY (id),
    CONSTRAINT uq_task_fact_revision UNIQUE (
        audit_task_id, fact_code, material_key, period_scope, statement_scope, entity_scope, revision_no
    ),
    CONSTRAINT uq_task_fact_current UNIQUE (audit_task_id, current_fact_scope),
    CONSTRAINT uq_task_fact_id_task UNIQUE (id, audit_task_id),
    CONSTRAINT chk_task_fact_revision CHECK (revision_no >= 1),
    CONSTRAINT fk_task_fact_task
        FOREIGN KEY (audit_task_id, scenario_version_id)
        REFERENCES audit_task (id, scenario_version_id)
        ON DELETE RESTRICT ON UPDATE RESTRICT,
    CONSTRAINT fk_task_fact_execution_task
        FOREIGN KEY (source_execution_id, audit_task_id)
        REFERENCES node_execution (id, audit_task_id)
        ON DELETE RESTRICT ON UPDATE RESTRICT,
    -- 当前事实的唯一定位：只有 is_current=1 的行才有值，因此唯一键对历史版本不生效。
    -- 各维度使用字符长度前缀；NULL 使用 -1:，不与任意真实材料键混淆。
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
    KEY idx_task_fact_lookup (audit_task_id, fact_code, period_scope, statement_scope, entity_scope),
    KEY idx_task_fact_kind (audit_task_id, fact_kind, is_current),
    KEY idx_task_fact_current (audit_task_id, is_current),
    KEY idx_task_fact_rule (audit_task_id, calculation_rule_code)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE rule_result (
    id BIGINT NOT NULL AUTO_INCREMENT,
    audit_task_id BIGINT NOT NULL,
    node_execution_id BIGINT NOT NULL,
    -- 当前结果对应的规则内部唯一身份，结合执行批次识别结果。
    rule_key VARCHAR(80) NOT NULL,
    -- 规则的业务编号，用于关联原始规则库。
    rule_code VARCHAR(80) NULL,
    rule_name VARCHAR(255) NOT NULL,
    status VARCHAR(40) NOT NULL,
    message TEXT NOT NULL,
    -- 风险等级：风险结果列表按它筛选与排序，因此单独成列而不是压进 JSON。
    risk_level VARCHAR(40) NULL,
    -- 执行诊断：模型原始响应、格式修正记录、循环状态、未解决事项与本条输入引用。
    -- 业务结论（产出、计算结果、风险上下文等）不在这里，按记录 id 从任务 CSV 取。
    result_json JSON NULL,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
    PRIMARY KEY (id),
    CONSTRAINT uq_rule_result_execution_rule UNIQUE (node_execution_id, rule_key),
    CONSTRAINT uq_rule_result_id_task UNIQUE (id, audit_task_id),
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
    -- 修改前后的真实值：只保存数量无法说明人工到底改了什么。
    before_value TEXT NULL,
    after_value TEXT NULL,
    -- 人工结论的依据类型：HUMAN_STATEMENT=人工业务判断；DOCUMENT_EVIDENCE=人工指定了文档证据；
    -- UPLOAD_RECORD=依据真实上传记录。人工依据与文档证据必须分开，旧引文不能冒充新值证明。
    basis_type VARCHAR(30) NULL,
    -- 产生本次裁决的后台命令编号，用于把裁决与命令对账。
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
-- 7. 延迟添加的复合外键
-- 两端互相引用，等相关表都创建后直接补充；本文件只在空库执行。
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
-- 8. 执行结果确认
--
-- 预期：不返回任何行。返回的每一行表示该表没有被创建出来。
-- 注意：本查询只检查表是否存在，不校验已存在表的结构；结构校验由应用启动时的
-- runtime/schema_check.py 只读完成，它发现问题会拒绝启动而不是自动修表。
-- ---------------------------------------------------------------------------


SELECT expected.table_name AS missing_table
FROM (
SELECT 'scenario_template' AS table_name
    UNION ALL
SELECT 'background_operation'
    UNION ALL
SELECT 'import_job'
    UNION ALL
SELECT 'scenario_version'
    UNION ALL
SELECT 'scenario_node'
    UNION ALL
SELECT 'scenario_draft'
    UNION ALL
SELECT 'audit_plan'
    UNION ALL
SELECT 'audit_task'
    UNION ALL
SELECT 'audit_report'
    UNION ALL
SELECT 'operation_log'
    UNION ALL
SELECT 'task_document'
    UNION ALL
SELECT 'document_upload'
    UNION ALL
SELECT 'stored_artifact'
    UNION ALL
SELECT 'node_execution'
    UNION ALL
SELECT 'task_field_value'
    UNION ALL
SELECT 'task_fact'
    UNION ALL
SELECT 'rule_result'
    UNION ALL
SELECT 'evidence'
    UNION ALL
SELECT 'manual_review'
) AS expected
LEFT JOIN information_schema.tables AS actual
       ON actual.table_schema = DATABASE()
      AND actual.table_name = expected.table_name
WHERE actual.table_name IS NULL
ORDER BY expected.table_name;
