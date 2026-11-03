/** 场景配置：草稿、材料、字段、规则、章节与发布。 */

import type { JsonValue, OperationAccepted } from "./common";

export interface RuleSnapshotEntry {
  rule_key: string;
  name: string;
}

export interface TaskScope {
  /** 本任务会跑的规则（创建任务时勾选的那些；系统不补规则）。 */
  enabled_rules: RuleSnapshotEntry[];
  /** 任务冻结配置中启用的计算规则。 */
  enabled_calculation_rules?: RuleSnapshotEntry[];
  /** 模板里有、本任务没选的规则。 */
  unenabled_rules: RuleSnapshotEntry[];
  enabled_rule_keys?: string[];
  unenabled_rule_keys?: string[];
}
export interface DocumentUploadResponse {
  document_no: string;
  material_name: string;
  filename: string;
  size_bytes: number;
  status: string;
  material_classify: Record<string, JsonValue> | null;
  /** 文件已经保存，自动校验通过此后台操作查询。 */
  classification_operation: OperationAccepted;
}

export interface ScenarioListItem {
  scenario_code: string;
  scenario_name: string;
  category: string | null;
  description: string | null;
  enabled: boolean;
  published_version_no: number | null;
  published_at: string | null;
  latest_version_no: number;
  material_count: number;
  field_count: number;
  rule_count: number;
  /** 数量与编辑页对应；有草稿时取草稿，否则取发布快照。 */
  configuration_kind: "DRAFT" | "PUBLISHED";
  draft_revision: number | null;
  draft_source: string | null;
  draft_base_version_no: number | null;
  /** 配置编译错误；null 不表示草稿已发布。 */
  sync_error: ConfigSyncError | null;
  updated_at: string;
}

export interface ConfigSyncError {
  code: string;
  message: string;
  details: Record<string, JsonValue>;
}

export interface ScenarioDraftInfo {
  schema_key?: string | null;
  schema_version?: string | null;
  schema_json?: Record<string, unknown> | null;
  revision_no: number;
  source: string;
  source_label: string;
  base_version_no: number | null;
  /** 草稿编译错误；发布状态由草稿是否存在与发布版本共同表达。 */
  sync_error: ConfigSyncError | null;
  updated_at: string;
}

export interface ScenarioDetail {
  scenario_code: string;
  scenario_name: string;
  schema_key?: string | null;
  schema_version?: string | null;
  schema_json?: Record<string, unknown> | null;
  category: string | null;
  description: string | null;
  enabled: boolean;
  latest_version_no: number;
  created_at: string;
  updated_at: string;
  plan_count: number;
  draft: ScenarioDraftInfo | null;
}

export interface DraftMaterialItem {
  material_key: string;
  /** 业务编号（Excel 的「材料编号」列）：导出规则库时写回该列。 */
  source_ref?: string | null;
  origin?: ConfigOrigin | null;
  name: string;
  required: boolean;
  description: string;
  sort_no?: number;
  updated_at?: string;
}

export interface DraftFieldItem {
  field_key: string;
  material_key: string;
  /** 内部技术容器身份，不是可编辑的业务阶段。 */
  node_code?: string | null;
  material_name: string | null;
  name: string;
  value_type: string;
  extraction_strategy: "RAW_TEXT" | "DETERMINISTIC" | "STRUCTURED";
  structure_schema: Record<string, unknown> | null;
  unit: string | null;
  /** 币种：金额比较与加总的口径之一；留空表示未声明，不表示人民币。 */
  currency?: string | null;
  /** 产物来源：当前仅印章识别 SEAL；留空表示字段由常规字段抽取提供。 */
  artifact_source?: string | null;
  /**
   * 取数期间与报表口径：由字段自己声明，计算参数按这两项定位字段事实
   * （参数表不再声明它们）。仅在业务实际涉及时填写，缺省由服务端归一为不适用。
   */
  period_scope?: string | null;
  statement_scope?: string | null;
  entity_scope?: string | null;
  required: boolean;
  instruction: string;
  /** 业务编号（Excel 的「字段编号」列）：导出时写回。 */
  source_ref?: string | null;
  origin?: ConfigOrigin | null;
  sort_no?: number;
  updated_at?: string;
}

export interface DraftRuleInputView {
  field_key: string | null;
  material_key: string | null;
  field_name: string | null;
  material_name: string | null;
  value_type?: string | null;
  unit?: string | null;
}

export interface ConfigOrigin {
  import_job_no?: string;
  sheet_name: string;
  row_no: number;
  related_rows: { sheet_name: string; row_no: number }[];
  rows: { sheet_name: string; row_no: number; cells: Record<string, JsonValue> }[];
}

export interface DraftRuleItem {
  rule_key: string;
  name: string;
  node: string;
  operator: string;
  /** 规则执行方式；旧配置没有该字段时为 UNKNOWN，发布前必须补填。 */
  execution_mode?: string | null;
  rule_role: "CHECK" | "RISK";
  enabled: boolean;
  /** 系统派生规则（字段抽取 / 报告生成）只读，不能独立删除。 */
  system: boolean;
  inputs: DraftRuleInputView[];
  materials: Array<{ material_key: string; material_name: string | null }>;
  outputs: Array<{ field_key: string; field_name: string | null; material_key: string | null }>;
  parameters: Record<string, JsonValue>;
  requirement: string;
  applicability: { kind: string; material_keys?: string[] };
  source_rule_key: string | null;
  source_rule_name: string | null;
  risk_level: string | null;
  risk_action: string | null;
  source_ref?: string | null;
  origin?: ConfigOrigin | null;
  /** 业务配置事实：检查等级、合格要求原文、风险等级原文、成立条件、多来源与配置状态。 */
  check_level?: string | null;
  check_operator?: string | null;
  risk_level_value?: string | null;
  risk_condition?: string | null;
  source_rule_keys?: string[];
  source_rule_codes?: string[];
  config_status?: string | null;
  updated_at?: string | null;
}

export interface DraftSectionItem {
  section_key: string;
  title: string;
  content_instruction: string;
  field_keys: string[];
  field_names: string[];
  data_refs: string[];
  data_names: string[];
  sort_no?: number;
  /** 报告的分组属性：报告编号、名称、材料范围、输出格式、章节顺序、内容类型、结果排序与配置状态。 */
  report_code?: string | null;
  report_name?: string | null;
  material_keys?: string[];
  material_names?: string[];
  formats?: string[];
  section_order?: number | null;
  content_type?: string | null;
  result_sort?: string | null;
  risk_level_order?: string[];
  config_status?: string | null;
  origin?: ConfigOrigin | null;
  updated_at?: string | null;
}

/**
 * 场景配置里的一个节点实例。
 *
 * 节点是执行记录的技术容器。业务执行顺序由生效规则的显式字段、规则和计算引用决定。
 */
export interface DraftNodeItem {
  node_code: string;
  node_type: string;
  node_type_label: string;
  name: string;
  sequence_no: number;
  /** 上游节点实例码列表。 */
  depends_on: string[];
  config_json: Record<string, JsonValue> | null;
  enabled: boolean;
  updated_at: string;
}

/** 当前计算配置与完整可编辑定义，来自 calculation_json，不是运行结果。 */
export interface DraftCalculationItem {
  rule_code: string | null;
  name: string | null;
  node_code: string | null;
  formula: string | null;
  requirement: string;
  business_context: Record<string, JsonValue>;
  result_code: string | null;
  unit: string | null;
  currency: string | null;
  status: string | null;
  origin?: ConfigOrigin | null;
  definition: Record<string, JsonValue>;
}

export interface ScenarioConfigurationView {
  scenario_code: string;
  configuration_kind: "DRAFT" | "PUBLISHED";
  version_no: number | null;
  revision_no: number | null;
  source: string;
  source_label: string;
  base_version_no: number | null;
  /** 规则执行契约的版本标识；页面新建时为 null。 */
  profile_version?: string | null;
  /** 配置编译错误；null 不表示草稿已发布。 */
  sync_error?: ConfigSyncError | null;
  updated_at: string;
  /** 执行记录技术容器，不供用户编排业务流程。 */
  nodes: DraftNodeItem[];
  calculations: DraftCalculationItem[];
  materials: DraftMaterialItem[];
  fields: DraftFieldItem[];
  rules: DraftRuleItem[];
  sections: DraftSectionItem[];
  business_rule_count: number;
  system_rule_count: number;
}
export interface ScenarioDraftView extends ScenarioConfigurationView {
  configuration_kind: "DRAFT";
  revision_no: number;
}
export interface ScenarioPublishResult {
  scenario_code: string;
  scenario_name: string;
  version_no: number;
  status: string;
  source: string;
  change_summary: string | null;
  material_count: number;
  field_count: number;
  rule_count: number;
  published_at: string;
  /** 内容与最新已发布版本完全一致时复用该版本，不制造重复版本。 */
  reused: boolean;
}
