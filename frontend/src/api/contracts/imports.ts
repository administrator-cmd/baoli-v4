/** 规则库导入：解析结果、业务配置调整与草稿写入。 */

import type { JsonValue, OperationAccepted } from "./common";
import type { ConfigOrigin } from "./scenes";

export type ImportProposalStatus = "CLEAR" | "PENDING" | "DISABLED";
export type ImportProposalGroup = "materials" | "fields" | "rules" | "calculations" | "report_sections";

export interface ProposalInterpretationView {
  interpretation?: { basis: string; missing_details: string[]; edited_after_interpretation?: boolean } | null;
}

export interface ProposalSupportingContent {
  support_ref: string;
  kind: "MATERIAL_SCOPE" | "DICTIONARY" | "MAPPING" | "EXPLANATION";
  title: string;
  content: string;
  reason: string;
  targets: Array<{ group: ImportProposalGroup; candidate_ref: string; usage: string }>;
  unresolved_references: string[];
  origin: ConfigOrigin;
}

export interface DependencyQuestion {
  owner_ref: string;
  term: string;
  options: Array<{ ref: string; group: "fields" | "rules" | "calculations"; name: string; sheet_name: string; row_no: number }>;
}

export interface ImportProposalChanges {
  dependency_answers?: string[];
  name?: string;
  description?: string;
  required?: boolean;
  instruction?: string;
  requirement?: string;
  execution_mode?: string;
  rule_role?: "CHECK" | "RISK";
  rule_name?: string;
  formula?: string;
  section_name?: string;
  content_instruction?: string;
  config_status?: ImportProposalStatus;
  status?: ImportProposalStatus;
}

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

export interface ProposalMaterial extends ProposalInterpretationView {
  candidate_ref: string;
  source_id?: string | null;
  origin?: ConfigOrigin | null;
  name: string;
  required: boolean;
  description?: string | null;
}

export interface ProposalField extends ProposalInterpretationView {
  candidate_ref: string;
  source_id?: string | null;
  origin?: ConfigOrigin | null;
  name: string;
  value_type: string;
  unit: string | null;
  /** 币种；留空表示未声明，不表示人民币。 */
  currency?: string | null;
  /** 字段自身声明的期间、报表口径与主体范围；同名不同口径必须是不同字段。 */
  period_scope?: string | null;
  statement_scope?: string | null;
  entity_scope?: string | null;
  required: boolean;
  instruction: string;
  /** 字段的补充业务事实，不参与抽取动作；没有时为空对象。 */
  business_context?: Record<string, JsonValue>;
  material_ref: string;
  material_name: string | null;
  config_status?: string | null;
}

/** 第二轮核对后仍未建立绑定的输入；只提示配置完整性，不改变启用状态。 */
export interface ProposalBindingGap {
  owner_group: "rules" | "calculations";
  owner_ref: string;
  owner_name?: string | null;
  parameter_code?: string | null;
  missing_input: string;
  reason: string;
  original_requirement?: string;
}

export interface ProposalRule extends ProposalInterpretationView {
  candidate_ref: string;
  source_id?: string | null;
  origin?: ConfigOrigin | null;
  name: string;
  operator: string;
  rule_code?: string | null;
  rule_role?: "CHECK" | "RISK";
  execution_mode?: string | null;
  requirement: string;
  config_status?: string | null;
  comparison: RuleComparison;
}

export interface ProposalReportSection extends ProposalInterpretationView {
  candidate_ref: string;
  report_code?: string | null;
  origin?: ConfigOrigin | null;
  section_name: string;
  content_instruction: string;
  report_name: string | null;
  formats: string[];
  config_status?: string | null;
}

export interface ProposalCalculation extends ProposalInterpretationView {
  candidate_ref: string;
  source_id?: string | null;
  origin?: ConfigOrigin | null;
  rule_code: string;
  rule_name: string;
  formula: string;
  requirement: string;
  business_context: Record<string, JsonValue>;
  result_code: string;
  result_type: string;
  status: string;
  parameters: Array<Record<string, JsonValue>>;
  input_gaps?: Array<{
    parameter_code?: string | null;
    missing_input: string;
    reason: string;
    original_requirement?: string;
  }>;
}

export interface ImportJob {
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
  proposals: {
    materials: ProposalMaterial[];
    fields: ProposalField[];
    rules: ProposalRule[];
    calculations: ProposalCalculation[];
    report_sections: ProposalReportSection[];
    dependency_questions?: DependencyQuestion[];
    binding_gaps?: ProposalBindingGap[];
    /** 未记录与已识别但为空应明确区分；辅助资料不计入执行规则。 */
    supporting_content?: ProposalSupportingContent[] | null;
  } | null;
  /** 草稿修订号：解析结果变更时递增。 */
  draft_revision: number;
  /** 第一轮目录可复用：为 true 时可以只重跑第二轮。 */
  can_resume_refinement?: boolean;
  failure: { code: string; message: string } | null;
  latest_operation: OperationAccepted | null;
  created_at: string;
}

export interface ImportJobSummary {
  job_no: string;
  source_filename: string;
  status: string;
  created_at: string;
}
