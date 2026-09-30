/** 规则库导入：工作表绑定、候选、问题、草稿写入与导入作业。 */

import type { ApiResponse, JsonValue, OperationAccepted } from "./common";
import type { DraftCalculationItem } from "./scenes";

export interface ProfileSheet {
  sheet_role: string;
  /** 编号前缀（例如 RSK-）：跨表引用靠它判断指向。 */
  code_prefix?: string;
  display_name: string;
  /** 这张 Sheet 可以绑定的节点类型（绑定由用户确认，系统只给建议）。 */
  node_types: string[];
  column_count: number;
}

export interface ProfileSummary {
  profile_version: string | null;
  display_name: string | null;
  /** 业务场景冻结的 Schema 身份；与统一执行协议版本分开。 */
  scenario_schema_key?: string | null;
  scenario_schema_version?: string | null;
  /** 内部统一协议版本；导入作业顶层 schema_version 是业务场景版本。 */
  schema_version: string | null;
  /** 模板说明页名称：模板生成、导出与导入读同一声明。 */
  instruction_sheet?: string;
  sheet_count: number;
  node_types: Array<{ node_type: string; display_name: string; depends_on_types: string[] }>;
  sheets: ProfileSheet[];
  /** 作业所属场景（上传时指定）；历史作业为 null。 */
  scenario_code?: string | null;
  scenario_name?: string | null;
}

export interface ImportSheet {
  sheet_key: string;
  name: string;
  visible: boolean;
  status: string;
  fixed_excluded: boolean;
  /** 已确认的 Sheet 角色（用户确认后落库）；null 表示尚未绑定。 */
  role: string | null;
  /** 已确认绑定的**节点实例码**；null 表示尚未绑定。 */
  node_code: string | null;
  /** 系统按表头给出的**建议**用途：只用于页面预选，不构成绑定。 */
  suggested_role: string | null;
  /** 建议用途的中文名，由服务端给出。 */
  suggested_role_label: string;
  /** 已确认用途的中文名，由服务端给出。 */
  role_label: string;
  header_row: number | null;
  /**
   * 表头候选行（服务端识别结果）。**页面不展示它**：「前 10 行里填充单元格最多的行」
   * 会把填满数据的数据行和真表头并列，展示出来只会制造歧义；表头行按默认值给出、可改。
   */
  header_candidates?: number[];
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
  /** 规则所在行的原始单元格文本（列名 → 原文），供预览逐项对照 Excel。 */
  source_texts: Record<string, string>;
  applicability: { kind: string; material_keys: string[] };
  applicability_label: string;
  direction_label: string | null;
  threshold: JsonValue;
  /** 风险疑点规则的来源节点：契约值 + 服务端给出的中文文案；未提供时不显示。 */
  source_node: string | null;
  source_node_label: string | null;
  source_rule_name: string | null;
  source_rule_refs: string[];
  source_rule_names: string[];
  scope_material_refs: string[];
  scope_material_names: Array<string | null>;
  output_field_names: Array<string | null>;
  input_units: Array<string | null>;
  input_currencies: Array<string | null>;
}

export interface ProposalMaterial {
  candidate_ref: string;
  name: string;
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
  material_ref: string;
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

export interface ProposalCalculation extends Pick<DraftCalculationItem, "origin" | "parameter_sources" | "band_sources"> {
  candidate_ref: string;
  rule_code: string;
  rule_name: string;
  formula: string;
  result_code: string;
  result_type: string;
  node_code: string;
  status: string;
  parameters: Array<Record<string, JsonValue>>;
  bands: Array<Record<string, JsonValue>>;
  bands_text: string;
  unresolved_identifiers: string[];
  decision_state: CandidateDecisionState;
  source_sheet: string;
  source_row: number;
  source_cells: string[];
}

export interface ImportJob extends ApiResponse {
  job_no: string;
  source_filename: string;
  status: string;
  profile_version: string;
  /** 上传时冻结的业务场景 Schema；profile.schema_version 是内部统一协议版本。 */
  scenario_code?: string | null;
  schema_key?: string | null;
  schema_version?: string | null;
  schema_json?: Record<string, JsonValue> | null;
  profile: ProfileSummary;
  sheets: ImportSheet[];
  issues: ImportIssue[];
  proposals: {
    sheets: ProposalSheet[];
    calculations: ProposalCalculation[];
  } | null;
  manifest_draft: {
    scene_code: string | null;
    scene_name: string | null;
  } | null;
  /** 每条来源业务记录的处置：候选 / 停用保留 / 明确排除 / 待处理问题。 */
  coverage: {
    notices?: Array<{
      code: string;
      message: string;
      sheet_name: string;
      row_no: number;
    }>;
    sheets: Array<{
      sheet_key: string;
      sheet_name: string;
      business_rows: number;
      rows: Array<{
        row_no: number;
        disposition: "CANDIDATE" | "DISABLED" | "EXCLUDED" | "QUESTION";
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
