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
    material_type: string;
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
  /** 自动纳入的真实原因（报告生成 / 风险来源 / 字段抽取）；显式选择与未启用为空。 */
  reason?: string | null;
}

export interface TaskScope {
  enabled_rules: RuleSnapshotEntry[];
  unenabled_rules: RuleSnapshotEntry[];
  auto_included_rules: RuleSnapshotEntry[];
  enabled_rule_keys?: string[];
  unenabled_rule_keys?: string[];
  auto_included_rule_keys?: string[];
  auto_included_reasons?: Record<string, string>;
  unconfigured_nodes: string[];
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
  auto_included_rule_keys: string[];
  unenabled_rule_keys: string[];
  auto_included_rule_names: string[];
  /** 每条自动补入规则的真实原因，键为 rule_key。 */
  auto_included_reasons?: Record<string, string>;
  unconfigured_check_nodes: string[];
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
    material_type: string;
  }>;
  field_count: number;
  auto_included_rule_keys: string[];
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
  name: string;
  material_type: string | null;
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
  required: boolean;
  instruction: string;
  /** 业务编号（Excel 的「字段编号」列）：导出时写回。 */
  source_ref?: string | null;
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
  /** 业务配置事实：检查等级、合格要求原文、风险等级原文、成立条件、多来源与配置状态。 */
  check_level?: string | null;
  check_operator?: string | null;
  risk_level_value?: string | null;
  risk_condition?: string | null;
  source_rule_keys?: string[];
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
  updated_at?: string | null;
}

export interface ScenarioDraftView extends ApiResponse {
  scenario_code: string;
  revision_no: number;
  source: string;
  source_label: string;
  base_version_no: number | null;
  /** 草稿的来源格式版本：由导入发布 / 版本修订 / 复制继承；页面新建时为 null。 */
  profile_version?: string | null;
  /** 当前配置是否已生效：null 表示已生效；非空时页面显示“尚未生效 + 原因”。 */
  sync_error?: ConfigSyncError | null;
  updated_at: string;
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
  unconfigured_check_nodes: string[];
  capability_gaps: Array<Record<string, JsonValue>>;
  materials: DraftMaterialItem[];
  fields: DraftFieldItem[];
  rules: DraftRuleItem[];
  sections: DraftSectionItem[];
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
