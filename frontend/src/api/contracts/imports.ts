/**
 * 规则库导入：工作表绑定、候选、问题、预检查与导入作业。
 *
 * 契约按业务域拆分：本文件只声明本域的载荷形状，跨域引用一律显式 import，
 * 不再让所有页面共同依赖一个不断扩大的中心文件。
 */

import type { ApiResponse, JsonValue, OperationAccepted } from "./common";

export interface ImportSheet {
  sheet_key: string;
  name: string;
  visible: boolean;
  status: string;
  fixed_excluded: boolean;
  role: string | null;
  header_row: number | null;
  summary: {
    max_row: number;
    max_column: number;
    non_empty_rows: number;
    merged_ranges: string[];
    truncated: boolean;
    unprocessed_note: string | null;
  };
  rows: Array<{
    row_no: number;
    cells: Record<string, JsonValue>;
  }>;
}

export interface ImportIssueOption {
  label: string;
  value: JsonValue;
}

export interface ImportIssueSource {
  target_type: string | null;
  sheet_name: string | null;
  row_no: number | null;
  cells: string[];
}

export interface ImportIssue {
  issue_no: string;
  category: string;
  severity: string;
  status: string;
  title: string;
  description: string;
  source: ImportIssueSource;
  /** SELECT=选择题；DECIMAL/TEXT=业务输入控件，没有候选选项。 */
  input_kind: "SELECT" | "DECIMAL" | "TEXT";
  options: ImportIssueOption[];
  decision: JsonValue;
}

export type CandidateDecisionState = "AUTO" | "PENDING" | "MANUAL" | "EXCLUDED";

export interface RuleComparison {
  operator_label: string;
  /** 加总比较的右侧加数：全部展示，不能只显示前两个输入。 */
  addends?: Array<{ material_name: string | null; field_name: string | null }>;
  /** 风险成立条件与等级原文；等级差异必须显式可见，不能被较低等级覆盖。 */
  risk_condition?: string | null;
  risk_condition_label?: string | null;
  check_level?: string | null;
  risk_level_value?: string | null;
  config_status?: string | null;
  support_status?: string;
  support_message?: string;
  left: { material_name: string | null; field_name: string | null } | null;
  right: { material_name: string | null; field_name: string | null } | null;
  extra_inputs: Array<{ material_name: string | null; field_name: string | null }>;
  comparison_label: string | null;
  parameters: Record<string, JsonValue>;
  /** 审核要求原文：页面展示的必须是用户实际填写的内容，不是模型的改写结果。 */
  requirement: string | null;
  applicability: { kind: string; material_keys: string[] };
  applicability_label: string;
  /** 风险疑点规则的来源节点：契约值 + 服务端给出的中文文案；未提供时不显示。 */
  source_node: string | null;
  source_node_label: string | null;
  source_rule_name: string | null;
  scope_material_names: Array<string | null>;
  output_field_names: Array<string | null>;
}

export interface ProposalMaterial {
  candidate_ref: string;
  name: string;
  material_type: string | null;
  required: boolean;
  description?: string | null;
  decision_state: CandidateDecisionState;
  original: Record<string, JsonValue> | null;
  source_sheet: string;
  source_row: number;
  source_cells: string[];
}

export interface ProposalField {
  candidate_ref: string;
  name: string;
  value_type: string;
  unit: string | null;
  /** 币种；留空表示未声明，不表示人民币。 */
  currency?: string | null;
  required: boolean;
  instruction: string;
  material_name: string | null;
  decision_state: CandidateDecisionState;
  original: Record<string, JsonValue> | null;
  source_sheet: string;
  source_row: number;
  source_cells: string[];
}

export interface ProposalRule {
  candidate_ref: string;
  name: string;
  node: string;
  node_name: string;
  operator: string;
  decision_state: CandidateDecisionState;
  original: Record<string, JsonValue> | null;
  comparison: RuleComparison;
  source_sheet: string;
  source_row: number;
  source_cells: string[];
}

export interface ProposalSheet {
  sheet_key: string;
  sheet_name: string;
  excluded: boolean;
  suggested_role: string;
  materials: ProposalMaterial[];
  fields: ProposalField[];
  rules: ProposalRule[];
  report_sections: Array<{ section_name: string }>;
  question_count: number;
}

export interface ImportJob extends ApiResponse {
  job_no: string;
  source_filename: string;
  status: string;
  profile_version: string;
  profile_display_name: string;
  sheets: ImportSheet[];
  issues: ImportIssue[];
  proposals: {
    sheets: ProposalSheet[];
  } | null;
  manifest_draft: {
    scene_code: string | null;
    scene_name: string | null;
  } | null;
  /** 每条来源业务记录的处置：已形成候选 / 明确排除 / 待处理问题。 */
  coverage: {
    sheets: Array<{
      sheet_key: string;
      sheet_name: string;
      business_rows: number;
      rows: Array<{
        row_no: number;
        disposition: "CANDIDATE" | "EXCLUDED" | "QUESTION";
        reason: string | null;
        candidate_count: number;
        question_count: number;
        merged_into: string | null;
        notes: string[];
      }>;
    }>;
    totals: Record<string, number>;
  } | null;
  /** 草稿修订号：绑定、排除、裁决、候选原地修正都会递增。 */
  draft_revision: number;
  precheck?: PrecheckResult | null;
  failure: { code: string; message: string } | null;
  latest_operation: OperationAccepted | null;
  created_at: string;
}

export interface ImportJobSummary extends ApiResponse {
  job_no: string;
  source_filename: string;
  status: string;
  created_at: string;
}

export interface PrecheckBinding {
  profile_version: string;
  draft_revision: number;
  manifest_hash: string;
  template_mode: "new" | "update";
  target_scenario_code: string | null;
  target_scenario_name: string;
  scene_code: string;
  scene_name: string;
  checked_at: string;
  /** 预检查时逐条确认过的能力缺口规则键；发布必须与它完全一致。 */
  acknowledged_capability_gaps: string[];
}

export interface CapabilityCoverageRow {
  rule_key: string | null;
  rule_name: string | null;
  node: string | null;
  operator: string | null;
  capability_id: string | null;
  capability_label: string | null;
  capability_version: string | null;
  known?: boolean;
  input_kind?: string;
  statuses?: string[];
  output_keys?: string[];
  supports_evidence?: boolean;
  supports_comparison?: boolean;
  evidence?: string | null;
  judge?: string | null;
  extension_namespace?: string | null;
  implemented: boolean;
  unimplemented_reason: string | null;
  parameters?: string[];
}

export interface CapabilityCoverage {
  capability_version: string;
  declared_rule_count: number;
  implemented_rule_count: number;
  unimplemented_rule_count: number;
  rules: CapabilityCoverageRow[];
}

export interface PrecheckResult {
  ready: boolean;
  blockers: Array<{
    code: string;
    message: string;
    details?: Record<string, JsonValue>;
  }>;
  summary: {
    materials: number;
    fields: number;
    rules: number;
    issues: number;
  };
  /** 这次预检查绑定到的发布意图；发布必须与它完全一致。 */
  binding: PrecheckBinding | null;
  /** 已配置但当前无法执行的检查：必须在预览里可见，不能发布时才发现永远无法执行。 */
  capability_gaps: Array<{
    rule_key?: string;
    rule_name: string;
    node?: string;
    operator?: string;
    capability_id?: string;
    capability_version?: string;
    gap: string;
    message: string;
  }>;
  /** 尚未被逐条确认的能力缺口规则键：发布请求必须把它们补进确认集合才放行。 */
  unacknowledged_capability_gaps?: string[];
  /** 展开后的能力覆盖结果。 */
  capability_coverage?: CapabilityCoverage | null;
}

export interface TemplateSummary extends ApiResponse {
  scenario_code: string;
  scenario_name: string;
  latest_version_no: number;
  status: string;
  published_at: string;
}
