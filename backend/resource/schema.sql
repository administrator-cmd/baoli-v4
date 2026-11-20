-- ============================================================================
-- baoli-v4 数据库结构（当前版本，对应 Alembic head 0002_schema_cleanup）
--
-- 前置条件：MySQL >= 8.0.16（CHECK、生成列、JSON 列）。
-- 用法：空库执行本文件一次，再执行 alembic stamp head 登记版本。
-- 已有数据库按 Alembic 版本执行 upgrade head，不要重建。
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
CREATE TABLE `scenario_template` (
  `id` bigint NOT NULL AUTO_INCREMENT,
  `scenario_code` varchar(80) NOT NULL,
  `scenario_name` varchar(255) NOT NULL,
  `schema_key` varchar(80) NOT NULL, -- 数据结构标识
  `schema_version` varchar(40) NOT NULL, -- 数据结构版本
  `schema_json` json NOT NULL, -- 对应的数据结构定义
  `category` varchar(80) DEFAULT NULL,
  `description` text,
  `enabled` tinyint(1) NOT NULL DEFAULT '1',
  `latest_version_no` int NOT NULL DEFAULT '0', -- 已分配的最新场景版本号
  `created_at` datetime(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  `updated_at` datetime(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_scenario_template_code` (`scenario_code`),
  CONSTRAINT `chk_scenario_template_enabled` CHECK ((`enabled` in (0,1))),
  CONSTRAINT `chk_scenario_template_version` CHECK ((`latest_version_no` >= 0))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- 后台长操作：先认领再执行，接口只返回受理状态。
CREATE TABLE `background_operation` (
  `id` bigint NOT NULL AUTO_INCREMENT,
  `operation_no` varchar(40) NOT NULL,
  `object_type` varchar(30) NOT NULL, -- 操作对象类型
  `object_no` varchar(40) NOT NULL, -- 操作对象编号
  `operation_type` varchar(40) NOT NULL, -- 操作类型
  `idempotency_key` varchar(120) NOT NULL, -- 请求幂等标识
  `target_node` varchar(40) DEFAULT NULL, -- 目标执行节点
  `input_version` varchar(120) DEFAULT NULL, -- 请求对应的输入版本
  `request_hash` char(64) DEFAULT NULL, -- 请求内容摘要
  `phase` varchar(40) DEFAULT NULL, -- 后台操作当前阶段
  `external_job_ref` varchar(120) DEFAULT NULL, -- 外部服务作业编号
  `status` varchar(30) NOT NULL, -- 操作状态
  `request_json` json NOT NULL, -- 原始请求参数
  `result_json` json DEFAULT NULL, -- 操作结果摘要
  `failure_code` varchar(80) DEFAULT NULL,
  `failure_message` text,
  `started_at` datetime(6) DEFAULT NULL,
  `finished_at` datetime(6) DEFAULT NULL,
  `created_at` datetime(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  `updated_at` datetime(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_background_operation_no` (`operation_no`),
  UNIQUE KEY `uq_background_operation_idempotency` (`object_type`,`object_no`,`operation_type`,`idempotency_key`),
  KEY `idx_background_operation_object` (`object_type`,`object_no`,`status`),
  KEY `idx_background_operation_status` (`status`,`created_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE `operation_llm_response` (
  `id` bigint NOT NULL AUTO_INCREMENT,
  `operation_id` bigint NOT NULL,
  `stage` varchar(40) NOT NULL,
  `relative_path` varchar(500) NOT NULL,
  `created_at` datetime(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_llm_response_path` (`relative_path`),
  KEY `idx_llm_response_operation` (`operation_id`,`id`),
  CONSTRAINT `fk_llm_response_operation` FOREIGN KEY (`operation_id`) REFERENCES `background_operation` (`id`) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;


-- 2. 规则库导入

-- 规则库导入作业：源文件、协议身份与两轮解析结果。
CREATE TABLE `import_job` (
  `id` bigint NOT NULL AUTO_INCREMENT,
  `job_no` varchar(40) NOT NULL,
  `source_filename` varchar(255) NOT NULL,
  `source_path` varchar(500) NOT NULL, -- 上传文件存储路径
  `source_sha256` char(64) NOT NULL, -- 上传文件 SHA-256
  `source_size` bigint NOT NULL, -- 上传文件大小（字节）
  `profile_version` varchar(40) NOT NULL, -- 导入解析配置版本
  `schema_key` varchar(80) NOT NULL,
  `schema_version` varchar(40) NOT NULL,
  `schema_json` json NOT NULL,
  `scenario_code` varchar(80) DEFAULT NULL,
  `status` varchar(40) NOT NULL,
  `proposals_json` json DEFAULT NULL, -- 待确认的导入提案
  `draft_revision` int NOT NULL DEFAULT '0', -- 当前草稿修订号
  `failure_code` varchar(80) DEFAULT NULL,
  `failure_message` text,
  `created_at` datetime(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  `updated_at` datetime(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_import_job_job_no` (`job_no`),
  KEY `idx_import_job_status_created_at` (`status`,`created_at`),
  KEY `idx_import_job_created_at_id` (`created_at`,`id`),
  KEY `idx_import_job_scenario` (`scenario_code`),
  CONSTRAINT `fk_import_job_template` FOREIGN KEY (`scenario_code`) REFERENCES `scenario_template` (`scenario_code`) ON DELETE RESTRICT ON UPDATE RESTRICT,
  CONSTRAINT `chk_import_job_source_size` CHECK ((`source_size` >= 0))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;


-- 3. 已发布场景版本

-- 已发布场景版本；发布后不可覆盖，场景删除后仍保留。
CREATE TABLE `scenario_version` (
  `id` bigint NOT NULL AUTO_INCREMENT,
  `import_job_id` bigint DEFAULT NULL,
  `scenario_code` varchar(80) NOT NULL,
  `scenario_name` varchar(255) NOT NULL,
  `schema_key` varchar(80) NOT NULL,
  `schema_version` varchar(40) NOT NULL,
  `schema_json` json NOT NULL,
  `version_no` int NOT NULL, -- 场景版本号
  `source` varchar(20) NOT NULL DEFAULT 'IMPORT', -- 版本来源
  `base_version_id` bigint DEFAULT NULL, -- 派生自哪个版本
  `change_summary` varchar(500) DEFAULT NULL, -- 本次版本变更说明
  `manifest_json` json NOT NULL, -- 发布后的完整场景配置
  `published_at` datetime(6) NOT NULL,
  `created_at` datetime(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  `updated_at` datetime(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_scenario_version_code_no` (`scenario_code`,`version_no`),
  UNIQUE KEY `uq_scenario_version_import_job` (`import_job_id`),
  KEY `idx_scenario_version_base` (`base_version_id`),
  KEY `idx_scenario_version_code_published` (`scenario_code`,`published_at`),
  CONSTRAINT `fk_scenario_version_base` FOREIGN KEY (`base_version_id`) REFERENCES `scenario_version` (`id`) ON DELETE RESTRICT ON UPDATE RESTRICT,
  CONSTRAINT `fk_scenario_version_import_job` FOREIGN KEY (`import_job_id`) REFERENCES `import_job` (`id`) ON DELETE RESTRICT ON UPDATE RESTRICT,
  CONSTRAINT `chk_scenario_version_no` CHECK ((`version_no` >= 1))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- 执行容器节点；audit_task.current_node 通过复合外键指向这里。
CREATE TABLE `scenario_node` (
  `id` bigint NOT NULL AUTO_INCREMENT,
  `scenario_version_id` bigint NOT NULL,
  `node_code` varchar(80) NOT NULL,
  `node_type` varchar(40) NOT NULL,
  `name` varchar(120) NOT NULL,
  `sequence_no` int NOT NULL,
  `depends_on` json NOT NULL, -- 前置节点编号列表
  `config_json` json DEFAULT NULL, -- 节点专属配置
  `enabled` tinyint(1) NOT NULL,
  `created_at` datetime(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  `updated_at` datetime(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_scenario_node_version_code` (`scenario_version_id`,`node_code`),
  UNIQUE KEY `uq_scenario_node_version_sequence` (`scenario_version_id`,`sequence_no`),
  CONSTRAINT `fk_scenario_node_version` FOREIGN KEY (`scenario_version_id`) REFERENCES `scenario_version` (`id`) ON DELETE RESTRICT ON UPDATE RESTRICT,
  CONSTRAINT `chk_scenario_node_enabled` CHECK ((`enabled` in (0,1))),
  CONSTRAINT `chk_scenario_node_sequence` CHECK ((`sequence_no` >= 1))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;


-- 4. 场景草稿、方案、操作日志

-- 场景配置草稿；config_json 是节点、材料、字段、规则与报告配置的唯一存储。
CREATE TABLE `scenario_draft` (
  `id` bigint NOT NULL AUTO_INCREMENT,
  `scenario_code` varchar(80) NOT NULL,
  `schema_key` varchar(80) NOT NULL,
  `schema_version` varchar(40) NOT NULL,
  `schema_json` json NOT NULL,
  `revision_no` int NOT NULL DEFAULT '0', -- 草稿修订号
  `source` varchar(20) NOT NULL DEFAULT 'FORM', -- 草稿来源
  `base_version_no` int DEFAULT NULL, -- 草稿基于的已发布版本
  `profile_version` varchar(40) DEFAULT NULL, -- 导入配置版本
  `sync_error` json DEFAULT NULL, -- 与外部规则库同步错误
  `config_json` json NOT NULL, -- 可编辑的场景配置
  `created_at` datetime(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  `updated_at` datetime(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_scenario_draft_code` (`scenario_code`),
  CONSTRAINT `fk_scenario_draft_template` FOREIGN KEY (`scenario_code`) REFERENCES `scenario_template` (`scenario_code`) ON DELETE RESTRICT ON UPDATE RESTRICT,
  CONSTRAINT `chk_scenario_draft_base` CHECK (((`base_version_no` is null) or (`base_version_no` >= 1))),
  CONSTRAINT `chk_scenario_draft_revision` CHECK ((`revision_no` >= 0))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- 方案：场景版本的编辑副本；manifest_json 是 config_json 的编译结果。
CREATE TABLE `audit_plan` (
  `id` bigint NOT NULL AUTO_INCREMENT,
  `plan_code` varchar(80) NOT NULL,
  `plan_name` varchar(255) NOT NULL,
  `scenario_code` varchar(80) NOT NULL,
  `scenario_version_id` bigint DEFAULT NULL,
  `description` text,
  `enabled` tinyint(1) NOT NULL DEFAULT '1',
  `deleted_at` datetime(6) DEFAULT NULL,
  `config_json` json NOT NULL, -- 可编辑的方案配置
  `manifest_json` json DEFAULT NULL, -- 编译后的执行配置
  `sync_error` json DEFAULT NULL, -- 配置同步错误
  `scope_hash` char(64) DEFAULT NULL, -- 执行范围指纹
  `created_at` datetime(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  `updated_at` datetime(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_audit_plan_code` (`plan_code`),
  KEY `fk_audit_plan_scenario_version` (`scenario_version_id`),
  KEY `idx_audit_plan_scenario` (`scenario_code`,`deleted_at`),
  KEY `idx_audit_plan_deleted` (`deleted_at`),
  CONSTRAINT `fk_audit_plan_scenario_version` FOREIGN KEY (`scenario_version_id`) REFERENCES `scenario_version` (`id`) ON DELETE RESTRICT ON UPDATE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- 已提交的同步业务写操作；后台长操作见 background_operation。
CREATE TABLE `operation_log` (
  `id` bigint NOT NULL AUTO_INCREMENT,
  `log_no` varchar(40) NOT NULL,
  `object_type` varchar(30) NOT NULL, -- 操作对象类型
  `object_no` varchar(40) NOT NULL, -- 操作对象编号
  `object_name` varchar(255) NOT NULL, -- 操作对象名称快照
  `operation_type` varchar(40) NOT NULL,
  `result` varchar(20) NOT NULL DEFAULT 'SUCCEEDED',
  `idempotency_key` varchar(120) DEFAULT NULL, -- 请求幂等标识
  `request_hash` char(64) DEFAULT NULL, -- 请求内容摘要
  `duration_ms` int DEFAULT NULL, -- 操作耗时（毫秒）
  `command_no` varchar(40) DEFAULT NULL, -- 对应的后台命令编号
  `permanent` tinyint(1) NOT NULL DEFAULT '0', -- 是否为永久删除
  `detail_json` json DEFAULT NULL, -- 操作详情
  `failure_code` varchar(80) DEFAULT NULL,
  `failure_message` text,
  `created_at` datetime(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  `updated_at` datetime(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_operation_log_no` (`log_no`),
  UNIQUE KEY `uq_operation_log_idempotency` (`object_type`,`operation_type`,`idempotency_key`),
  KEY `idx_operation_log_object` (`object_type`,`object_no`,`created_at`),
  KEY `idx_operation_log_created` (`created_at`),
  KEY `idx_operation_log_delete` (`object_type`,`object_no`,`operation_type`,`permanent`),
  CONSTRAINT `chk_operation_log_permanent` CHECK ((`permanent` in (0,1)))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;


-- 5. 审核任务、执行批次与报告

-- 审核任务：冻结的执行配置、当前节点与输入修订号。
CREATE TABLE `audit_task` (
  `id` bigint NOT NULL AUTO_INCREMENT,
  `task_no` varchar(40) NOT NULL,
  `scenario_version_id` bigint NOT NULL,
  `plan_id` bigint DEFAULT NULL,
  `task_name` varchar(255) NOT NULL,
  `remark` text,
  `status` varchar(40) NOT NULL, -- 任务状态
  `current_node` varchar(80) NOT NULL, -- 当前执行节点编号
  `manifest_json` json NOT NULL, -- 创建任务时冻结的执行配置
  `task_schema_json` json NOT NULL, -- 任务启用范围的结构摘要
  `value_file_json` json DEFAULT NULL, -- 任务取值 CSV 文件索引
  `input_revision` int NOT NULL DEFAULT '0', -- 输入材料修订号
  `created_at` datetime(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  `updated_at` datetime(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_audit_task_task_no` (`task_no`),
  UNIQUE KEY `uq_audit_task_id_scenario_version` (`id`,`scenario_version_id`),
  KEY `fk_audit_task_current_node` (`scenario_version_id`,`current_node`),
  KEY `idx_audit_task_version_status` (`scenario_version_id`,`status`),
  KEY `idx_audit_task_plan` (`plan_id`),
  CONSTRAINT `fk_audit_task_current_node` FOREIGN KEY (`scenario_version_id`, `current_node`) REFERENCES `scenario_node` (`scenario_version_id`, `node_code`) ON DELETE RESTRICT ON UPDATE RESTRICT,
  CONSTRAINT `fk_audit_task_plan` FOREIGN KEY (`plan_id`) REFERENCES `audit_plan` (`id`) ON DELETE RESTRICT ON UPDATE RESTRICT,
  CONSTRAINT `fk_audit_task_scenario_version` FOREIGN KEY (`scenario_version_id`) REFERENCES `scenario_version` (`id`) ON DELETE RESTRICT ON UPDATE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- 节点执行批次；result_json 只存批次状态与进度汇总。
CREATE TABLE `node_execution` (
  `id` bigint NOT NULL AUTO_INCREMENT,
  `audit_task_id` bigint NOT NULL,
  `scenario_version_id` bigint NOT NULL,
  `node_code` varchar(80) NOT NULL,
  `sequence_no` int NOT NULL, -- 任务内执行批次序号
  `status` varchar(30) NOT NULL, -- 本批次执行状态
  `started_at` datetime(6) DEFAULT NULL,
  `finished_at` datetime(6) DEFAULT NULL,
  `result_json` json DEFAULT NULL, -- 执行进度与结果摘要
  `failure_code` varchar(80) DEFAULT NULL,
  `failure_message` text,
  `invalidated_from` varchar(30) DEFAULT NULL, -- 使本批次失效的原因类型
  `invalidated_at` datetime(6) DEFAULT NULL,
  `invalidation_reason` varchar(200) DEFAULT NULL, -- 失效原因说明
  `created_at` datetime(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  `updated_at` datetime(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_node_execution_task_sequence` (`audit_task_id`,`sequence_no`),
  UNIQUE KEY `uq_node_execution_id_task` (`id`,`audit_task_id`),
  KEY `fk_node_execution_task_version` (`audit_task_id`,`scenario_version_id`),
  KEY `fk_node_execution_version_node` (`scenario_version_id`,`node_code`),
  KEY `idx_node_execution_task_node` (`audit_task_id`,`node_code`),
  KEY `idx_node_execution_task_status` (`audit_task_id`,`status`),
  KEY `idx_node_execution_display_status` (`status`,`invalidated_from`),
  CONSTRAINT `fk_node_execution_task_version` FOREIGN KEY (`audit_task_id`, `scenario_version_id`) REFERENCES `audit_task` (`id`, `scenario_version_id`) ON DELETE RESTRICT ON UPDATE RESTRICT,
  CONSTRAINT `fk_node_execution_version_node` FOREIGN KEY (`scenario_version_id`, `node_code`) REFERENCES `scenario_node` (`scenario_version_id`, `node_code`) ON DELETE RESTRICT ON UPDATE RESTRICT,
  CONSTRAINT `chk_node_execution_sequence` CHECK ((`sequence_no` >= 1))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- 报告快照；重新生成是新快照，历史保留。
CREATE TABLE `audit_report` (
  `id` bigint NOT NULL AUTO_INCREMENT,
  `report_no` varchar(40) NOT NULL,
  `audit_task_id` bigint NOT NULL,
  `node_execution_id` bigint DEFAULT NULL,
  `report_name` varchar(255) NOT NULL,
  `content_type` varchar(60) NOT NULL DEFAULT 'text/markdown', -- 正文格式
  `content_md` mediumtext, -- 报告正文
  `scope_execution_complete` tinyint(1) NOT NULL DEFAULT '0', -- 生成时执行范围是否完整
  `risk_conclusion_available` tinyint(1) NOT NULL DEFAULT '0', -- 生成时风险结论是否可判定
  `reasons_json` json DEFAULT NULL, -- 未完成原因与格式支持信息
  `snapshot_json` json DEFAULT NULL, -- 生成时所依据的结果范围快照
  `input_revision` int DEFAULT NULL, -- 生成报告时的输入修订号
  `remark` text,
  `archived_at` datetime(6) DEFAULT NULL, -- 非空表示报告已归档
  `generated_at` datetime(6) NOT NULL,
  `created_at` datetime(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  `updated_at` datetime(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_audit_report_no` (`report_no`),
  KEY `idx_audit_report_task_generated` (`audit_task_id`,`generated_at`),
  KEY `idx_audit_report_generated` (`generated_at`),
  KEY `idx_audit_report_execution` (`node_execution_id`,`audit_task_id`),
  CONSTRAINT `fk_audit_report_execution_task` FOREIGN KEY (`node_execution_id`, `audit_task_id`) REFERENCES `node_execution` (`id`, `audit_task_id`) ON DELETE RESTRICT ON UPDATE RESTRICT,
  CONSTRAINT `fk_audit_report_task` FOREIGN KEY (`audit_task_id`) REFERENCES `audit_task` (`id`) ON DELETE RESTRICT ON UPDATE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- 可复用报告模板：原始 PDF 与模型提炼的结构/表达风格 JSON。
CREATE TABLE `report_template` (
  `id` bigint NOT NULL AUTO_INCREMENT,
  `template_no` varchar(40) NOT NULL,
  `upload_idempotency_key` varchar(120) DEFAULT NULL, -- 上传请求幂等标识
  `name` varchar(255) NOT NULL,
  `source_filename` varchar(255) NOT NULL,
  `source_path` varchar(500) NOT NULL,
  `source_size` bigint NOT NULL,
  `source_sha256` varchar(64) NOT NULL,
  `page_count` bigint NOT NULL DEFAULT '0',
  `status` varchar(24) NOT NULL DEFAULT 'PROCESSING',
  `style_profile_json` json DEFAULT NULL, -- 提取出的报告结构与风格
  `analysis_error` text,
  `created_at` datetime(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  `updated_at` datetime(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_report_template_no` (`template_no`),
  UNIQUE KEY `uq_report_template_upload_idempotency_key` (`upload_idempotency_key`),
  KEY `idx_report_template_status_updated` (`status`,`updated_at`),
  CONSTRAINT `chk_report_template_status` CHECK ((`status` in ('PROCESSING','READY','FAILED')))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;


-- 6. 材料位、上传与处理产物

-- 任务材料位；当前上传由 document_upload.is_current 唯一确定。
CREATE TABLE `task_document` (
  `id` bigint NOT NULL AUTO_INCREMENT,
  `document_no` varchar(40) NOT NULL,
  `audit_task_id` bigint NOT NULL,
  `material_key` varchar(40) NOT NULL,
  `material_name` varchar(255) NOT NULL,
  `required` tinyint(1) NOT NULL, -- 是否为必传材料
  `status` varchar(30) NOT NULL,
  `created_at` datetime(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  `updated_at` datetime(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_task_document_no` (`document_no`),
  UNIQUE KEY `uq_task_document_task_material` (`audit_task_id`,`material_key`),
  UNIQUE KEY `uq_task_document_id_task` (`id`,`audit_task_id`),
  CONSTRAINT `fk_task_document_task` FOREIGN KEY (`audit_task_id`) REFERENCES `audit_task` (`id`) ON DELETE RESTRICT ON UPDATE RESTRICT,
  CONSTRAINT `chk_task_document_required` CHECK ((`required` in (0,1)))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- 上传版本：追加写入，current_task_document_id 保证每个材料位只有一条当前记录。
CREATE TABLE `document_upload` (
  `id` bigint NOT NULL AUTO_INCREMENT,
  `upload_no` varchar(40) NOT NULL,
  `task_document_id` bigint NOT NULL,
  `audit_task_id` bigint NOT NULL,
  `original_filename` varchar(255) NOT NULL,
  `relative_path` varchar(500) NOT NULL, -- 文件相对存储路径
  `content_type` varchar(120) NOT NULL,
  `declared_material_type` varchar(80) NOT NULL, -- 上传时声明的材料类型
  `size_bytes` bigint NOT NULL, -- 文件大小（字节）
  `sha256` char(64) NOT NULL, -- 文件 SHA-256
  `status` varchar(30) NOT NULL, -- 上传版本状态
  `is_current` tinyint(1) NOT NULL, -- 是否为材料位当前版本
  `current_task_document_id` bigint GENERATED ALWAYS AS (if((`is_current` = 1),`task_document_id`,NULL)) STORED, -- 用于唯一约束当前版本
  `created_at` datetime(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  `updated_at` datetime(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_document_upload_document_no` (`task_document_id`,`upload_no`),
  UNIQUE KEY `uq_document_upload_id_document` (`id`,`task_document_id`),
  UNIQUE KEY `uq_document_upload_id_task` (`id`,`audit_task_id`),
  UNIQUE KEY `uq_document_upload_one_current` (`current_task_document_id`),
  KEY `fk_document_upload_task_document_task` (`task_document_id`,`audit_task_id`),
  KEY `idx_document_upload_document_current` (`task_document_id`,`is_current`),
  KEY `idx_document_upload_sha256` (`sha256`),
  CONSTRAINT `fk_document_upload_task_document_task` FOREIGN KEY (`task_document_id`, `audit_task_id`) REFERENCES `task_document` (`id`, `audit_task_id`) ON DELETE RESTRICT ON UPDATE RESTRICT,
  CONSTRAINT `chk_document_upload_current` CHECK ((`is_current` in (0,1))),
  CONSTRAINT `chk_document_upload_size` CHECK ((`size_bytes` >= 0))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- 外部处理产物索引；正文在 relative_path 指向的文件，不在数据库。
CREATE TABLE `stored_artifact` (
  `id` bigint NOT NULL AUTO_INCREMENT,
  `artifact_no` varchar(40) NOT NULL,
  `document_upload_id` bigint NOT NULL,
  `audit_task_id` bigint NOT NULL,
  `artifact_type` varchar(40) NOT NULL,
  `relative_path` varchar(500) NOT NULL, -- 产物正文相对路径
  `content_sha256` char(64) DEFAULT NULL, -- 产物内容摘要
  `status` varchar(30) NOT NULL, -- 外部处理状态
  `external_job_id` varchar(120) DEFAULT NULL, -- 外部服务作业编号
  `deadline_at` datetime(6) DEFAULT NULL, -- 外部作业查询截止时间
  `failure_code` varchar(80) DEFAULT NULL,
  `failure_message` text,
  `metadata_json` json DEFAULT NULL, -- 产物元数据
  `created_at` datetime(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  `updated_at` datetime(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_stored_artifact_no` (`artifact_no`),
  UNIQUE KEY `uq_stored_artifact_id_task` (`id`,`audit_task_id`),
  UNIQUE KEY `uq_stored_artifact_upload_type` (`document_upload_id`,`artifact_type`),
  KEY `fk_stored_artifact_upload_task` (`document_upload_id`,`audit_task_id`),
  KEY `idx_stored_artifact_upload_status` (`document_upload_id`,`status`),
  KEY `idx_stored_artifact_content` (`document_upload_id`,`artifact_type`,`content_sha256`),
  KEY `idx_stored_artifact_status` (`status`,`updated_at`),
  CONSTRAINT `fk_stored_artifact_upload_task` FOREIGN KEY (`document_upload_id`, `audit_task_id`) REFERENCES `document_upload` (`id`, `audit_task_id`) ON DELETE RESTRICT ON UPDATE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;


-- 7. 取值索引与规则结果

-- 字段候选版本索引；取值（原文、候选值、规范值）在任务 CSV。
CREATE TABLE `task_field_value` (
  `id` bigint NOT NULL AUTO_INCREMENT,
  `audit_task_id` bigint NOT NULL,
  `scenario_version_id` bigint NOT NULL,
  `field_key` varchar(40) NOT NULL,
  `material_key` varchar(40) NOT NULL,
  `document_upload_id` bigint NOT NULL,
  `revision_no` int NOT NULL DEFAULT '1', -- 字段候选修订号
  `is_current` tinyint(1) NOT NULL DEFAULT '1', -- 是否为当前候选
  `status` varchar(40) NOT NULL, -- 候选值状态
  `extraction_execution_id` bigint DEFAULT NULL, -- 产生候选的执行批次
  `origin` varchar(30) NOT NULL DEFAULT 'DOCUMENT_EXTRACTION', -- 候选来源
  `current_fact_scope` varchar(120) GENERATED ALWAYS AS (if((`is_current` = 1),concat(char_length(`field_key`),_utf8mb4':',`field_key`,char_length(`material_key`),_utf8mb4':',`material_key`),NULL)) VIRTUAL, -- 当前候选唯一键
  `created_at` datetime(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  `updated_at` datetime(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_task_field_value_revision` (`audit_task_id`,`field_key`,`material_key`,`document_upload_id`,`revision_no`),
  UNIQUE KEY `uq_task_field_value_id_task` (`id`,`audit_task_id`),
  UNIQUE KEY `uq_task_field_value_current` (`audit_task_id`,`current_fact_scope`),
  KEY `fk_task_field_value_upload_task` (`document_upload_id`,`audit_task_id`),
  KEY `fk_task_field_value_execution_task` (`extraction_execution_id`,`audit_task_id`),
  KEY `idx_task_field_lookup` (`audit_task_id`,`field_key`,`material_key`,`document_upload_id`),
  KEY `idx_task_field_version` (`audit_task_id`,`scenario_version_id`),
  KEY `idx_task_field_material` (`audit_task_id`,`material_key`),
  KEY `idx_task_field_current` (`audit_task_id`,`is_current`),
  CONSTRAINT `fk_task_field_value_execution_task` FOREIGN KEY (`extraction_execution_id`, `audit_task_id`) REFERENCES `node_execution` (`id`, `audit_task_id`) ON DELETE RESTRICT ON UPDATE RESTRICT,
  CONSTRAINT `fk_task_field_value_task_material` FOREIGN KEY (`audit_task_id`, `material_key`) REFERENCES `task_document` (`audit_task_id`, `material_key`) ON DELETE RESTRICT ON UPDATE RESTRICT,
  CONSTRAINT `fk_task_field_value_task_version` FOREIGN KEY (`audit_task_id`, `scenario_version_id`) REFERENCES `audit_task` (`id`, `scenario_version_id`) ON DELETE RESTRICT ON UPDATE RESTRICT,
  CONSTRAINT `fk_task_field_value_upload_task` FOREIGN KEY (`document_upload_id`, `audit_task_id`) REFERENCES `document_upload` (`id`, `audit_task_id`) ON DELETE RESTRICT ON UPDATE RESTRICT,
  CONSTRAINT `chk_task_field_value_current` CHECK ((`is_current` in (0,1))),
  CONSTRAINT `chk_task_field_value_revision` CHECK ((`revision_no` >= 1))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- 统一事实索引（字段抽取与计算结果）；取值在任务 CSV。
CREATE TABLE `task_fact` (
  `id` bigint NOT NULL AUTO_INCREMENT,
  `audit_task_id` bigint NOT NULL,
  `scenario_version_id` bigint NOT NULL,
  `fact_code` varchar(80) NOT NULL,
  `fact_kind` varchar(20) NOT NULL, -- 抽取事实或计算事实
  `material_key` varchar(40) DEFAULT NULL, -- 来源材料类型
  `entity_scope` varchar(20) NOT NULL DEFAULT 'SELF', -- 事实所属主体范围
  `period_scope` varchar(30) NOT NULL, -- 事实所属期间
  `statement_scope` varchar(20) NOT NULL, -- 事实所属报表范围
  `value_type` varchar(20) NOT NULL, -- 事实值类型
  `source_execution_id` bigint DEFAULT NULL, -- 产生事实的执行批次
  `calculation_rule_code` varchar(80) DEFAULT NULL, -- 派生事实所用规则编号
  `revision_no` int NOT NULL DEFAULT '1', -- 事实修订号
  `is_current` tinyint(1) NOT NULL DEFAULT '1', -- 是否为当前事实
  `status` varchar(40) NOT NULL, -- 事实状态
  `material_scope` varchar(40) GENERATED ALWAYS AS (ifnull(`material_key`,_utf8mb4'')) VIRTUAL, -- 空材料统一用于唯一键
  `current_fact_scope` varchar(255) GENERATED ALWAYS AS (if((`is_current` = 1),concat(char_length(`fact_code`),_utf8mb4':',`fact_code`,if((`material_key` is null),_utf8mb4'-1:',concat(char_length(`material_key`),_utf8mb4':',`material_key`)),char_length(`period_scope`),_utf8mb4':',`period_scope`,char_length(`statement_scope`),_utf8mb4':',`statement_scope`,char_length(`entity_scope`),_utf8mb4':',`entity_scope`),NULL)) VIRTUAL, -- 当前事实唯一键
  `created_at` datetime(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  `updated_at` datetime(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_task_fact_id_task` (`id`,`audit_task_id`),
  UNIQUE KEY `uq_task_fact_revision` (`audit_task_id`,`fact_code`,`material_scope`,`period_scope`,`statement_scope`,`entity_scope`,`revision_no`),
  UNIQUE KEY `uq_task_fact_current` (`audit_task_id`,`current_fact_scope`),
  KEY `fk_task_fact_task` (`audit_task_id`,`scenario_version_id`),
  KEY `fk_task_fact_execution_task` (`source_execution_id`,`audit_task_id`),
  KEY `idx_task_fact_lookup` (`audit_task_id`,`fact_code`,`period_scope`,`statement_scope`,`entity_scope`),
  KEY `idx_task_fact_kind` (`audit_task_id`,`fact_kind`,`is_current`),
  KEY `idx_task_fact_current` (`audit_task_id`,`is_current`),
  KEY `idx_task_fact_rule` (`audit_task_id`,`calculation_rule_code`),
  KEY `idx_task_fact_material` (`audit_task_id`,`material_key`),
  CONSTRAINT `fk_task_fact_execution_task` FOREIGN KEY (`source_execution_id`, `audit_task_id`) REFERENCES `node_execution` (`id`, `audit_task_id`) ON DELETE RESTRICT ON UPDATE RESTRICT,
  CONSTRAINT `fk_task_fact_material` FOREIGN KEY (`audit_task_id`, `material_key`) REFERENCES `task_document` (`audit_task_id`, `material_key`) ON DELETE RESTRICT ON UPDATE RESTRICT,
  CONSTRAINT `fk_task_fact_task` FOREIGN KEY (`audit_task_id`, `scenario_version_id`) REFERENCES `audit_task` (`id`, `scenario_version_id`) ON DELETE RESTRICT ON UPDATE RESTRICT,
  CONSTRAINT `chk_task_fact_current` CHECK ((`is_current` in (0,1))),
  CONSTRAINT `chk_task_fact_material` CHECK (((`material_key` is null) or (`material_key` <> _utf8mb4''))),
  CONSTRAINT `chk_task_fact_revision` CHECK ((`revision_no` >= 1))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- 规则结论索引；产出、依据、缺项在任务 CSV。
CREATE TABLE `rule_result` (
  `id` bigint NOT NULL AUTO_INCREMENT,
  `audit_task_id` bigint NOT NULL,
  `node_execution_id` bigint NOT NULL,
  `rule_key` varchar(80) NOT NULL, -- 规则稳定标识
  `rule_code` varchar(80) DEFAULT NULL,
  `rule_name` varchar(255) NOT NULL,
  `status` varchar(40) NOT NULL,
  `message` text NOT NULL,
  `risk_level` varchar(40) DEFAULT NULL,
  `rule_role` varchar(20) NOT NULL, -- 规则用途类别
  `loop_status` varchar(30) DEFAULT NULL, -- 规则循环处理状态
  `turn_count` int NOT NULL DEFAULT '0', -- 模型处理轮数
  `result_json` json DEFAULT NULL, -- 结论的结构化补充信息
  `created_at` datetime(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  `updated_at` datetime(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_rule_result_execution_rule` (`node_execution_id`,`rule_key`),
  UNIQUE KEY `uq_rule_result_id_task` (`id`,`audit_task_id`),
  KEY `fk_rule_result_execution_task` (`node_execution_id`,`audit_task_id`),
  KEY `idx_rule_result_task_status` (`audit_task_id`,`status`),
  KEY `idx_rule_result_task_rule` (`audit_task_id`,`rule_key`),
  KEY `idx_rule_result_task_code` (`audit_task_id`,`rule_code`),
  KEY `idx_rule_result_task_risk_level` (`audit_task_id`,`risk_level`),
  KEY `idx_rule_result_role_task` (`rule_role`,`audit_task_id`),
  CONSTRAINT `fk_rule_result_execution_task` FOREIGN KEY (`node_execution_id`, `audit_task_id`) REFERENCES `node_execution` (`id`, `audit_task_id`) ON DELETE RESTRICT ON UPDATE RESTRICT,
  CONSTRAINT `fk_rule_result_task` FOREIGN KEY (`audit_task_id`) REFERENCES `audit_task` (`id`) ON DELETE RESTRICT ON UPDATE RESTRICT,
  CONSTRAINT `chk_rule_result_turn_count` CHECK ((`turn_count` >= 0))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- 字段人工裁决记录；system_judgment_json 是提交时的不可变快照。
CREATE TABLE `manual_review` (
  `id` bigint NOT NULL AUTO_INCREMENT,
  `review_no` varchar(40) NOT NULL,
  `audit_task_id` bigint NOT NULL,
  `field_value_id` bigint NOT NULL,
  `status` varchar(30) NOT NULL,
  `system_judgment_json` json NOT NULL, -- 人工提交时的系统判断快照
  `decision` varchar(40) DEFAULT NULL, -- 人工裁决类型
  `business_note` text,
  `before_value` text, -- 裁决前取值
  `after_value` text, -- 裁决后取值
  `basis_type` varchar(30) DEFAULT NULL, -- 裁决依据类型
  `decision_command_no` varchar(40) DEFAULT NULL, -- 提交裁决的操作编号
  `created_at` datetime(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  `updated_at` datetime(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_manual_review_no` (`review_no`),
  KEY `fk_manual_review_field_value_task` (`field_value_id`,`audit_task_id`),
  KEY `idx_manual_review_task_status` (`audit_task_id`,`status`),
  KEY `idx_manual_review_field_value` (`field_value_id`),
  CONSTRAINT `fk_manual_review_field_value_task` FOREIGN KEY (`field_value_id`, `audit_task_id`) REFERENCES `task_field_value` (`id`, `audit_task_id`) ON DELETE RESTRICT ON UPDATE RESTRICT,
  CONSTRAINT `fk_manual_review_task` FOREIGN KEY (`audit_task_id`) REFERENCES `audit_task` (`id`) ON DELETE RESTRICT ON UPDATE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- 字段核对确认：绑定执行批次、任务输入修订与字段版本摘要；每次确认新增一行，最新一行代表当前输入。
CREATE TABLE `field_review_confirmation` (
  `id` bigint NOT NULL AUTO_INCREMENT,
  `confirmation_no` varchar(40) NOT NULL,
  `audit_task_id` bigint NOT NULL,
  `node_execution_id` bigint NOT NULL,
  `input_revision` int NOT NULL, -- 确认时的任务输入修订号
  `field_digest` char(64) NOT NULL, -- 字段结果集合摘要
  `missing_field_count` int NOT NULL DEFAULT 0, -- 缺失字段数量
  `missing_field_keys` json DEFAULT NULL, -- 缺失字段编号列表
  `snapshot_json` json DEFAULT NULL, -- 确认时字段值与证据快照
  `note` text,
  `operation_no` varchar(40) DEFAULT NULL, -- 对应操作编号
  `created_at` datetime(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  `updated_at` datetime(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_field_review_no` (`confirmation_no`),
  KEY `idx_field_review_task` (`audit_task_id`,`node_execution_id`),
  KEY `idx_field_review_task_id` (`audit_task_id`,`id`),
  CONSTRAINT `fk_field_review_execution_task` FOREIGN KEY (`node_execution_id`, `audit_task_id`) REFERENCES `node_execution` (`id`, `audit_task_id`) ON DELETE RESTRICT ON UPDATE RESTRICT,
  CONSTRAINT `fk_field_review_task` FOREIGN KEY (`audit_task_id`) REFERENCES `audit_task` (`id`) ON DELETE RESTRICT ON UPDATE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;


-- AI 辅助生成规则库：生成作业、冻结导出与导入关联。与审核任务 CSV 无关。

-- 生成作业：原文件、冻结生成输入、当前可编辑文档；不保存可由文档派生的状态列。
CREATE TABLE `rulebook_generation_job` (
  `id` bigint NOT NULL AUTO_INCREMENT,
  `job_no` varchar(40) NOT NULL,
  `idempotency_key` varchar(120) NOT NULL, -- 创建请求幂等标识
  `request_hash` char(64) NOT NULL, -- 创建请求内容摘要
  `source_filename` varchar(255) NOT NULL,
  `source_path` varchar(500) NOT NULL, -- 原始文件存储路径
  `source_sha256` char(64) NOT NULL, -- 原始文件 SHA-256
  `source_size` bigint NOT NULL, -- 原始文件大小（字节）
  `business_background` text NOT NULL,
  `generation_requirements` text NOT NULL,
  `document_schema_version` varchar(40) NOT NULL, -- 生成文档结构版本
  `prompt_version` varchar(40) NOT NULL, -- 使用的提示词版本
  `document_json` json DEFAULT NULL, -- 当前可编辑的规则库文档
  `checkpoint_json` json DEFAULT NULL, -- 首轮编制私有断点，不用于导出或场景导入
  `revision` int NOT NULL DEFAULT 0, -- 当前文档修订号
  `created_at` datetime(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  `updated_at` datetime(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_rulebook_generation_job_no` (`job_no`),
  UNIQUE KEY `uq_rulebook_generation_job_idempotency` (`idempotency_key`),
  KEY `idx_rulebook_generation_created_at_id` (`created_at`,`id`),
  CONSTRAINT `ck_rulebook_generation_source_size` CHECK ((`source_size` >= 0))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- 冻结导出：同一修订只产生一份规范导出；快照不可变。
CREATE TABLE `rulebook_generation_export` (
  `id` bigint NOT NULL AUTO_INCREMENT,
  `export_no` varchar(40) NOT NULL,
  `generation_job_id` bigint NOT NULL,
  `revision` int NOT NULL, -- 导出对应的文档修订号
  `document_snapshot_json` json NOT NULL, -- 导出时的文档快照
  `export_format_version` varchar(40) NOT NULL, -- 导出文件格式版本
  `filename` varchar(255) NOT NULL,
  `relative_path` varchar(500) NOT NULL,
  `sha256` char(64) NOT NULL, -- 导出文件 SHA-256
  `size` bigint NOT NULL, -- 导出文件大小（字节）
  `created_at` datetime(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_rulebook_generation_export_no` (`export_no`),
  UNIQUE KEY `uq_rulebook_generation_export_revision` (`generation_job_id`,`revision`),
  KEY `idx_rulebook_generation_export_job` (`generation_job_id`,`created_at`),
  CONSTRAINT `ck_rulebook_generation_export_size` CHECK ((`size` >= 0)),
  CONSTRAINT `fk_rulebook_generation_export_job` FOREIGN KEY (`generation_job_id`) REFERENCES `rulebook_generation_job` (`id`) ON DELETE RESTRICT ON UPDATE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- 导出与真实导入作业的关系；导入被删除后只解除关联。
CREATE TABLE `rulebook_generation_import` (
  `id` bigint NOT NULL AUTO_INCREMENT,
  `generation_export_id` bigint NOT NULL,
  `import_job_id` bigint DEFAULT NULL,
  `idempotency_key` varchar(120) NOT NULL, -- 导入请求幂等标识
  `request_hash` char(64) NOT NULL, -- 导入请求内容摘要
  `created_at` datetime(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_rulebook_generation_import_request` (`generation_export_id`,`idempotency_key`),
  UNIQUE KEY `uq_rulebook_generation_import_job` (`import_job_id`),
  KEY `idx_rulebook_generation_import_export` (`generation_export_id`),
  CONSTRAINT `fk_rulebook_generation_import_export` FOREIGN KEY (`generation_export_id`) REFERENCES `rulebook_generation_export` (`id`) ON DELETE RESTRICT ON UPDATE RESTRICT,
  CONSTRAINT `fk_rulebook_generation_import_job` FOREIGN KEY (`import_job_id`) REFERENCES `import_job` (`id`) ON DELETE SET NULL ON UPDATE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;


-- 规则库修订为只追加快照；job.document_json 是唯一可编辑入口。
CREATE TABLE `rulebook_generation_revision` (
  `id` bigint NOT NULL AUTO_INCREMENT,
  `generation_job_id` bigint NOT NULL,
  `revision` int NOT NULL, -- 历史修订号
  `document_json` json NOT NULL, -- 本次修订的完整文档
  `summary` text NOT NULL, -- 本次修订说明
  `origin` varchar(16) NOT NULL, -- 修订来源
  `operation_id` bigint DEFAULT NULL, -- 触发本次修订的后台操作
  `created_at` datetime(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_generation_revision_job` (`generation_job_id`,`revision`),
  UNIQUE KEY `uq_generation_revision_operation` (`operation_id`),
  CONSTRAINT `fk_generation_revision_job` FOREIGN KEY (`generation_job_id`) REFERENCES `rulebook_generation_job` (`id`) ON DELETE RESTRICT,
  CONSTRAINT `fk_generation_revision_operation` FOREIGN KEY (`operation_id`) REFERENCES `background_operation` (`id`) ON DELETE RESTRICT,
  CONSTRAINT `ck_generation_revision_origin` CHECK ((`origin` in (_utf8mb4'INITIAL',_utf8mb4'MANUAL',_utf8mb4'AI',_utf8mb4'RESTORE',_utf8mb4'BASELINE'))),
  CONSTRAINT `ck_generation_revision_positive` CHECK ((`revision` >= 1))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE `rulebook_generation_message` (
  `id` bigint NOT NULL AUTO_INCREMENT,
  `generation_job_id` bigint NOT NULL,
  `revision` int NOT NULL, -- 对话对应的文档修订号
  `operation_id` bigint NOT NULL, -- 对应的后台操作
  `role` varchar(16) NOT NULL, -- 对话角色
  `content` text NOT NULL, -- 对话内容
  `created_at` datetime(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_generation_message_operation_role` (`operation_id`,`role`),
  KEY `fk_generation_message_revision` (`generation_job_id`,`revision`),
  KEY `idx_generation_message_job_id` (`generation_job_id`,`id`),
  CONSTRAINT `fk_generation_message_operation` FOREIGN KEY (`operation_id`) REFERENCES `background_operation` (`id`) ON DELETE RESTRICT,
  CONSTRAINT `fk_generation_message_revision` FOREIGN KEY (`generation_job_id`, `revision`) REFERENCES `rulebook_generation_revision` (`generation_job_id`, `revision`) ON DELETE RESTRICT,
  CONSTRAINT `ck_generation_message_role` CHECK ((`role` in (_utf8mb4'USER',_utf8mb4'ASSISTANT')))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- 8. 结构确认：预期不返回任何行。

SELECT expected.table_name AS missing_table
FROM (
SELECT 'scenario_template' AS table_name
    UNION ALL SELECT 'background_operation' AS table_name
    UNION ALL SELECT 'operation_llm_response' AS table_name
    UNION ALL SELECT 'import_job' AS table_name
    UNION ALL SELECT 'scenario_version' AS table_name
    UNION ALL SELECT 'scenario_node' AS table_name
    UNION ALL SELECT 'scenario_draft' AS table_name
    UNION ALL SELECT 'audit_plan' AS table_name
    UNION ALL SELECT 'operation_log' AS table_name
    UNION ALL SELECT 'audit_task' AS table_name
    UNION ALL SELECT 'node_execution' AS table_name
    UNION ALL SELECT 'audit_report' AS table_name
    UNION ALL SELECT 'report_template' AS table_name
    UNION ALL SELECT 'task_document' AS table_name
    UNION ALL SELECT 'document_upload' AS table_name
    UNION ALL SELECT 'stored_artifact' AS table_name
    UNION ALL SELECT 'task_field_value' AS table_name
    UNION ALL SELECT 'task_fact' AS table_name
    UNION ALL SELECT 'rule_result' AS table_name
    UNION ALL SELECT 'manual_review' AS table_name
    UNION ALL SELECT 'field_review_confirmation' AS table_name
    UNION ALL SELECT 'rulebook_generation_job' AS table_name
    UNION ALL SELECT 'rulebook_generation_export' AS table_name
    UNION ALL SELECT 'rulebook_generation_import' AS table_name
    UNION ALL SELECT 'rulebook_generation_revision' AS table_name
    UNION ALL SELECT 'rulebook_generation_message' AS table_name
) AS expected
LEFT JOIN information_schema.tables AS actual
       ON actual.table_schema = DATABASE()
      AND actual.table_name = expected.table_name
WHERE actual.table_name IS NULL
ORDER BY expected.table_name;


-- 9. 状态取值（取值以代码常量为准）
--
-- audit_task.status            READY / RUNNING / WAITING_FIELD_REVIEW / BLOCKED / FAILED / COMPLETED
-- background_operation.status  PENDING / RUNNING / SUCCEEDED / FAILED / INTERRUPTED
-- import_job.status            PARSED / PROPOSED / APPLIED / EXTERNAL_FAILED
-- task_document.status         WAITING_UPLOAD / UPLOADED / PREPARED
-- document_upload.status       UPLOADED / SUPERSEDED / REMOVED
-- stored_artifact.status       PROCESSING / SUBMITTING / PENDING / READY / FAILED / INVALIDATED
-- node_execution.status        RUNNING / WAITING_FIELD_REVIEW / SUCCEEDED / BLOCKED / FAILED / SKIPPED / INVALIDATED
-- task_field_value.origin      DOCUMENT_EXTRACTION / HUMAN_CORRECTION
-- task_field_value.status      PRESENT / NOT_PRESENT / EVIDENCE_INSUFFICIENT / REVIEWED
-- task_fact.fact_kind          EXTRACTED / DERIVED
-- rule_result.status           COMPUTED / PASSED / ABNORMAL / EVIDENCE_INSUFFICIENT /
--                              NOT_APPLICABLE / NOT_EXECUTED / PENDING_REVIEW / REVIEWED /
--                              SKIPPED / RUNNING
-- rule_result.loop_status      RUNNING / DONE / BLOCKED / NEEDS_HUMAN
-- audit_report.archived_at     非空表示已归档；报告行存在即表示正文已保存
-- report_template.status       PROCESSING / READY / FAILED
-- rulebook_generation_job.revision 0=尚未生成，1=首次生成完成，之后每次保存递增
-- manual_review.status         PENDING / RESOLVED / INVALIDATED
