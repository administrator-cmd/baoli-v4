/** 规则库导入：工作表选择、候选、问题、草稿写入与导入作业。 */

import type { ApiResponse, JsonValue, OperationAccepted } from "./common";
import type { DraftCalculationItem } from "./scenes";

export interface ProfileSummary {
  profile_version: string | null;
  display_name: string | null;
  /** 业务场景冻结的 Schema 身份；与内部能力目录版本分开。 */
  scenario_schema_key?: string | null;
  scenario_schema_version?: string | null;
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
  /** 预览启发式候选行；页面不要求确认表头，LLM 使用完整工作表内容。 */
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
}

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
  source_rule_name: string | null;
  source_rule_refs: string[];
  source_rule_names: string[];
  /** 风险关联的计算结果：名称与所属节点来自后端当前配置。 */
  calculation_sources: Array<{
    result_code: string;
    rule_name: string | null;
    node_name: string | null;
  }>;
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
  original: Record<string, JsonValue> | null;
  source_sheet: string;
  source_row: number;
  source_cells: string[];
}

export interface ProposalRule {
  candidate_ref: string;
  name: string;
  operator: string;
  config_status?: "CLEAR" | "PENDING" | "DISABLED" | null;
  original: Record<string, JsonValue> | null;
  comparison: RuleComparison;
  source_sheet: string;
  source_row: number;
  source_cells: string[];
}

export interface ProposalSheet {
  sheet_key: string;
  sheet_name: string;
  suggested_role: string;
  materials: ProposalMaterial[];
  fields: ProposalField[];
  rules: ProposalRule[];
  report_sections: Array<{ section_name: string }>;
}

export interface ProposalCalculation extends Pick<DraftCalculationItem, "origin" | "parameter_sources"> {
  candidate_ref: string;
  rule_code: string;
  rule_name: string;
  formula: string;
  requirement: string;
  business_context: Record<string, JsonValue>;
  result_code: string;
  result_type: string;
  node_code: string;
  node_name: string | null;
  status: string;
  parameters: Array<Record<string, JsonValue>>;
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
    rejected_candidates?: Array<{
      kind: string;
      path: string;
      name: string | null;
      candidate_ref: string | null;
      source: { sheet_key?: string; row_no?: number; cells?: string[] } | null;
      code: string;
      message: string;
      raw: JsonValue;
    }>;
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
        disposition: "CANDIDATE" | "DISABLED" | "EXCLUDED" | "UNPARSED";
        reason: string | null;
        candidate_count: number;
        merged_into: string | null;
        notes: string[];
      }>;
    }>;
    totals: Record<string, number>;
  } | null;
  /** 草稿修订号：工作表选择与解析结果变更时递增。 */
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
