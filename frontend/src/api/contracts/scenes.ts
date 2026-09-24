/**
 * 场景配置：场景版本、草稿、材料/字段/规则/章节、发布与版本对比。
 *
 * 契约按业务域拆分：本文件只声明本域的载荷形状，跨域引用一律显式 import，
 * 不再让所有页面共同依赖一个不断扩大的中心文件。
 */

import type { ApiResponse, JsonValue } from "./common";

export interface ScenarioSummary extends ApiResponse {
  scenario_code: string;
  scenario_name: string;
  latest_version_no: number;
  status: string;
  published_at: string;
}

export interface ScenarioManifest {
  scene_name: string;
  materials: Array<{
    material_key: string;
    name: string;
    required: boolean;
  }>;
  fields: Array<{
    field_key: string;
    name: string;
    material_key: string;
    required: boolean;
  }>;
  rules: Array<{
    rule_key: string;
    name: string;
    node: string;
    operator: string;
    enabled: boolean;
  }>;
}

export interface ScenarioVersion extends ApiResponse {
  scenario_code: string;
  scenario_name: string;
  version_no: number;
  status: string;
  manifest: ScenarioManifest;
  published_at: string;
}

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
  /** 未配置的检查类别（**能力类型**汇总）：用于「这类检查有没有配」。 */
  unconfigured_node_types: string[];
  /** 这些类别下真实存在的**节点实例码**：用于「当前这个节点是不是未配置的类别」。 */
  unconfigured_node_codes: string[];
  capability_gaps?: Array<{
    rule_key?: string;
    rule_name: string;
    node?: string;
    operator?: string;
    gap: string;
    message: string;
  }>;
}

export interface TaskSchemaPreview extends ApiResponse {
  enabled_rule_keys: string[];
  unenabled_rule_keys: string[];
  unconfigured_check_node_types: string[];
  unconfigured_check_node_codes: string[];
  scope: TaskScope;
  field_count: number;
}

export interface CreateTaskResponse extends ApiResponse {
  task_no: string;
  task_name: string;
  status: string;
  current_node_name: string;
  materials: Array<{
    name: string;
    required: boolean;
  }>;
  field_count: number;
  unenabled_rule_keys: string[];
  scope: TaskScope;
}

export interface DocumentUploadResponse extends ApiResponse {
  document_no: string;
  material_name: string;
  filename: string;
  size_bytes: number;
  status: string;
}

export interface ScenarioListItem {
  scenario_code: string;
  scenario_name: string;
  category: string | null;
  description: string | null;
  enabled: boolean;
  deleted: boolean;
  published_version_no: number | null;
  published_at: string | null;
  latest_version_no: number;
  material_count: number;
  field_count: number;
  rule_count: number;
  draft_revision: number | null;
  draft_source: string | null;
  draft_base_version_no: number | null;
  /** 当前配置是否已生效：null 表示已生效；非空时为 {code, message, details}。 */
  sync_error: ConfigSyncError | null;
  updated_at: string;
}

export interface ConfigSyncError {
  code: string;
  message: string;
  details: Record<string, JsonValue>;
}

export interface ScenarioVersionRow {
  version_no: number;
  status: string;
  source: string;
  source_label: string;
  change_summary: string | null;
  published_at: string;
  manifest_hash: string;
  material_count: number;
  field_count: number;
  rule_count: number;
  system_rule_count?: number;
}

export interface ScenarioDraftInfo {
  revision_no: number;
  source: string;
  source_label: string;
  base_version_no: number | null;
  /** 当前配置是否已生效：null 表示已生效。 */
  sync_error: ConfigSyncError | null;
  updated_at: string;
}

export interface ScenarioDetail extends ApiResponse {
  scenario_code: string;
  scenario_name: string;
  category: string | null;
  description: string | null;
  enabled: boolean;
  deleted: boolean;
  latest_version_no: number;
  created_at: string;
  updated_at: string;
  plan_count: number;
  draft: ScenarioDraftInfo | null;
  versions: ScenarioVersionRow[];
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
  material_name: string | null;
  name: string;
  value_type: string;
  unit: string | null;
  /** 币种：金额比较与加总的口径之一；留空表示未声明，不表示人民币。 */
  currency?: string | null;
  /** 产物来源：当前仅印章识别 SEAL；留空表示字段由常规字段抽取提供。 */
  artifact_source?: string | null;
  /**
   * 取数期间与报表口径：**由字段自己声明**，计算参数按这两项定位字段事实
   * （参数表不再声明它们）。必填，没有默认值。
   */
  period_scope?: string | null;
  statement_scope?: string | null;
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
  sheet_key: string;
  sheet_name: string;
  row_no: number;
  cells: string[];
  texts: Record<string, string>;
}

export interface DraftRuleItem {
  rule_key: string;
  name: string;
  node: string;
  operator: string;
  enabled: boolean;
  /** 系统派生规则（字段抽取 / 报告生成）只读，不能独立删除。 */
  system: boolean;
  inputs: DraftRuleInputView[];
  materials: Array<{ material_key: string; material_name: string | null }>;
  outputs: Array<{ field_key: string; field_name: string | null; material_key: string | null }>;
  parameters: Record<string, JsonValue>;
  requirement: string;
  applicability: { kind: string; material_keys?: string[] };
  source_node: string | null;
  source_rule_key: string | null;
  source_rule_name: string | null;
  risk_level: string | null;
  risk_action: string | null;
  requires_manual_review: boolean;
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
  quality_supported?: boolean | null;
  updated_at?: string | null;
}

export interface DraftSectionItem {
  section_key: string;
  title: string;
  content_instruction: string;
  field_keys: string[];
  field_names: string[];
  sort_no?: number;
  /** 节点版报告的分组属性：报告编号、名称、材料范围、输出格式、章节顺序、内容类型、结果排序与配置状态。 */
  report_code?: string | null;
  report_name?: string | null;
  material_keys?: string[];
  material_names?: string[];
  formats?: string[];
  section_order?: number | null;
  content_type?: string | null;
  result_sort?: string | null;
  config_status?: string | null;
  origin?: ConfigOrigin | null;
  updated_at?: string | null;
}

/**
 * 场景配置里的一个**节点实例**。
 *
 * 节点由用户配置：`node_code` 是实例码（同一场景可以有两个同类节点），
 * `node_type` 才是能力类型。顺序与依赖决定执行顺序，不由系统按规则内容生成。
 */
export interface DraftNodeItem {
  node_code: string;
  node_type: string;
  node_type_label: string;
  name: string;
  sequence_no: number;
  /** 上游节点**实例码**列表。 */
  depends_on: string[];
  config_json: Record<string, JsonValue> | null;
  enabled: boolean;
  updated_at: string;
}

/**
 * 场景**选用**的一张 Sheet 及它归属的节点实例。
 *
 * 场景配置「使用哪些 Sheet、Sheet 对应哪些节点及其流程关系」；统一模板协议定义
 * 「系统支持哪些 Sheet 以及每张 Sheet 的结构」。这份选择是用户配置，不由系统按内容反推。
 */
export interface DraftSheetItem {
  sheet_role: string;
  sheet_role_label: string;
  /** 该 Sheet 归属的节点实例码；null 表示已写入草稿但节点为空，发布会被阻断。 */
  node_code: string | null;
  sort_no: number;
}

/** 草稿中的计算配置核对行。来自 calculation_json，不是运行结果。 */
export interface DraftCalculationItem {
  rule_code: string | null;
  name: string | null;
  node_code: string | null;
  formula: string | null;
  result_code: string | null;
  unit: string | null;
  currency: string | null;
  status: string | null;
  frozen: boolean;
  origin?: ConfigOrigin | null;
  parameter_sources: Array<{ code: string | null; origin: ConfigOrigin | null }>;
  band_sources: Array<{ code: string | null; origin: ConfigOrigin | null }>;
}

export interface DraftDisabledSourceRow {
  row_key: string;
  sheet_role: string;
  sheet_name: string;
  import_job_no?: string;
  source_id: string | null;
  source: { sheet_key: string; row_no: number; cells: string[]; texts: Record<string, string> };
  columns: Array<{ key: string; label: string; value: string; cell?: string | null }>;
}

export interface DraftDisabledSourceRowActivationPreview extends ApiResponse {
  revision_no: number;
  rows: Array<{
    row_key: string;
    sheet_role: string;
    sheet_name: string;
    row_no: number;
    source_id: string | null;
  }>;
  updated_risk_codes: string[];
}

export interface ScenarioDraftView extends ApiResponse {
  scenario_code: string;
  revision_no: number;
  source: string;
  source_label: string;
  base_version_no: number | null;
  /** 统一模板协议的版本标识；页面新建时为 null。 */
  profile_version?: string | null;
  /** 当前配置是否已生效：null 表示已生效；非空时页面显示“尚未生效 + 原因”。 */
  sync_error?: ConfigSyncError | null;
  updated_at: string;
  /** 用户配置的节点实例。Sheet 绑定不在节点面板里改。 */
  nodes: DraftNodeItem[];
  /** 导入成功写入草稿后的 Sheet→节点。导入绑定下拉不读这份清单。 */
  sheets: DraftSheetItem[];
  calculations: DraftCalculationItem[];
  disabled_source_rows: DraftDisabledSourceRow[];
  materials: DraftMaterialItem[];
  fields: DraftFieldItem[];
  rules: DraftRuleItem[];
  sections: DraftSectionItem[];
  business_rule_count: number;
  system_rule_count: number;
}

export interface ScenarioVersionConfigView extends ApiResponse {
  scenario_code: string;
  scenario_name: string;
  version_no: number;
  source: string;
  source_label: string;
  change_summary: string | null;
  published_at: string;
  manifest_hash: string;
  unconfigured_check_node_types: string[];
  unconfigured_check_node_codes: string[];
  capability_gaps: Array<Record<string, JsonValue>>;
  materials: DraftMaterialItem[];
  fields: DraftFieldItem[];
  rules: DraftRuleItem[];
  sections: DraftSectionItem[];
  calculations: DraftCalculationItem[];
  disabled_source_rows: DraftDisabledSourceRow[];
  business_rule_count: number;
  system_rule_count: number;
}

export interface ScenarioPublishResult extends ApiResponse {
  scenario_code: string;
  scenario_name: string;
  version_no: number;
  status: string;
  source: string;
  change_summary: string | null;
  manifest_hash: string;
  material_count: number;
  field_count: number;
  rule_count: number;
  published_at: string;
  /** 内容与最新已发布版本完全一致时复用该版本，不制造重复版本。 */
  reused: boolean;
}

export interface VersionCompareEntry {
  kind: "MATERIAL" | "FIELD" | "RULE";
  key: string;
  name: string;
  change: "ADDED" | "REMOVED" | "CHANGED";
  changed_fields?: string[];
  before?: Record<string, JsonValue>;
  after?: Record<string, JsonValue>;
}

export interface VersionCompareResult extends ApiResponse {
  scenario_code: string;
  from_version_no: number;
  to_version_no: number;
  entries: VersionCompareEntry[];
  summary: { added: number; removed: number; changed: number };
}
