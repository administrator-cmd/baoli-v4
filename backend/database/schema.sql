-- ============================================================================
-- baoli-v4 合同审核验证版：完整数据库结构（唯一权威脚本）
--
-- 适用数据库：baoli_v4（应用与 checkpoint 运行时都只允许连接该库）
-- 前置条件：MySQL >= 8.0.19（已按 MySQL 8.4.11 核对）
--   - 业务表使用 utf8mb4_0900_ai_ci 排序规则，需要 MySQL 8.0+
--   - 第 8 节的 checkpoint 表由 langgraph-checkpoint-mysql 3.0.0 使用。
--     该包要求 MySQL >= 8.0.19：除了建表 DDL，它的运行期 UPSERT 语句还使用
--     8.0.19 引入的 INSERT ... AS new 行别名语法，因此 8.0.11～8.0.18 不适用。
--   - 本文件已去掉该包建表 DDL 里 JSON 列的表达式默认值（需要 8.0.13+，
--     且 saver 每次都会显式写入该列，并不依赖默认值），建表本身不再受此限制。
--
-- 用法：
--   本文件是**唯一的结构脚本**：在全新的 baoli_v4 库上执行一次，得到完整结构。
--   1. 库不存在时先建库：
--      CREATE DATABASE baoli_v4 CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;
--   2. 以该库为默认库执行本文件。
--   本仓库只保留这一份脚本，不再维护增量脚本。需要改结构时直接改本文件；
--   本地测试前会先清空数据库，
--   因此不存在「已有数据的库需要增量升级」这一路径。
--   注意：本文件的 CREATE TABLE IF NOT EXISTS 只保证表存在，**不会**给已存在的表
--   补列、改约束或加索引——所以结构变更后必须从空库重建，不能在旧库上重复执行。
--
-- 重复执行的行为（重要）：
--   - 建表用 CREATE TABLE IF NOT EXISTS：已存在的表被跳过（结构是否一致不校验）；
--   - 版本计数器回填用 INSERT ... ON DUPLICATE KEY UPDATE，计数器只增不减；
--   - 触发器采用「先建新版（_r2）→ 再删旧名字」的顺序，重复执行不会出现保护空窗；
--   再次强调：重复执行只能补齐缺失的表/触发器，不能把旧结构升级成本文件的结构。
--
-- 需要注意的代价：
--   IF NOT EXISTS 只保证表存在，**不会校验已存在表的结构**。如果某张表已存在但列
--   或约束与本文不同，本文件不会修正它。执行完请查看文件末尾的确认查询；
--   它只会列出缺失的表名，结构是否一致由应用启动时的只读校验（runtime/schema_check.py）
--   负责发现并拒绝启动，不会自动修表。
--
-- 重要约束：
--   - 应用启动与请求过程只检查连接和表结构，绝不执行 DDL、不调用 create_all()、
--     不调用 saver.setup()、不自动修表。
--   - 表结构以 backend/app/models.py 为准，本文件必须与 ORM 保持一致。
--
-- 业务主流程（表与状态取值都按这条链组织，任何一步都不得跳过）：
--   场景草稿（scenario_draft*，可编辑、不生效）
--     -> 显式发布（scenario_version 及其关系投影，不可覆盖）
--     -> 方案钉住某个已发布版本号（audit_plan.scenario_version_id）
--     -> 任务冻结方案执行配置与范围（audit_task.manifest_json / task_schema_json）
--   规则库导入只写草稿，不在这里发布；导入预览不保留第二套编译/发布状态机。
--
-- 目录：
--   1. 审核模板身份与后台操作
--   2. 导入作业与候选
--   3. 已发布模板版本及关系投影
--   3b. 场景配置草稿、审核方案、报告快照与操作日志
--   4. 审核任务与流程等待点摘要
--   5. 材料位、文件上传与处理产物
--   6. 节点执行、字段事实、规则结果、证据与人工复核
--   7. 延迟添加的复合外键
--   8. LangGraph checkpoint 表（来自 langgraph-checkpoint-mysql 3.0.0）
--   9. 不可变性与快照保护触发器
-- ============================================================================


-- ---------------------------------------------------------------------------
-- 1. 审核模板身份与后台操作
-- ---------------------------------------------------------------------------

-- 审核场景身份与版本计数器。
-- scenario_code 是场景身份（稳定标识），不是中文名称；版本号由本表原子分配，
-- 不依赖无保护的 max(version_no)+1。
-- 基本资料（名称/业务类型/说明/启停/逻辑删除）直接维护在本表；已发布版本保存的是
-- 发布时刻的名称与配置快照，不随本表修改而变。
CREATE TABLE IF NOT EXISTS scenario_template (
    id BIGINT NOT NULL AUTO_INCREMENT,
    scenario_code VARCHAR(80) NOT NULL,
    scenario_name VARCHAR(255) NOT NULL,
    -- 业务类型是页面维护的展示与筛选属性，不参与执行语义。
    category VARCHAR(80) NULL,
    description TEXT NULL,
    -- 启停：停用后不用于新建方案与任务；已创建任务按原快照继续。
    enabled TINYINT(1) NOT NULL DEFAULT 1,
    -- 逻辑删除：保留历史版本、任务与报告。仅删除场景身份，不级联清理任何历史资料。
    deleted_at DATETIME(6) NULL,
    latest_version_no INT NOT NULL DEFAULT 0,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
    PRIMARY KEY (id),
    CONSTRAINT uq_scenario_template_code UNIQUE (scenario_code),
    CONSTRAINT chk_scenario_template_version CHECK (latest_version_no >= 0),
    KEY idx_scenario_template_deleted (deleted_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- 已有数据库的一次性回填：把现存已发布版本的最大版本号写进计数器，避免新版本号与旧版本冲突。
-- 它引用 scenario_version，因此必须放在第 3 节该表建好之后，不能紧跟在 scenario_template 后面。

-- 后台长操作：先认领记录，再由后台用独立 Session 执行；请求只返回受理状态。
-- object_no 是业务编号（作业号或任务号），跨两种对象，因此不设外键。
CREATE TABLE IF NOT EXISTS background_operation (
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

CREATE TABLE IF NOT EXISTS import_job (
    id BIGINT NOT NULL AUTO_INCREMENT,
    job_no VARCHAR(40) NOT NULL,
    source_filename VARCHAR(255) NOT NULL,
    source_path VARCHAR(500) NOT NULL,
    source_sha256 CHAR(64) NOT NULL,
    source_size BIGINT NOT NULL,
    profile_version VARCHAR(40) NOT NULL,
    -- 上传入口的**场景身份**：用户先建场景、再在该场景下上传 Excel。
    -- 场景不由 Excel 表头推断——猜错场景会拿另一套列协议解释它，报出一堆与真实问题
    -- 无关的缺列错误，而用户手里的文件根本没变。历史作业没有这一列，
    -- 读取方按「未记录场景」如实处理，不自动补一个场景。
    scenario_code VARCHAR(80) NULL,
    -- 统一模板协议的版本标识。系统只有一份协议，这里只记录「按哪一版协议解释」。
    profile_display_version VARCHAR(40) NULL,
    status VARCHAR(40) NOT NULL,
    workbook_ir_json JSON NOT NULL,
    proposals_json JSON NULL,
    decisions_json JSON NULL,
    manifest_draft_json JSON NULL,
    -- 每条来源业务记录的处置（候选 / 明确排除 / 待处理问题）及合并拆分关系。
    coverage_json JSON NULL,
    -- 草稿修订号：绑定、排除、裁决、候选原地修正都会递增；过期提交被明确拒绝。
    draft_revision INT NOT NULL DEFAULT 0,
    -- **历史**作业当时的发布检查绑定（profile 版本、草稿修订、manifest 规范化哈希、
    -- 场景身份、逐条确认的能力缺口）。导入页已不再产生新的绑定：导入预览只展示转换结果，
    -- 编译与发布状态机只由场景页的显式发布承担。该列保留是为了让历史作业能按原样读取，
    -- 不批量改写历史数据。
    precheck_json JSON NULL,
    failure_code VARCHAR(80) NULL,
    failure_message TEXT NULL,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
    PRIMARY KEY (id),
    CONSTRAINT uq_import_job_job_no UNIQUE (job_no),
    CONSTRAINT chk_import_job_source_size CHECK (source_size >= 0),
    KEY idx_import_job_status_created_at (status, created_at),
    -- 与上面互补：本表带 workbook_ir_json 等大 JSON 列，列表按 (created_at, id) 倒序分页时
    -- 若命不中索引就会 filesort，把整行塞进排序缓冲（默认仅 256KB）并报 1038 Out of sort memory。
    KEY idx_import_job_created_at_id (created_at, id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS import_sheet (
    id BIGINT NOT NULL AUTO_INCREMENT,
    import_job_id BIGINT NOT NULL,
    sheet_key VARCHAR(40) NOT NULL,
    sheet_name VARCHAR(255) NOT NULL,
    visible TINYINT(1) NOT NULL,
    status VARCHAR(40) NOT NULL,
    bound_role VARCHAR(40) NULL,
    -- 绑定的节点实例码。一张 Sheet 只能绑定一个节点；一个节点可以绑定多张 Sheet。
    -- 空表示未绑定：未绑定的 Sheet 不进入转换、发布与执行范围，也不自动推断用途。
    bound_node_code VARCHAR(80) NULL,
    header_row INT NULL,
    snapshot_json JSON NOT NULL,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
    PRIMARY KEY (id),
    CONSTRAINT uq_import_sheet_job_sheet_key UNIQUE (import_job_id, sheet_key),
    CONSTRAINT chk_import_sheet_header_row CHECK (header_row IS NULL OR header_row >= 1),
    CONSTRAINT fk_import_sheet_job
        FOREIGN KEY (import_job_id) REFERENCES import_job (id)
        ON DELETE RESTRICT ON UPDATE RESTRICT,
    KEY idx_import_sheet_job_status (import_job_id, status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS import_issue (
    id BIGINT NOT NULL AUTO_INCREMENT,
    import_job_id BIGINT NOT NULL,
    issue_no VARCHAR(40) NOT NULL,
    category VARCHAR(40) NOT NULL,
    severity VARCHAR(20) NOT NULL,
    status VARCHAR(30) NOT NULL,
    title VARCHAR(255) NOT NULL,
    description TEXT NOT NULL,
    source_json JSON NOT NULL,
    options_json JSON NOT NULL,
    decision_json JSON NULL,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
    PRIMARY KEY (id),
    CONSTRAINT uq_import_issue_job_issue_no UNIQUE (import_job_id, issue_no),
    CONSTRAINT fk_import_issue_job
        FOREIGN KEY (import_job_id) REFERENCES import_job (id)
        ON DELETE RESTRICT ON UPDATE RESTRICT,
    KEY idx_import_issue_job_status (import_job_id, status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS field_candidate (
    id BIGINT NOT NULL AUTO_INCREMENT,
    import_job_id BIGINT NOT NULL,
    candidate_ref VARCHAR(120) NOT NULL,
    name VARCHAR(255) NOT NULL,
    value_type VARCHAR(30) NOT NULL,
    material_ref VARCHAR(120) NOT NULL,
    proposal_json JSON NOT NULL,
    status VARCHAR(30) NOT NULL,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
    PRIMARY KEY (id),
    CONSTRAINT uq_field_candidate_job_ref UNIQUE (import_job_id, candidate_ref),
    CONSTRAINT fk_field_candidate_job
        FOREIGN KEY (import_job_id) REFERENCES import_job (id)
        ON DELETE RESTRICT ON UPDATE RESTRICT,
    KEY idx_field_candidate_job_status (import_job_id, status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;


-- ---------------------------------------------------------------------------
-- 3. 已发布模板版本及关系投影
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS scenario_version (
    id BIGINT NOT NULL AUTO_INCREMENT,
    -- 页面表单 / 复制来源的版本没有导入作业（import_job_id 为 NULL），
    -- 不伪造 Excel 作业与工作表行号；旧数据保持原 import_job_id 不变。
    import_job_id BIGINT NULL,
    scenario_code VARCHAR(80) NOT NULL,
    scenario_name VARCHAR(255) NOT NULL,
    version_no INT NOT NULL,
    status VARCHAR(30) NOT NULL,
    -- 来源：IMPORT=规则库导入；FORM=页面表单发布；COPY=复制场景；REVISION=从某一版本创建的修订。
    source VARCHAR(20) NOT NULL DEFAULT 'IMPORT',
    -- 基于哪个版本创建的修订（仅页面修订来源有值）；不指向表示从零编辑。
    base_version_id BIGINT NULL,
    change_summary VARCHAR(500) NULL,
    manifest_hash CHAR(64) NOT NULL,
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

CREATE TABLE IF NOT EXISTS scenario_material (
    id BIGINT NOT NULL AUTO_INCREMENT,
    scenario_version_id BIGINT NOT NULL,
    material_key VARCHAR(40) NOT NULL,
    name VARCHAR(255) NOT NULL,
    required TINYINT(1) NOT NULL,
    source_json JSON NOT NULL,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
    PRIMARY KEY (id),
    CONSTRAINT uq_scenario_material_version_key UNIQUE (scenario_version_id, material_key),
    CONSTRAINT fk_scenario_material_version
        FOREIGN KEY (scenario_version_id) REFERENCES scenario_version (id)
        ON DELETE RESTRICT ON UPDATE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS scenario_node (
    id BIGINT NOT NULL AUTO_INCREMENT,
    scenario_version_id BIGINT NOT NULL,
    -- node_code 承载节点**实例码**（例如 extract_balance_sheet），在场景版本内唯一。
    -- 保留列名不改：它在 17 个文件、313 处被引用，改名会牵动全部读写路径而收益为零。
    node_code VARCHAR(40) NOT NULL,
    -- node_type 是节点**能力类型**（MATERIAL_CHECK / CALCULATION / RISK_RATING …）。
    -- 它与实例码是两件事：同一能力类型在场景里可以有多个实例。
    -- 旧数据（线性流程）在迁移中按 node_code 回填，因此非空。
    node_type VARCHAR(40) NOT NULL,
    name VARCHAR(120) NOT NULL,
    sequence_no INT NOT NULL,
    -- 依赖的节点实例码清单（JSON 数组）。空数组表示无依赖，可以立即就绪。
    -- sequence_no 只用于页面展示与确定性排序，**不代替**依赖关系。
    depends_on JSON NOT NULL,
    -- 节点级配置（例如计算节点绑定的计算规则编号清单）。
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

CREATE TABLE IF NOT EXISTS scenario_rule (
    id BIGINT NOT NULL AUTO_INCREMENT,
    scenario_version_id BIGINT NOT NULL,
    -- rule_key 是**历史**规则身份（由 namespace + 来源编号派生的稳定键）。
    -- 新版本不再生成新的 rule_key；新规则的身份在 rule_code 列。
    -- 这一列保留不删：历史 manifest、历史任务与历史导出都按它读取。
    rule_key VARCHAR(40) NOT NULL,
    -- rule_code 是业务规则编号，直接来自 Excel 的「规则ID」列。
    -- 可空只用于表达「这条规则来自旧格式，没有业务编号」：新导入的规则必须非空。
    rule_code VARCHAR(80) NULL,
    -- 这条规则的历史身份。与 rule_key 同值，单独留一列是为了让新规则
    -- 在读取时不需要区分「哪个字段是历史身份」：legacy_rule_key 恒为历史口径。
    legacy_rule_key VARCHAR(40) NULL,
    name VARCHAR(255) NOT NULL,
    node_code VARCHAR(40) NOT NULL,
    operator_code VARCHAR(40) NOT NULL,
    enabled TINYINT(1) NOT NULL,
    rule_json JSON NOT NULL,
    source_json JSON NOT NULL,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
    PRIMARY KEY (id),
    CONSTRAINT uq_scenario_rule_version_key UNIQUE (scenario_version_id, rule_key),
    CONSTRAINT fk_scenario_rule_version
        FOREIGN KEY (scenario_version_id) REFERENCES scenario_version (id)
        ON DELETE RESTRICT ON UPDATE RESTRICT,
    CONSTRAINT fk_scenario_rule_version_node
        FOREIGN KEY (scenario_version_id, node_code)
        REFERENCES scenario_node (scenario_version_id, node_code)
        ON DELETE RESTRICT ON UPDATE RESTRICT,
    KEY idx_scenario_rule_version_node_enabled (scenario_version_id, node_code, enabled),
    KEY idx_scenario_rule_version_code (scenario_version_id, rule_code)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- 模板版本计数器的回填（在 scenario_version 建好之后执行）。
-- 把现存已发布版本的最大版本号写进计数器，避免后续发布分配到已被占用的版本号。
-- 全新初始化的库里 scenario_version 为空，这条语句不会插入任何行。
-- 可重复执行：已存在的模板身份不会被重建，计数器只增不减。
-- 这里不使用 ON DUPLICATE KEY UPDATE + VALUES()（该写法自 MySQL 8.0.20 起已弃用），
-- 改为给子查询起别名后引用它。
INSERT INTO scenario_template (scenario_code, scenario_name, latest_version_no)
SELECT derived.scenario_code, derived.scenario_name, derived.max_version
FROM (
    SELECT scenario_code, MAX(scenario_name) AS scenario_name, MAX(version_no) AS max_version
    FROM scenario_version
    GROUP BY scenario_code
) AS derived
ON DUPLICATE KEY UPDATE
    latest_version_no = GREATEST(scenario_template.latest_version_no, derived.max_version);


-- ---------------------------------------------------------------------------
-- 3b. 场景配置草稿、审核方案、报告快照与操作日志
-- ---------------------------------------------------------------------------

-- 场景配置草稿：每个场景一条。revision_no 是草稿修订号（材料/字段/规则/报告配置的
-- 每次写入都会递增），发布必须携带页面看到的修订号；source 记录草稿来源
-- （FORM=页面直接建立；EXCEL=从导入发布派生；COPY=复制场景；REVISION=基于某个
-- 已发布版本创建的修订），base_version_no 记录它基于的版本号（没有则为 NULL——
-- 不伪造“第 0 版”）。
CREATE TABLE IF NOT EXISTS scenario_draft (
    id BIGINT NOT NULL AUTO_INCREMENT,
    scenario_code VARCHAR(80) NOT NULL,
    revision_no INT NOT NULL DEFAULT 0,
    source VARCHAR(20) NOT NULL DEFAULT 'FORM',
    base_version_no INT NULL,
    -- 配置草稿的来源格式版本：由导入发布 / 版本修订 / 复制从原始 manifest 继承，
    -- 发布时按它导出；NULL 表示页面表单新建、没有历史格式来源。
    profile_version VARCHAR(40) NULL,
    -- 最近一次保存时草稿还不能发布的原因（编译未通过：缺规则、缺识别类型等）。
    -- NULL 表示当前草稿可以通过发布检查。这是可展示状态，不是「已经发布」：
    -- 已发布版本只在管理员显式发布时产生，保存草稿不会改它。
    sync_error JSON NULL,
    -- 计算规则候选（定义、参数、判定区间）。发布时按这份候选重新编译。
    -- NULL 与空数组都表示这份草稿没有计算配置，不是「使用全部计算」。
    -- 历史版本只有编译产物、没有候选时，存为 {"frozen": [...]}，发布前核对字段与节点仍在。
    calculation_json JSON NULL,
    -- 停用行允许其他业务列未填，仍保留来源供场景核对与版本修订。
    disabled_source_rows JSON NULL,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
    PRIMARY KEY (id),
    CONSTRAINT uq_scenario_draft_code UNIQUE (scenario_code),
    CONSTRAINT chk_scenario_draft_revision CHECK (revision_no >= 0),
    CONSTRAINT chk_scenario_draft_base CHECK (base_version_no IS NULL OR base_version_no >= 1)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- ---------------------------------------------------------------------------
-- 2b. 场景配置草稿的节点实例
--
-- 节点的存在、顺序与依赖由**用户配置**，不由系统按规则内容或固定序列自动生成。
-- 一个场景可以有两个同类节点（两张报表各一个字段抽取），因此 node_code 是实例码、
-- node_type 才是能力类型，两者不能混用。
--
-- 节点与 Sheet 是一对多：一个节点可以绑定多张 Sheet（财报的计算定义/参数/判定三表
-- 都绑到同一个 CALCULATION 实例上），一张 Sheet 只能绑定一个节点
-- （见 import_sheet.bound_node_code）。
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS scenario_draft_node (
    id BIGINT NOT NULL AUTO_INCREMENT,
    draft_id BIGINT NOT NULL,
    -- 节点实例码：同一场景内唯一，规则按它归属。
    node_code VARCHAR(80) NOT NULL,
    -- 能力类型：来自统一模板协议的 node_types，决定谁执行这个节点。
    node_type VARCHAR(40) NOT NULL,
    name VARCHAR(120) NOT NULL,
    sequence_no INT NOT NULL,
    -- 上游节点**实例码**列表。发布期校验缺失依赖、自依赖、循环依赖与顺序冲突。
    depends_on JSON NOT NULL,
    -- 节点级配置（例如计算节点参与的计算规则编号）；为空表示没有额外配置。
    config_json JSON NULL,
    enabled TINYINT(1) NOT NULL DEFAULT 1,
    sort_no INT NOT NULL DEFAULT 0,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
    PRIMARY KEY (id),
    CONSTRAINT uq_scenario_draft_node_code UNIQUE (draft_id, node_code),
    CONSTRAINT chk_scenario_draft_node_sequence CHECK (sequence_no >= 1),
    CONSTRAINT fk_scenario_draft_node_draft
        FOREIGN KEY (draft_id) REFERENCES scenario_draft (id)
        ON DELETE CASCADE ON UPDATE RESTRICT,
    KEY idx_scenario_draft_node_draft (draft_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- ---------------------------------------------------------------------------
-- 2c. 场景选用的 Sheet
--
-- 场景负责配置「使用哪些 Sheet、Sheet 对应哪些节点及其流程关系」；统一模板协议
-- 负责定义「系统支持哪些 Sheet 以及每张 Sheet 的结构」。两者职责分离，
-- 但**不形成两套 Profile 文件**。
--
-- 这张表就是「这个场景用哪些 Sheet」的持久化落点。缺了它，模板下载只能按草稿里
-- 已有内容反推 Sheet（有材料→材料清单表，有规则→按节点类型反查），于是
-- 「本场景选了哪些表」这件事没有权威来源：反推会在合同版与财报版同类 Sheet 之间
-- 无法区分，也会把用户明确排除的表重新算进来。
--
-- node_code 指向该 Sheet 在本场景里归属的节点实例；一个节点可以绑多张 Sheet，
-- 一张 Sheet 只能绑一个节点（与 import_sheet.bound_node_code 同一口径）。
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS scenario_draft_sheet (
    id BIGINT NOT NULL AUTO_INCREMENT,
    draft_id BIGINT NOT NULL,
    -- Sheet 角色：来自统一模板协议的 sheet_role。
    sheet_role VARCHAR(80) NOT NULL,
    -- 该 Sheet 归属的节点实例码。为空表示本场景选用这张表但尚未指定节点。
    node_code VARCHAR(80) NULL,
    sort_no INT NOT NULL DEFAULT 0,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
    PRIMARY KEY (id),
    CONSTRAINT uq_scenario_draft_sheet_role UNIQUE (draft_id, sheet_role),
    CONSTRAINT fk_scenario_draft_sheet_draft
        FOREIGN KEY (draft_id) REFERENCES scenario_draft (id)
        ON DELETE CASCADE ON UPDATE RESTRICT,
    KEY idx_scenario_draft_sheet_draft (draft_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS scenario_draft_material (
    id BIGINT NOT NULL AUTO_INCREMENT,
    draft_id BIGINT NOT NULL,
    material_key VARCHAR(40) NOT NULL,    name VARCHAR(255) NOT NULL,
    required TINYINT(1) NOT NULL,
    description TEXT NOT NULL,
    -- 来源业务编号（Excel 的「材料编号」列）：导出时写回，页面新建的行留空。
    source_ref VARCHAR(80) NULL,
    origin_json JSON NULL,
    sort_no INT NOT NULL DEFAULT 0,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
    PRIMARY KEY (id),
    CONSTRAINT uq_scenario_draft_material_key UNIQUE (draft_id, material_key),
    CONSTRAINT fk_scenario_draft_material_draft
        FOREIGN KEY (draft_id) REFERENCES scenario_draft (id)
        ON DELETE RESTRICT ON UPDATE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS scenario_draft_field (
    id BIGINT NOT NULL AUTO_INCREMENT,
    draft_id BIGINT NOT NULL,
    field_key VARCHAR(40) NOT NULL,
    material_key VARCHAR(40) NOT NULL,
    name VARCHAR(255) NOT NULL,
    value_type VARCHAR(30) NOT NULL,
    unit VARCHAR(40) NULL,
    -- 币种是金额比较与加总的口径之一：留空表示未声明，不表示人民币。
    currency VARCHAR(40) NULL,
    -- 产物来源：该字段的取值由哪种处理产物提供（当前仅印章识别 SEAL）。
    -- 留空表示字段由常规字段抽取提供。
    artifact_source VARCHAR(30) NULL,
    -- 取数期间与报表口径：**由字段自己声明**，计算参数按这两项定位字段事实。
    -- 参数表不再声明它们，因此这里是唯一的来源；必填、无默认值。
    period_scope VARCHAR(30) NOT NULL,
    statement_scope VARCHAR(20) NOT NULL,
    required TINYINT(1) NOT NULL,
    instruction TEXT NOT NULL,
    -- 来源业务编号（Excel 的「字段编号」列）：导出时写回，页面新建的字段留空。
    source_ref VARCHAR(80) NULL,
    origin_json JSON NULL,
    sort_no INT NOT NULL DEFAULT 0,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
    PRIMARY KEY (id),
    CONSTRAINT uq_scenario_draft_field_key UNIQUE (draft_id, field_key),
    CONSTRAINT fk_scenario_draft_field_draft
        FOREIGN KEY (draft_id) REFERENCES scenario_draft (id)
        ON DELETE RESTRICT ON UPDATE RESTRICT,
    KEY idx_scenario_draft_field_material (draft_id, material_key)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS scenario_draft_rule (
    id BIGINT NOT NULL AUTO_INCREMENT,
    draft_id BIGINT NOT NULL,
    rule_key VARCHAR(40) NOT NULL,
    name VARCHAR(255) NOT NULL,
    node_code VARCHAR(40) NOT NULL,
    operator_code VARCHAR(40) NOT NULL,
    enabled TINYINT(1) NOT NULL DEFAULT 1,
    -- 规则的业务定义：输入对象（材料/字段引用）、审核要求、参数、适用条件、
    -- 风险来源规则与等级建议。发布时由 compile_form_manifest 编译成 manifest 规则。
    rule_json JSON NOT NULL,
    -- 来源业务编号（Excel 的「规则编号」列）：导出时写回，页面新建的规则留空。
    source_ref VARCHAR(80) NULL,
    origin_json JSON NULL,
    sort_no INT NOT NULL DEFAULT 0,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
    PRIMARY KEY (id),
    CONSTRAINT uq_scenario_draft_rule_key UNIQUE (draft_id, rule_key),
    CONSTRAINT fk_scenario_draft_rule_draft
        FOREIGN KEY (draft_id) REFERENCES scenario_draft (id)
        ON DELETE RESTRICT ON UPDATE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS scenario_draft_section (
    id BIGINT NOT NULL AUTO_INCREMENT,
    draft_id BIGINT NOT NULL,
    section_key VARCHAR(40) NOT NULL,
    title VARCHAR(255) NOT NULL,
    content_instruction TEXT NOT NULL,
    field_keys JSON NOT NULL,
    sort_no INT NOT NULL DEFAULT 0,
    -- 节点版（contract_review_v3）报告分组属性：报告编号、报告名称、材料范围、
    -- 输出格式、章节顺序、内容类型、结果排序与业务配置状态。旧格式章节保持 NULL，
    -- 继续按「附加展示章节」语义渲染与导出。
    report_code VARCHAR(80) NULL,
    report_name VARCHAR(255) NULL,
    material_keys JSON NULL,
    formats JSON NULL,
    section_order INT NULL,
    content_type VARCHAR(40) NULL,
    result_sort VARCHAR(60) NULL,
    config_status VARCHAR(30) NULL,
    origin_json JSON NULL,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
    PRIMARY KEY (id),
    CONSTRAINT uq_scenario_draft_section_key UNIQUE (draft_id, section_key),
    CONSTRAINT fk_scenario_draft_section_draft
        FOREIGN KEY (draft_id) REFERENCES scenario_draft (id)
        ON DELETE RESTRICT ON UPDATE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- 审核方案：对某个场景版本的可复用审核范围选择，保存即生效。
-- 方案只有一份配置，没有草稿/发布之分；scope_json 是每次保存时算好的范围快照
-- （显式规则、自动补入依赖、未启用规则、任务 schema 快照与场景版本引用）。
-- 已建任务不受方案后续改动影响：创建时冻结 manifest_json 与 task_schema_json。
CREATE TABLE IF NOT EXISTS audit_plan (
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
    context_json JSON NULL,
    -- 方案的编译产物：报告渲染与任务冻结都读它。编译未成功时为空，原因见 sync_error。
    manifest_json JSON NULL,
    manifest_hash CHAR(64) NULL,
    -- 最近一次保存时范围未能生效的原因（编译未通过：没选规则、字段引用缺失等）。
    -- NULL 表示当前范围已生效。这是可展示状态，不是静默降级：页面据此提示
    -- “待核对 + 具体原因”，已建任务保持创建时的快照不变。
    sync_error JSON NULL,
    scope_hash CHAR(64) NULL,
    scope_json JSON NULL,
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

-- 方案自己的配置：创建时从钉住的场景版本复制一份，之后由用户在方案里增删改。
-- 形状与 scenario_draft_* 一一对应，只是归属换成 plan_id —— 方案是场景版本的
-- **可编辑副本**，配置本身归方案所有，不再随场景版本变化。
CREATE TABLE IF NOT EXISTS plan_material (
    id BIGINT NOT NULL AUTO_INCREMENT,
    plan_id BIGINT NOT NULL,
    material_key VARCHAR(40) NOT NULL,
    name VARCHAR(255) NOT NULL,
    required TINYINT(1) NOT NULL,
    description TEXT NOT NULL,
    source_ref VARCHAR(80) NULL,
    origin_json JSON NULL,
    sort_no INT NOT NULL DEFAULT 0,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
    PRIMARY KEY (id),
    CONSTRAINT uq_plan_material_key UNIQUE (plan_id, material_key),
    CONSTRAINT fk_plan_material_plan
        FOREIGN KEY (plan_id) REFERENCES audit_plan (id)
        ON DELETE RESTRICT ON UPDATE RESTRICT,
    KEY idx_plan_material_plan (plan_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS plan_field (
    id BIGINT NOT NULL AUTO_INCREMENT,
    plan_id BIGINT NOT NULL,
    field_key VARCHAR(40) NOT NULL,
    material_key VARCHAR(40) NOT NULL,
    name VARCHAR(255) NOT NULL,
    value_type VARCHAR(30) NOT NULL,
    unit VARCHAR(40) NULL,
    currency VARCHAR(40) NULL,
    artifact_source VARCHAR(30) NULL,
    period_scope VARCHAR(30) NOT NULL,
    statement_scope VARCHAR(20) NOT NULL,
    required TINYINT(1) NOT NULL,
    instruction TEXT NOT NULL,
    source_ref VARCHAR(80) NULL,
    origin_json JSON NULL,
    sort_no INT NOT NULL DEFAULT 0,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
    PRIMARY KEY (id),
    CONSTRAINT uq_plan_field_key UNIQUE (plan_id, field_key),
    CONSTRAINT fk_plan_field_plan
        FOREIGN KEY (plan_id) REFERENCES audit_plan (id)
        ON DELETE RESTRICT ON UPDATE RESTRICT,
    KEY idx_plan_field_plan (plan_id),
    KEY idx_plan_field_material (plan_id, material_key)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- 方案自己的规则。enabled 就是「这条规则要不要在这个方案里跑」：
-- 用户在方案页勾选与取消，直接改它。不再区分「系统派生 / 业务规则」。
CREATE TABLE IF NOT EXISTS plan_rule (
    id BIGINT NOT NULL AUTO_INCREMENT,
    plan_id BIGINT NOT NULL,
    rule_key VARCHAR(40) NOT NULL,
    name VARCHAR(255) NOT NULL,
    node_code VARCHAR(40) NOT NULL,
    operator_code VARCHAR(40) NOT NULL,
    enabled TINYINT(1) NOT NULL DEFAULT 1,
    rule_json JSON NOT NULL,
    source_ref VARCHAR(80) NULL,
    origin_json JSON NULL,
    sort_no INT NOT NULL DEFAULT 0,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
    PRIMARY KEY (id),
    CONSTRAINT uq_plan_rule_key UNIQUE (plan_id, rule_key),
    CONSTRAINT fk_plan_rule_plan
        FOREIGN KEY (plan_id) REFERENCES audit_plan (id)
        ON DELETE RESTRICT ON UPDATE RESTRICT,
    KEY idx_plan_rule_plan (plan_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS plan_section (
    id BIGINT NOT NULL AUTO_INCREMENT,
    plan_id BIGINT NOT NULL,
    section_key VARCHAR(40) NOT NULL,
    title VARCHAR(255) NOT NULL,
    content_instruction TEXT NOT NULL,
    field_keys JSON NOT NULL,
    sort_no INT NOT NULL DEFAULT 0,
    report_code VARCHAR(80) NULL,
    report_name VARCHAR(255) NULL,
    material_keys JSON NULL,
    formats JSON NULL,
    section_order INT NULL,
    content_type VARCHAR(40) NULL,
    result_sort VARCHAR(60) NULL,
    config_status VARCHAR(30) NULL,
    origin_json JSON NULL,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
    PRIMARY KEY (id),
    CONSTRAINT uq_plan_section_key UNIQUE (plan_id, section_key),
    CONSTRAINT fk_plan_section_plan
        FOREIGN KEY (plan_id) REFERENCES audit_plan (id)
        ON DELETE RESTRICT ON UPDATE RESTRICT,
    KEY idx_plan_section_plan (plan_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- 业务写操作的持久记录：只记录已成功提交的同步写操作；后台长命令的记录在
-- background_operation，两者按编号并列查询，不互相复制。
-- 同一对象同一操作类型下幂等标识唯一：响应丢失后的重发返回原结果，绝不重复创建。
CREATE TABLE IF NOT EXISTS operation_log (
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


-- ---------------------------------------------------------------------------
-- 4. 审核任务与流程等待点摘要
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS audit_task (
    id BIGINT NOT NULL AUTO_INCREMENT,
    task_no VARCHAR(40) NOT NULL,
    scenario_version_id BIGINT NOT NULL,
    -- 任务创建时使用的审核方案。旧任务没有方案关联时为空，页面显示“历史任务配置”，
    -- 不猜测迁移、不改写历史结果。（外键在本节内直接建立。）
    plan_id BIGINT NULL,
    task_name VARCHAR(255) NOT NULL,
    -- 任务备注：属于可编辑的基本资料，不进入执行快照。
    remark TEXT NULL,
    status VARCHAR(40) NOT NULL,
    current_node VARCHAR(40) NOT NULL,
    thread_id VARCHAR(64) NOT NULL,
    -- 本任务的执行快照：**方案的编译产物整份冻结**（材料/字段/规则/报告章节/节点/Sheet）。
    -- 执行期只读它，不读场景版本、也不读方案当前配置——方案后来怎么改都不影响已建任务。
    manifest_json JSON NOT NULL,
    task_schema_json JSON NOT NULL,
    checkpoint_json JSON NULL,
    pending_review_hash CHAR(64) NULL,
    -- 任务输入修订号：首次补传、替换材料和人工有效值变化都会递增。
    -- 写入口在对象锁内核对命令携带的修订号，过期命令不得覆盖新输入。
    input_revision INT NOT NULL DEFAULT 0,
    -- 逻辑删除：历史事实、报告与文件保留；有活跃后台操作时拒绝删除。
    deleted_at DATETIME(6) NULL,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
    PRIMARY KEY (id),
    CONSTRAINT uq_audit_task_task_no UNIQUE (task_no),
    CONSTRAINT uq_audit_task_thread_id UNIQUE (thread_id),
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

-- 业务流程等待点的摘要，用于业务查询与一致性交叉校验。
-- 它不是 LangGraph 的 checkpoint，不能替代第 8 节的 checkpoints 表。
CREATE TABLE IF NOT EXISTS workflow_checkpoint (
    id BIGINT NOT NULL AUTO_INCREMENT,
    audit_task_id BIGINT NOT NULL,
    thread_id VARCHAR(64) NOT NULL,
    checkpoint_ns VARCHAR(128) NOT NULL,
    status VARCHAR(30) NOT NULL,
    state_json JSON NOT NULL,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
    PRIMARY KEY (id),
    CONSTRAINT uq_workflow_checkpoint_task_namespace UNIQUE (audit_task_id, checkpoint_ns),
    CONSTRAINT fk_workflow_checkpoint_task
        FOREIGN KEY (audit_task_id) REFERENCES audit_task (id)
        ON DELETE RESTRICT ON UPDATE RESTRICT,
    KEY idx_workflow_checkpoint_thread_status (thread_id, status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- 报告输出节点生成的一份报告快照。报告由真实节点生成，不提供空白报告手填结论；
-- 重新生成是新快照，历史快照保留。名称与备注可编辑，正文、三信号与快照范围不可改写。
-- origin=NODE 表示由报告节点写入；LEGACY_BACKFILL 表示增量升级时从历史节点结果按原值回填。
CREATE TABLE IF NOT EXISTS audit_report (
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
    origin VARCHAR(20) NOT NULL DEFAULT 'NODE',
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

CREATE TABLE IF NOT EXISTS task_document (
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

CREATE TABLE IF NOT EXISTS document_upload (
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

CREATE TABLE IF NOT EXISTS stored_artifact (
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

CREATE TABLE IF NOT EXISTS node_execution (
    id BIGINT NOT NULL AUTO_INCREMENT,
    audit_task_id BIGINT NOT NULL,
    scenario_version_id BIGINT NOT NULL,
    node_code VARCHAR(40) NOT NULL,
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

CREATE TABLE IF NOT EXISTS task_field_value (
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
    raw_value TEXT NULL,
    normalized_value_json JSON NULL,
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
-- 6b. 统一事实表：字段抽取事实与计算派生事实
--
-- 为什么需要它：task_field_value 的唯一范围是
-- (任务, 字段, 材料位, 上传版本, 版本号)，它表达不了财报场景的三个必要维度——
-- **期间**（本期/上期/期初/期末）、**报表口径**（合并/母公司）与**主体范围**。
-- 同一个「营业收入」在本期与上期是两个不同的数，在 task_field_value 里无处安放。
--
-- 与 task_field_value 的关系：两者并存，不是替换。task_field_value 继续承载
-- 合同审核的字段事实与其不可变版本、证据关联；task_fact 承载财报场景需要
-- 多维口径的事实。合同审核的既有读取路径一行都不改。
--
-- 事实不可变：重跑新增 revision_no 并置 is_current，旧行保留为历史。
-- 同一 (任务, 事实编号, 期间, 报表口径, 主体范围) 最多一条当前事实，
-- 由生成列唯一键在数据库层保证——与 task_field_value 同一手法。
--
-- RAG 只保存证据、原文与检索信息，**不是** decimal_value 的权威来源。
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS task_fact (
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
    -- 数值事实用 DECIMAL(38, 10)：财报金额可达万亿量级，10 位小数足够容纳
    -- 除法中间结果。用定点而非浮点：float 的二进制误差会让边界判定随机翻转。
    decimal_value DECIMAL(38, 10) NULL,
    text_value TEXT NULL,
    date_value DATE NULL,
    unit VARCHAR(40) NULL,
    currency VARCHAR(40) NULL,
    -- 产生这条事实的节点执行批次。
    source_execution_id BIGINT NULL,
    -- DERIVED 事实的输入事实快照：本次计算实际用到的每一版事实（fact_id + revision_no）、
    -- 表达式版本哈希与缺失参数。没有它就无法回答「这个数是按哪一版输入算出来的」。
    input_snapshot_json JSON NULL,
    -- 计算结果的判定结果（命中哪条判定区间、给出什么业务结论）。仅 DERIVED 事实有。
    judgement_json JSON NULL,
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
    -- material_key 可能为 NULL，CONCAT 会把 NULL 吞掉，因此用 COALESCE 归一成固定串。
    current_fact_scope VARCHAR(255) GENERATED ALWAYS AS (
        IF(
            is_current = 1,
            CONCAT(
                fact_code, ':', COALESCE(material_key, '-'), ':',
                period_scope, ':', statement_scope, ':', entity_scope
            ),
            NULL
        )
    ) VIRTUAL,
    KEY idx_task_fact_lookup (audit_task_id, fact_code, period_scope, statement_scope, entity_scope),
    KEY idx_task_fact_kind (audit_task_id, fact_kind, is_current),
    KEY idx_task_fact_current (audit_task_id, is_current),
    KEY idx_task_fact_rule (audit_task_id, calculation_rule_code)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS rule_result (
    id BIGINT NOT NULL AUTO_INCREMENT,
    audit_task_id BIGINT NOT NULL,
    node_execution_id BIGINT NOT NULL,
    -- rule_key 是历史规则身份；新任务的规则身份在 rule_code。
    -- 唯一键仍建在 rule_key 上：历史任务按它去重，新任务写入时 rule_key 与
    -- rule_code 取同一个业务编号，因此唯一性口径不变。
    rule_key VARCHAR(40) NOT NULL,
    -- 业务规则编号。历史结果为空（当时没有这个口径），新结果必须非空。
    rule_code VARCHAR(80) NULL,
    legacy_rule_key VARCHAR(40) NULL,
    rule_name VARCHAR(255) NOT NULL,
    status VARCHAR(40) NOT NULL,
    message TEXT NOT NULL,
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
    KEY idx_rule_result_task_code (audit_task_id, rule_code)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS evidence (
    id BIGINT NOT NULL AUTO_INCREMENT,
    evidence_no VARCHAR(40) NOT NULL,
    audit_task_id BIGINT NOT NULL,
    document_upload_id BIGINT NULL,
    rule_result_id BIGINT NULL,
    field_value_id BIGINT NULL,
    page_no INT NULL,
    quote_text TEXT NOT NULL,
    bbox_json JSON NULL,
    artifact_id BIGINT NULL,
    -- 引文/页码/坐标是否真的能在所属产物里核对：
    -- VERIFIED=已在指定产物页内核对通过；UNVERIFIED=产物没有可定位依据（例如只给出文本块）；
    -- UNLOCATABLE=声明了位置但核对失败。任何情况下都不伪造高亮。
    locator_status VARCHAR(30) NOT NULL DEFAULT 'UNVERIFIED',
    locator_note TEXT NULL,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
    PRIMARY KEY (id),
    CONSTRAINT uq_evidence_no UNIQUE (evidence_no),
    CONSTRAINT uq_evidence_id_task UNIQUE (id, audit_task_id),
    CONSTRAINT chk_evidence_reference CHECK (
        document_upload_id IS NOT NULL OR rule_result_id IS NOT NULL OR field_value_id IS NOT NULL
    ),
    CONSTRAINT chk_evidence_page_no CHECK (page_no IS NULL OR page_no >= 1),
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

CREATE TABLE IF NOT EXISTS manual_review (
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


-- 复核草稿：未提交的复核答案按“待办版本”持久化暂存。
-- pending_hash 是保存草稿时任务的待办版本；读取只返回与当前待办版本一致的草稿，
-- 旧草稿不会套用到变化后的待办；提交（正式裁决）仍走后台命令。
CREATE TABLE IF NOT EXISTS review_draft (
    id BIGINT NOT NULL AUTO_INCREMENT,
    audit_task_id BIGINT NOT NULL,
    review_no VARCHAR(40) NOT NULL,
    pending_hash CHAR(64) NOT NULL,
    decision VARCHAR(40) NULL,
    business_note TEXT NULL,
    field_value TEXT NULL,
    basis_type VARCHAR(30) NULL,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
    PRIMARY KEY (id),
    CONSTRAINT uq_review_draft_task_review UNIQUE (audit_task_id, review_no),
    CONSTRAINT fk_review_draft_task
        FOREIGN KEY (audit_task_id) REFERENCES audit_task (id)
        ON DELETE RESTRICT ON UPDATE RESTRICT,
    KEY idx_review_draft_task_hash (audit_task_id, pending_hash)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;


-- ---------------------------------------------------------------------------
-- 6c. Agent 工具调用账本
--
-- 为什么必须有这张表：LangGraph checkpoint 只负责**恢复流程状态**，
-- 它不回答「这次审核里模型调了哪些工具、传了什么、得到什么、依据哪一版事实」。
-- 把审计账本寄托在 checkpoint 上，会导致：
--   - interrupt/resume 后无法判断某个 tool_call_id 是否已经执行过，只能重跑；
--   - 工具结果与当时的事实快照失去绑定，事后无法复现结论；
--   - 模型看到的是哪一版工具定义无从查证。
--
-- 因此工具调用的**请求事实**与**结果**都落在这里，与 checkpoint 各自提交、
-- 通过 tool_call_id 对账（见 services/agent_tool_ledger.py）。
--
-- tool_call_id 唯一：同一个调用不可能执行两次。恢复时按状态区分处理：
--   SUCCEEDED → 读原结果，不重复执行；
--   RUNNING   → 显式进入待核查，不直接重跑；
--   FAILED    → 保留原错误，是否重试由明确动作触发。
--
-- 快照与策略版本随每条记录冻结：工具结果必须绑定到**当时**冻结的事实快照与
-- 工具目录版本，否则「这个数是按哪一版输入算出来的」无法回答。
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS agent_tool_call (
    id BIGINT NOT NULL AUTO_INCREMENT,
    audit_task_id BIGINT NULL,
    -- 规则评估批次：一次评估内的事实集合是冻结的，同一批次的调用共享快照身份。
    -- 非空：本表只承载 Agent 工具调用，缺失评估身份说明调用来源不合法，必须显式失败
    -- （见 services/agent_tool_ledger.py 的 require 校验），不允许写一条无归属的记录。
    evaluation_id VARCHAR(80) NOT NULL,
    -- 规则实例：同一条规则定义作用于不同材料组合时是不同实例，结果不能互相覆盖。非空理由同上。
    rule_instance_id VARCHAR(160) NOT NULL,
    -- 模型给出的调用标识。唯一约束建在它上面（见下方 UNIQUE）。
    tool_call_id VARCHAR(120) NOT NULL,
    -- 明确重试的关联：失败后**不允许**用同一个 tool_call_id 重试，必须由模型发起新的调用标识，
    -- 并把原调用的标识写在这里。为空表示这是一次全新调用，不是重试。
    retry_of_tool_call_id VARCHAR(120) NULL,
    tool_name VARCHAR(80) NOT NULL,
    tool_version VARCHAR(20) NULL,
    -- 工具目录版本与内容指纹：回答「模型当时看到的是哪一版工具定义」。
    -- 指纹由目录内容算出，不由人维护，因此能发现「版本号没改但内容变了」。
    tool_catalog_version VARCHAR(20) NOT NULL,
    tool_catalog_hash CHAR(64) NOT NULL,
    -- 任务冻结的工具策略版本。与调用时的目录版本不一致即协议冲突。
    tool_policy_version VARCHAR(20) NULL,
    -- 模型给出的参数原文（JSON）。它只含业务口径与身份引用，
    -- 会话、任务 ID、快照身份等运行期依赖不在这里，也不允许模型填写。
    arguments_json JSON NOT NULL,
    -- 本次调用依据的输入事实快照身份：与 arguments 一起决定「这次调用算的是什么」。
    -- 恢复时 arguments 或 snapshot hash 与原记录不同，直接判协议冲突，不按新参数重算。
    -- 非空：只存 hash 不存快照等于无法复现，只存快照不存 hash 等于无法对账。
    input_snapshot_hash CHAR(64) NOT NULL,
    fact_snapshot_ref VARCHAR(120) NOT NULL,
    -- 调用方身份（节点执行 / Agent 循环 / 人工重试），用于审计追溯。
    caller_identity VARCHAR(80) NULL,
    -- PENDING / RUNNING / SUCCEEDED / FAILED。RUNNING 表示可能已发出但结果未知，
    -- 恢复时必须先核查而不是重跑。
    status VARCHAR(20) NOT NULL,
    result_json JSON NULL,
    error_json JSON NULL,
    -- 外部作业号（若该工具调用过外部服务）。有它才能查询原作业而不是重发。
    external_job_id VARCHAR(120) NULL,
    started_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    completed_at DATETIME(6) NULL,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
    PRIMARY KEY (id),
    -- 同一个 tool_call_id 只能有一条记录：这是「不重复执行」的数据库级保证。
    CONSTRAINT uq_agent_tool_call_id UNIQUE (tool_call_id),
    CONSTRAINT fk_agent_tool_call_task
        FOREIGN KEY (audit_task_id) REFERENCES audit_task (id)
        ON DELETE RESTRICT ON UPDATE RESTRICT,
    -- 按任务+评估批次查一次评估的全部调用（报告与页面据此列出取证过程）。
    KEY idx_agent_tool_call_evaluation (audit_task_id, evaluation_id),
    KEY idx_agent_tool_call_rule (rule_instance_id),
    KEY idx_agent_tool_call_retry (retry_of_tool_call_id),
    KEY idx_agent_tool_call_status (status),
    KEY idx_agent_tool_call_started (started_at, id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- 工具调用账本是**追加+状态推进**的账本，不是可任意改写的普通表：
--
-- 1. 调用身份一经写入即不可改：改掉它们等于把历史调用解释成另一次调用。
--    参与冻结的是**完整调用身份**——任务、评估批次、规则实例、工具与版本、
--    工具目录版本与内容指纹、工具策略版本、参数、事实快照引用与内容指纹、
--    调用方身份、重试关联。任一列可改都会让「同一次调用」的判据出现缺口。
-- 2. 终态（SUCCEEDED / FAILED）一经形成不允许改为任何其他状态：否则
--    「同一个 tool_call_id 不重复执行」的保证会被一次状态回退绕过。
--
-- 先删后建：本文件要能重复执行，同名触发器必须先清理（DROP IF EXISTS 不报错）。
DROP TRIGGER IF EXISTS trg_agent_tool_call_identity_immutable;
DELIMITER $$
CREATE TRIGGER trg_agent_tool_call_identity_immutable
BEFORE UPDATE ON agent_tool_call
FOR EACH ROW
BEGIN
    IF NOT (NEW.audit_task_id <=> OLD.audit_task_id)
       OR NOT (NEW.evaluation_id <=> OLD.evaluation_id)
       OR NOT (NEW.rule_instance_id <=> OLD.rule_instance_id)
       OR NEW.tool_call_id <> OLD.tool_call_id
       OR NOT (NEW.retry_of_tool_call_id <=> OLD.retry_of_tool_call_id)
       OR NEW.tool_name <> OLD.tool_name
       OR NOT (NEW.tool_version <=> OLD.tool_version)
       OR NEW.tool_catalog_version <> OLD.tool_catalog_version
       OR NEW.tool_catalog_hash <> OLD.tool_catalog_hash
       OR NOT (NEW.tool_policy_version <=> OLD.tool_policy_version)
       OR NOT (NEW.arguments_json <=> OLD.arguments_json)
       OR NOT (NEW.input_snapshot_hash <=> OLD.input_snapshot_hash)
       OR NOT (NEW.fact_snapshot_ref <=> OLD.fact_snapshot_ref)
       OR NOT (NEW.caller_identity <=> OLD.caller_identity)
    THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'agent tool call identity is immutable';
    END IF;
    IF OLD.status IN ('SUCCEEDED', 'FAILED') AND NOT (NEW.status <=> OLD.status) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'agent tool call terminal status is immutable';
    END IF;
END$$
DELIMITER ;

-- ---------------------------------------------------------------------------
-- 6d. 规则评估事实快照
--
-- 为什么必须有这张表：`agent_tool_call` 只保存事实快照的**引用与指纹**。
-- 只有指纹意味着工具执行时仍然会去读「当前数据」——同一批次的两次工具调用
-- 可能落到不同的材料版本上，指纹对得上只是巧合。只有引用意味着无法对账。
-- 因此把**冻结的事实内容本身**落在这里：
--   - 工具执行只从这份冻结快照解析事实；
--   - 工具确实需要读取文件或产物时，必须核对其引用、hash 与修订号与本表一致；
--   - 恢复时按 `snapshot_ref` 读回同一份事实，不重新查库。
--
-- `snapshot_ref` 唯一：它同时是可读引用（页面与日志）与恢复入口。
-- `snapshot_hash` 由内容算出（含事实与材料位身份），不由人维护。
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS evaluation_snapshot (
    id BIGINT NOT NULL AUTO_INCREMENT,
    -- 可读引用，形如 facts:<条数>:<短哈希>。唯一，恢复按它读回冻结内容。
    snapshot_ref VARCHAR(120) NOT NULL,
    audit_task_id BIGINT NOT NULL,
    -- 规则评估批次与规则实例：快照按「一条规则实例一次评估」冻结。
    evaluation_id VARCHAR(80) NOT NULL,
    rule_instance_id VARCHAR(160) NOT NULL,
    node_code VARCHAR(80) NOT NULL,
    -- 冻结时的任务输入修订号。恢复时与任务当前修订不一致即说明输入已变，
    -- 不能再把这份快照当成当前输入使用。
    input_revision INT NOT NULL,
    -- 冻结的事实内容（含事实与材料位身份，见 domain/fact_snapshot.py）。
    facts_json JSON NOT NULL,
    fact_snapshot_hash CHAR(64) NOT NULL,
    tool_catalog_version VARCHAR(20) NOT NULL,
    tool_catalog_hash CHAR(64) NOT NULL,
    tool_policy_version VARCHAR(20) NOT NULL,
    -- 本次评估允许调用的工具范围（来自已发布配置的 allowed_tools，非整个目录）。
    allowed_tools_json JSON NOT NULL,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
    PRIMARY KEY (id),
    CONSTRAINT uq_evaluation_snapshot_ref UNIQUE (snapshot_ref),
    -- 一条规则实例在一次评估里只冻结一份事实快照。重复冻结说明同一次评估
    -- 拿到了两份不同的输入，必须显式失败而不是覆盖。
    CONSTRAINT uq_evaluation_snapshot_scope UNIQUE (evaluation_id, rule_instance_id, input_revision),
    CONSTRAINT fk_evaluation_snapshot_task
        FOREIGN KEY (audit_task_id) REFERENCES audit_task (id)
        ON DELETE RESTRICT ON UPDATE RESTRICT,
    KEY idx_evaluation_snapshot_task (audit_task_id, node_code),
    KEY idx_evaluation_snapshot_hash (fact_snapshot_hash)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- 冻结快照的内容不可改写：改写它等于让已经据此执行的工具结果无从解释。
DROP TRIGGER IF EXISTS trg_evaluation_snapshot_immutable;
DELIMITER $$
CREATE TRIGGER trg_evaluation_snapshot_immutable
BEFORE UPDATE ON evaluation_snapshot
FOR EACH ROW
BEGIN
    IF NEW.snapshot_ref <> OLD.snapshot_ref
       OR NOT (NEW.audit_task_id <=> OLD.audit_task_id)
       OR NOT (NEW.evaluation_id <=> OLD.evaluation_id)
       OR NOT (NEW.rule_instance_id <=> OLD.rule_instance_id)
       OR NOT (NEW.node_code <=> OLD.node_code)
       OR NOT (NEW.input_revision <=> OLD.input_revision)
       OR NOT (NEW.facts_json <=> OLD.facts_json)
       OR NOT (NEW.fact_snapshot_hash <=> OLD.fact_snapshot_hash)
       OR NOT (NEW.tool_catalog_version <=> OLD.tool_catalog_version)
       OR NOT (NEW.tool_catalog_hash <=> OLD.tool_catalog_hash)
       OR NOT (NEW.tool_policy_version <=> OLD.tool_policy_version)
       OR NOT (NEW.allowed_tools_json <=> OLD.allowed_tools_json)
    THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'evaluation snapshot is immutable';
    END IF;
END$$
DELIMITER ;

-- ---------------------------------------------------------------------------
-- 7. 延迟添加的复合外键
--
-- 这两条外键的两端互相引用（task_document ↔ document_upload、task_field_value ↔ evidence），
-- 无法在建表时同时声明，只能在两张表都建好之后补加。
-- 为了让本文件可以重复执行，这里先用 information_schema 判断约束是否已存在：
-- 已存在时执行空语句 DO 0，不会重复添加，也不会报错。
-- （MySQL 没有 DROP FOREIGN KEY IF EXISTS，所以不能直接写成先删后加。）

-- 让 current_upload_id 只能指向本材料位自己的上传版本。
SET @fk_exists := (
    SELECT COUNT(*) FROM information_schema.table_constraints
    WHERE constraint_schema = DATABASE()
      AND table_name = 'task_document'
      AND constraint_name = 'fk_task_document_current_upload'
);
SET @fk_sql := IF(@fk_exists = 0,
    'ALTER TABLE task_document ADD CONSTRAINT fk_task_document_current_upload FOREIGN KEY (current_upload_id, id) REFERENCES document_upload (id, task_document_id) ON DELETE RESTRICT ON UPDATE RESTRICT',
    'DO 0');
PREPARE fk_stmt FROM @fk_sql;
EXECUTE fk_stmt;
DEALLOCATE PREPARE fk_stmt;

-- 字段事实指向证据的指针，在 evidence 表建好之后补加。
SET @fk_exists := (
    SELECT COUNT(*) FROM information_schema.table_constraints
    WHERE constraint_schema = DATABASE()
      AND table_name = 'task_field_value'
      AND constraint_name = 'fk_task_field_value_evidence_task'
);
SET @fk_sql := IF(@fk_exists = 0,
    'ALTER TABLE task_field_value ADD CONSTRAINT fk_task_field_value_evidence_task FOREIGN KEY (evidence_id, audit_task_id) REFERENCES evidence (id, audit_task_id) ON DELETE RESTRICT ON UPDATE RESTRICT',
    'DO 0');
PREPARE fk_stmt FROM @fk_sql;
EXECUTE fk_stmt;
DEALLOCATE PREPARE fk_stmt;


-- ---------------------------------------------------------------------------
-- 8. LangGraph checkpoint 表
--
-- 这些表由 langgraph-checkpoint-mysql 3.0.0 的 PyMySQLSaver 使用，
-- 与业务表共用同一个 baoli_v4 库。
--
-- 下面的建表语句是该包 MIGRATIONS 列表（langgraph/checkpoint/mysql/base.py）逐条执行完
-- 之后的**最终结构**，不是迁移过程的中间态：
--   - checkpoint_ns 最终为 VARCHAR(2000)：迁移先扩到 255，再扩到 2000。
--   - checkpoint_ns_hash 最终是普通 BINARY(16) 列（不再是 STORED 生成列）：
--     该包为兼容 MariaDB 在后续迁移里把它改成普通列，由 saver 的 SQL 显式写入
--     UNHEX(MD5(...))。它参与主键，因此必须是 NOT NULL。
--   - 主键与索引为该包全部迁移执行后的最终形态。
--
-- checkpoint_migrations 记录已经应用过的迁移版本。这里一次性写入 0..21（共 22 条，
-- 与该包的 MIGRATIONS 条数一致），表示结构已经是最新：
-- 这样即使有人误调用 saver.setup()，它读到的版本号已是最大值，不会重复执行迁移。
-- 应用自身不会调用 setup()，也不执行任何 DDL。
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS checkpoint_migrations (
    v INTEGER PRIMARY KEY
);

CREATE TABLE IF NOT EXISTS checkpoints (
    thread_id VARCHAR(150) NOT NULL,
    checkpoint_ns VARCHAR(2000) NOT NULL DEFAULT '',
    checkpoint_id VARCHAR(150) NOT NULL,
    parent_checkpoint_id VARCHAR(150) NULL,
    type VARCHAR(150) NULL,
    checkpoint JSON NOT NULL,
    -- 上游迁移写的是 `metadata JSON NOT NULL DEFAULT ('{}')`；JSON 列的表达式默认值
    -- 是 MySQL 8.0.13 才支持的特性，8.0.11/8.0.12 会直接报
    -- "BLOB, TEXT, GEOMETRY or JSON column 'metadata' can't have a default value"，
    -- 导致 checkpoints 建表失败、后续两条 checkpoint 表也建不出来。
    -- saver 的 UPSERT_CHECKPOINTS_SQL 每次都会显式写入 metadata，从不依赖该默认值，
    -- 因此这里去掉默认值，保持与上游相同的列定义与 NOT NULL 约束。
    metadata JSON NOT NULL,
    checkpoint_ns_hash BINARY(16) NOT NULL,
    PRIMARY KEY (thread_id, checkpoint_ns_hash, checkpoint_id),
    KEY checkpoints_thread_id_idx (thread_id),
    KEY checkpoints_checkpoint_id_idx (checkpoint_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS checkpoint_blobs (
    thread_id VARCHAR(150) NOT NULL,
    checkpoint_ns VARCHAR(2000) NOT NULL DEFAULT '',
    channel VARCHAR(150) NOT NULL,
    version VARCHAR(150) NOT NULL,
    type VARCHAR(150) NOT NULL,
    `blob` LONGBLOB NULL,
    checkpoint_ns_hash BINARY(16) NOT NULL,
    PRIMARY KEY (thread_id, checkpoint_ns_hash, channel, version),
    KEY checkpoint_blobs_thread_id_idx (thread_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS checkpoint_writes (
    thread_id VARCHAR(150) NOT NULL,
    checkpoint_ns VARCHAR(2000) NOT NULL DEFAULT '',
    checkpoint_id VARCHAR(150) NOT NULL,
    task_id VARCHAR(150) NOT NULL,
    idx INTEGER NOT NULL,
    channel VARCHAR(150) NOT NULL,
    type VARCHAR(150) NULL,
    `blob` LONGBLOB NOT NULL,
    checkpoint_ns_hash BINARY(16) NOT NULL,
    task_path VARCHAR(2000) NOT NULL DEFAULT '',
    PRIMARY KEY (thread_id, checkpoint_ns_hash, checkpoint_id, task_id, idx),
    KEY checkpoint_writes_thread_id_idx (thread_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- 记录已经应用过的迁移版本。这里一次性写入 0..21（共 22 条，与该包的 MIGRATIONS 条数一致），
-- 表示结构已经是最新：这样即使有人误调用 saver.setup()，它读到的版本号已是最大值，
-- 不会重复执行迁移。应用自身不会调用 setup()，也不执行任何 DDL。
-- ON DUPLICATE KEY UPDATE v = v 是空更新：重复执行时已存在的版本行保持不变，
-- 既不会因主键冲突中断，也不会掩盖其它类型的错误。
INSERT INTO checkpoint_migrations (v) VALUES
    (0), (1), (2), (3), (4), (5), (6), (7), (8), (9), (10),
    (11), (12), (13), (14), (15), (16), (17), (18), (19), (20), (21)
ON DUPLICATE KEY UPDATE v = v;


-- ---------------------------------------------------------------------------
-- 9. 不可变性与快照保护触发器
--
-- 已发布模板版本及其关系投影、任务快照、上传文件出处、字段事实版本与人工复核的
-- 系统原判断都是只能追加、不能被改写的记录。
--
-- 执行顺序（重要）：本版触发器使用带版本后缀的新名字（_r2）。
--   1) 先删除同名的新版触发器（只影响本版自身，旧名字的保护仍在生效）；
--   2) 创建新版触发器——此时保护已经覆盖，不存在"先删后建"的空窗期；
--   3) 最后才删除旧名字的触发器。
-- 因此重复执行本文件不会出现"旧触发器已被删除、新触发器还没建好"的窗口，
-- 也不会因为重复执行而报错（每一步都是 DROP IF EXISTS + CREATE）。
-- MySQL 允许同一事件/时机存在多个触发器，第 2 步与第 3 步之间的短暂共存只是
-- 同一条件的重复保护：先触发的那个 SIGNAL 会中断语句，行为不变。
-- ---------------------------------------------------------------------------

-- 先清理本版触发器（旧名字的保护此时仍然存在）。
DROP TRIGGER IF EXISTS trg_scenario_version_no_update_r2;
DROP TRIGGER IF EXISTS trg_scenario_version_no_delete_r2;
DROP TRIGGER IF EXISTS trg_scenario_material_no_update_r2;
DROP TRIGGER IF EXISTS trg_scenario_material_no_delete_r2;
DROP TRIGGER IF EXISTS trg_scenario_node_no_update_r2;
DROP TRIGGER IF EXISTS trg_scenario_node_no_delete_r2;
DROP TRIGGER IF EXISTS trg_scenario_rule_no_update_r2;
DROP TRIGGER IF EXISTS trg_scenario_rule_no_delete_r2;
-- 任务快照触发器升到 _r3（新增方案绑定 plan_id 不可变）；旧 _r2 在本节末尾才退休，
-- 先建新版再删旧名，不存在保护空窗。
DROP TRIGGER IF EXISTS trg_audit_task_snapshot_immutable_r3;
DROP TRIGGER IF EXISTS trg_task_document_snapshot_immutable_r2;
DROP TRIGGER IF EXISTS trg_document_upload_identity_immutable_r2;
DROP TRIGGER IF EXISTS trg_manual_review_system_judgment_immutable_r2;
DROP TRIGGER IF EXISTS trg_task_field_value_immutable_r2;
-- 报告快照的不可变保护；操作日志只追加。
DROP TRIGGER IF EXISTS trg_audit_report_content_immutable_r2;
DROP TRIGGER IF EXISTS trg_operation_log_immutable_r2;
-- 统一事实表与字段事实表同一口径：事实不可变，重跑新增版本而不是原地改写。
DROP TRIGGER IF EXISTS trg_task_fact_immutable_r2;

DELIMITER $$

-- A version and its relational projections are append-only after publication.
CREATE TRIGGER trg_scenario_version_no_update_r2
BEFORE UPDATE ON scenario_version
FOR EACH ROW
BEGIN
    SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'scenario version is immutable';
END$$

CREATE TRIGGER trg_scenario_version_no_delete_r2
BEFORE DELETE ON scenario_version
FOR EACH ROW
BEGIN
    SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'published scenario version cannot be deleted';
END$$

CREATE TRIGGER trg_scenario_material_no_update_r2
BEFORE UPDATE ON scenario_material
FOR EACH ROW
BEGIN
    SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'published scenario material is immutable';
END$$

CREATE TRIGGER trg_scenario_material_no_delete_r2
BEFORE DELETE ON scenario_material
FOR EACH ROW
BEGIN
    SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'published scenario material cannot be deleted';
END$$

CREATE TRIGGER trg_scenario_node_no_update_r2
BEFORE UPDATE ON scenario_node
FOR EACH ROW
BEGIN
    SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'published scenario node is immutable';
END$$

CREATE TRIGGER trg_scenario_node_no_delete_r2
BEFORE DELETE ON scenario_node
FOR EACH ROW
BEGIN
    SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'published scenario node cannot be deleted';
END$$

CREATE TRIGGER trg_scenario_rule_no_update_r2
BEFORE UPDATE ON scenario_rule
FOR EACH ROW
BEGIN
    SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'published scenario rule is immutable';
END$$

CREATE TRIGGER trg_scenario_rule_no_delete_r2
BEFORE DELETE ON scenario_rule
FOR EACH ROW
BEGIN
    SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'published scenario rule cannot be deleted';
END$$

-- Task execution may advance status and checkpoints, but never replace its immutable snapshot.
-- _r3：方案绑定（plan_id）同样属于执行快照，创建后不可改写；
-- 旧任务（无方案关联）保持 NULL，恢复/删除只动 deleted_at。
CREATE TRIGGER trg_audit_task_snapshot_immutable_r3
BEFORE UPDATE ON audit_task
FOR EACH ROW
BEGIN
    -- 注意：thread_id 不在保护范围内。它是「图实例身份」而不是业务快照：字段纠错需要
    -- 退回节点重新判定，必须换一个新的图实例（沿用旧 thread 会复用已废弃的等待点，
    -- 新等待点无法建立、任务会被误判为失败），因此 thread_id 必须允许变更。
    -- 执行配置与规则范围都在创建时冻结，后续方案或场景编辑不得改变旧任务。
    IF OLD.task_no <> NEW.task_no
       OR OLD.scenario_version_id <> NEW.scenario_version_id
       OR NOT (OLD.plan_id <=> NEW.plan_id)
       OR CAST(OLD.task_schema_json AS CHAR) <> CAST(NEW.task_schema_json AS CHAR)
       OR CAST(OLD.manifest_json AS CHAR) <> CAST(NEW.manifest_json AS CHAR) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'audit task snapshot is immutable';
    END IF;
END$$

-- A task material slot retains the material identity copied from its published scenario.
CREATE TRIGGER trg_task_document_snapshot_immutable_r2
BEFORE UPDATE ON task_document
FOR EACH ROW
BEGIN
    IF OLD.document_no <> NEW.document_no
       OR OLD.audit_task_id <> NEW.audit_task_id
       OR OLD.material_key <> NEW.material_key
       OR OLD.material_name <> NEW.material_name
       OR OLD.required <> NEW.required THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'task material snapshot is immutable';
    END IF;
END$$

-- Superseding a file changes only its lifecycle flags; its physical-file provenance remains fixed.
CREATE TRIGGER trg_document_upload_identity_immutable_r2
BEFORE UPDATE ON document_upload
FOR EACH ROW
BEGIN
    IF OLD.upload_no <> NEW.upload_no
       OR OLD.task_document_id <> NEW.task_document_id
       OR OLD.original_filename <> NEW.original_filename
       OR OLD.relative_path <> NEW.relative_path
       OR OLD.content_type <> NEW.content_type
       OR OLD.size_bytes <> NEW.size_bytes
       OR OLD.sha256 <> NEW.sha256 THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'document upload provenance is immutable';
    END IF;
END$$

-- 字段事实版本不可变：重跑或人工纠错只能新增一版并把旧的置为非当前，
-- 不允许改写任何一版的值、状态、身份或产生来源。允许变化的只有 is_current 与证据指针。
CREATE TRIGGER trg_task_field_value_immutable_r2
BEFORE UPDATE ON task_field_value
FOR EACH ROW
BEGIN
    IF OLD.audit_task_id <> NEW.audit_task_id
       OR OLD.scenario_version_id <> NEW.scenario_version_id
       OR OLD.field_key <> NEW.field_key
       OR OLD.material_key <> NEW.material_key
       OR OLD.document_upload_id <> NEW.document_upload_id
       OR OLD.revision_no <> NEW.revision_no
       OR OLD.origin <> NEW.origin
       OR OLD.status <> NEW.status
       OR NOT (OLD.raw_value <=> NEW.raw_value)
       OR CAST(OLD.normalized_value_json AS CHAR) <> CAST(NEW.normalized_value_json AS CHAR) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'task field value revision is immutable';
    END IF;
END$$

-- 统一事实（task_fact）与字段事实同一口径：重跑新增一版并把旧的置为非当前，
-- 不允许改写任何一版的身份、取值、口径、输入快照或计算状态。
-- 允许变化的只有 is_current。生成列 current_fact_scope 由 is_current 派生，不必比较。
CREATE TRIGGER trg_task_fact_immutable_r2
BEFORE UPDATE ON task_fact
FOR EACH ROW
BEGIN
    IF OLD.audit_task_id <> NEW.audit_task_id
       OR OLD.scenario_version_id <> NEW.scenario_version_id
       OR OLD.fact_code <> NEW.fact_code
       OR OLD.fact_kind <> NEW.fact_kind
       OR NOT (OLD.material_key <=> NEW.material_key)
       OR OLD.period_scope <> NEW.period_scope
       OR OLD.statement_scope <> NEW.statement_scope
       OR OLD.entity_scope <> NEW.entity_scope
       OR OLD.value_type <> NEW.value_type
       OR NOT (OLD.decimal_value <=> NEW.decimal_value)
       OR NOT (OLD.text_value <=> NEW.text_value)
       OR NOT (OLD.date_value <=> NEW.date_value)
       OR NOT (OLD.unit <=> NEW.unit)
       OR NOT (OLD.currency <=> NEW.currency)
       OR OLD.revision_no <> NEW.revision_no
       OR OLD.status <> NEW.status
       OR NOT (OLD.input_snapshot_json <=> NEW.input_snapshot_json)
       OR NOT (OLD.judgement_json <=> NEW.judgement_json)
       OR NOT (OLD.calculation_status <=> NEW.calculation_status)
       OR NOT (OLD.calculation_rule_code <=> NEW.calculation_rule_code) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'task fact revision is immutable';
    END IF;
END$$

-- A reviewer may add a decision and note, but must not replace the system judgment.
CREATE TRIGGER trg_manual_review_system_judgment_immutable_r2
BEFORE UPDATE ON manual_review
FOR EACH ROW
BEGIN
    IF CAST(OLD.system_judgment_json AS CHAR) <> CAST(NEW.system_judgment_json AS CHAR) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'manual review system judgment is immutable';
    END IF;
END$$

-- 报告快照：正文、三信号与范围快照不可改写；只允许改备注、名称与归档状态。
CREATE TRIGGER trg_audit_report_content_immutable_r2
BEFORE UPDATE ON audit_report
FOR EACH ROW
BEGIN
    IF OLD.report_no <> NEW.report_no
       OR OLD.audit_task_id <> NEW.audit_task_id
       OR NOT (OLD.node_execution_id <=> NEW.node_execution_id)
       OR NOT (OLD.content_md <=> NEW.content_md)
       OR OLD.report_generated <> NEW.report_generated
       OR OLD.scope_execution_complete <> NEW.scope_execution_complete
       OR OLD.risk_conclusion_available <> NEW.risk_conclusion_available
       OR CAST(OLD.reasons_json AS CHAR) <> CAST(NEW.reasons_json AS CHAR)
       OR CAST(OLD.snapshot_json AS CHAR) <> CAST(NEW.snapshot_json AS CHAR)
       OR NOT (OLD.input_revision <=> NEW.input_revision)
       OR OLD.generated_at <> NEW.generated_at THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'audit report snapshot is immutable';
    END IF;
END$$

-- 操作日志只追加：不允许修改已写入的日志行（防止事后篡改操作记录）。
CREATE TRIGGER trg_operation_log_immutable_r2
BEFORE UPDATE ON operation_log
FOR EACH ROW
BEGIN
    SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'operation log is append-only';
END$$

DELIMITER ;

-- 新版触发器全部就位之后，才让旧名字的触发器退休。
-- 这一步在任何情况下都不会让保护出现空窗：上面的 _r2/_r3 触发器已经生效。
DROP TRIGGER IF EXISTS trg_scenario_version_no_update;
DROP TRIGGER IF EXISTS trg_scenario_version_no_delete;
DROP TRIGGER IF EXISTS trg_scenario_material_no_update;
DROP TRIGGER IF EXISTS trg_scenario_material_no_delete;
DROP TRIGGER IF EXISTS trg_scenario_node_no_update;
DROP TRIGGER IF EXISTS trg_scenario_node_no_delete;
DROP TRIGGER IF EXISTS trg_scenario_rule_no_update;
DROP TRIGGER IF EXISTS trg_scenario_rule_no_delete;
DROP TRIGGER IF EXISTS trg_audit_task_snapshot_immutable_r2;
DROP TRIGGER IF EXISTS trg_audit_task_snapshot_immutable;
DROP TRIGGER IF EXISTS trg_task_document_snapshot_immutable;
DROP TRIGGER IF EXISTS trg_document_upload_identity_immutable;
DROP TRIGGER IF EXISTS trg_manual_review_system_judgment_immutable;


-- ---------------------------------------------------------------------------
-- 9a. 审核任务材料证据索引与检索（本地 Embedding + 任务范围检索）
--
-- 三张表都是**可重建的派生数据**：原始产物、字段事实与人工决定各自保存，索引不替代它们。
-- 检索范围先按任务、材料、当前上传版本、产物版本与索引版本过滤，再在候选内做向量排序。
-- ---------------------------------------------------------------------------

-- 一个上传版本某个产物的一份索引版本。模型、向量维度或切分方式变化时必须新建版本。
CREATE TABLE IF NOT EXISTS evidence_index_version (
    id BIGINT NOT NULL AUTO_INCREMENT,
    index_no VARCHAR(40) NOT NULL,
    audit_task_id BIGINT NOT NULL,
    document_upload_id BIGINT NOT NULL,
    artifact_id BIGINT NOT NULL,
    artifact_type VARCHAR(40) NOT NULL,
    -- 上传身份：这份索引由哪个上传文件产生。
    artifact_sha256 CHAR(64) NOT NULL,
    -- 产物内容版本：索引实际依据的产物内容哈希。产物重新识别后内容变化，
    -- 旧索引即不再适用于当前产物（必须重建，不能继续沿用）。
    -- 可空只表达「升级前写入、没有内容版本」的历史索引：它们不再可检索。
    artifact_content_sha256 CHAR(64) NULL,
    -- 索引版本标识：格式名 + 向量维度 + 决定因素摘要，长度固定（见 embedding_service）。
    index_version VARCHAR(80) NOT NULL,
    model_id VARCHAR(160) NOT NULL,
    model_revision VARCHAR(80) NOT NULL,
    embedding_dim INT NOT NULL,
    chunk_version VARCHAR(80) NOT NULL,
    -- BUILDING=构建中；READY=整批成功、可检索；FAILED=构建失败；INVALIDATED=不再进入当前检索。
    status VARCHAR(30) NOT NULL,
    chunk_count INT NOT NULL DEFAULT 0,
    failure_code VARCHAR(80) NULL,
    failure_message TEXT NULL,
    built_at DATETIME(6) NULL,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
    PRIMARY KEY (id),
    CONSTRAINT uq_evidence_index_version_no UNIQUE (index_no),
    CONSTRAINT uq_evidence_index_scope UNIQUE (document_upload_id, artifact_type, artifact_content_sha256, index_version),
    CONSTRAINT uq_evidence_index_id_task UNIQUE (id, audit_task_id),
    CONSTRAINT fk_evidence_index_upload_task FOREIGN KEY (document_upload_id, audit_task_id)
        REFERENCES document_upload (id, audit_task_id),
    CONSTRAINT fk_evidence_index_artifact_task FOREIGN KEY (artifact_id, audit_task_id)
        REFERENCES stored_artifact (id, audit_task_id),
    CONSTRAINT chk_evidence_index_chunk_count CHECK (chunk_count >= 0),
    KEY idx_evidence_index_upload_status (document_upload_id, status),
    KEY idx_evidence_index_task_status (audit_task_id, status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- 可检索分块：原文、真实位置与向量。没有页码时记录原文行/字符区间并标注缺失。
CREATE TABLE IF NOT EXISTS evidence_chunk (
    id BIGINT NOT NULL AUTO_INCREMENT,
    audit_task_id BIGINT NOT NULL,
    index_version_id BIGINT NOT NULL,
    chunk_no INT NOT NULL,
    chunk_text MEDIUMTEXT NOT NULL,
    -- 上下文是否由派生规则补全（例如补齐表头）：派生上下文不得伪装成连续原文。
    context_derived TINYINT(1) NOT NULL DEFAULT 0,
    heading_path VARCHAR(500) NULL,
    char_start INT NOT NULL,
    char_end INT NOT NULL,
    -- PROVIDER=供应商返回的真实页号；SYNTHETIC_SEQUENCE=按结果项顺序的合成序号；ABSENT=没有页码。
    page_no INT NULL,
    page_no_source VARCHAR(30) NOT NULL DEFAULT 'ABSENT',
    -- 印章条目：保留它在原 JSON 中的条目序号，可以回到原始条目。
    source_entry_no INT NULL,
    -- 向量以十进制文本的 JSON 数组保存：不引入新的二进制依赖，也不做有损压缩。
    embedding_json JSON NOT NULL,
    embedding_norm VARCHAR(80) NOT NULL,
    token_count INT NOT NULL DEFAULT 0,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
    PRIMARY KEY (id),
    CONSTRAINT uq_evidence_chunk_no UNIQUE (index_version_id, chunk_no),
    CONSTRAINT uq_evidence_chunk_id_task UNIQUE (id, audit_task_id),
    CONSTRAINT fk_evidence_chunk_index_task FOREIGN KEY (index_version_id, audit_task_id)
        REFERENCES evidence_index_version (id, audit_task_id),
    CONSTRAINT chk_evidence_chunk_no CHECK (chunk_no >= 1),
    CONSTRAINT chk_evidence_chunk_offset CHECK (char_start >= 0 AND char_end >= char_start),
    KEY idx_evidence_chunk_index (index_version_id, chunk_no)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- 每次任务内检索的完整记录：查询内容、限定范围、模型/索引版本、候选与采用结果。
CREATE TABLE IF NOT EXISTS evidence_retrieval (
    id BIGINT NOT NULL AUTO_INCREMENT,
    retrieval_no VARCHAR(40) NOT NULL,
    audit_task_id BIGINT NOT NULL,
    scenario_version_id BIGINT NOT NULL,
    rule_key VARCHAR(40) NULL,
    node_code VARCHAR(40) NULL,
    query_text TEXT NOT NULL,
    scope_json JSON NOT NULL,
    model_id VARCHAR(160) NOT NULL,
    model_revision VARCHAR(80) NOT NULL,
    embedding_dim INT NULL,
    chunk_version VARCHAR(80) NULL,
    index_version VARCHAR(80) NULL,
    index_versions JSON NOT NULL,
    -- 候选结果；人工结论单独保存，不写回候选，也不把前几条自动标为已采纳。
    candidates_json JSON NOT NULL,
    -- 本次实际送入后续步骤的正文条目（上下文预算），不是人工结论。
    adopted_json JSON NOT NULL,
    -- 人工结论：CONSISTENT / INCONSISTENT / EVIDENCE_INSUFFICIENT。
    human_decision VARCHAR(40) NULL,
    human_decision_note TEXT NULL,
    human_selected JSON NULL,
    human_decided_at DATETIME(6) NULL,
    human_decided_by VARCHAR(120) NULL,
    -- 人工结论对应的任务输入修订：修订变化后旧结论不得套用到新的候选上。
    decision_input_revision INT NULL,
    top_k INT NOT NULL,
    status VARCHAR(30) NOT NULL,
    message TEXT NULL,
    input_revision INT NOT NULL DEFAULT 0,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
    PRIMARY KEY (id),
    CONSTRAINT uq_evidence_retrieval_no UNIQUE (retrieval_no),
    CONSTRAINT uq_evidence_retrieval_id_task UNIQUE (id, audit_task_id),
    CONSTRAINT fk_evidence_retrieval_task FOREIGN KEY (audit_task_id, scenario_version_id)
        REFERENCES audit_task (id, scenario_version_id),
    KEY idx_evidence_retrieval_task (audit_task_id, created_at),
    KEY idx_evidence_retrieval_rule (audit_task_id, rule_key)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;


-- ---------------------------------------------------------------------------
-- 10. 执行结果确认
--
-- 预期：不返回任何行。返回的每一行表示该表没有被创建出来。
-- 本文件使用 CREATE TABLE IF NOT EXISTS，已存在的表会被跳过，
-- 所以重复执行时这里同样只反映"缺哪些表"。
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
SELECT 'import_sheet'
    UNION ALL
SELECT 'import_issue'
    UNION ALL
SELECT 'field_candidate'
    UNION ALL
SELECT 'scenario_version'
    UNION ALL
SELECT 'scenario_material'
    UNION ALL
SELECT 'scenario_node'
    UNION ALL
SELECT 'scenario_rule'
    UNION ALL
SELECT 'scenario_draft'
    UNION ALL
SELECT 'scenario_draft_node'
    UNION ALL
SELECT 'scenario_draft_sheet'
    UNION ALL
SELECT 'scenario_draft_material'
    UNION ALL
SELECT 'scenario_draft_field'
    UNION ALL
SELECT 'scenario_draft_rule'
    UNION ALL
SELECT 'scenario_draft_section'
    UNION ALL
SELECT 'audit_plan'
    UNION ALL
SELECT 'plan_material'
    UNION ALL
SELECT 'plan_field'
    UNION ALL
SELECT 'plan_rule'
    UNION ALL
SELECT 'plan_section'
    UNION ALL
SELECT 'audit_task'
    UNION ALL
SELECT 'workflow_checkpoint'
    UNION ALL
SELECT 'audit_report'
    UNION ALL
SELECT 'operation_log'
    UNION ALL
SELECT 'review_draft'
    UNION ALL
SELECT 'evidence_index_version'
    UNION ALL
SELECT 'evidence_chunk'
    UNION ALL
SELECT 'evidence_retrieval'
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
    UNION ALL
SELECT 'agent_tool_call'
    UNION ALL
SELECT 'evaluation_snapshot'
    UNION ALL
SELECT 'checkpoint_migrations'
    UNION ALL
SELECT 'checkpoints'
    UNION ALL
SELECT 'checkpoint_blobs'
    UNION ALL
SELECT 'checkpoint_writes'
) AS expected
LEFT JOIN information_schema.tables AS actual
       ON actual.table_schema = DATABASE()
      AND actual.table_name = expected.table_name
WHERE actual.table_name IS NULL
ORDER BY expected.table_name;
