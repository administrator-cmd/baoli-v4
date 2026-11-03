/** 报告与风险结果：报告正文、结论、格式支持与风险列表。 */

import type { JsonValue } from "./common";

export interface Report {
  task_no: string;
  report_no: string | null;
  report_name: string | null;
  reports: Array<{
    report_no: string;
    report_name: string;
    current: boolean;
    /** 每份报告的生成、覆盖与缺项口径分开给出。 */
    report_generated?: boolean;
    scope_execution_complete?: boolean;
    unresolved_count?: number;
  }>;
  status: "COMPLETE" | "INCOMPLETE" | "NOT_GENERATED";
  content_type: "text/markdown" | "text/plain" | "text/html";
  content: string | null;
  reasons: string[];
  /** 报告是否已生成。 */
  report_generated: boolean;
  /** 已配置的审核范围是否执行完整；与"是否通过"不是一回事。 */
  scope_execution_complete: boolean;
  /** 风险是否可判定；未配置风险评级时为 false，但不代表执行故障。 */
  risk_conclusion_available: boolean;
  /** 报告范围只对应当前有效事实与执行结果。 */
  results_scope: "CURRENT_RUN";
  /** 当前入口返回的报告是否仍对应当前执行；为 false 时页面必须标明“已失效”。 */
  current: boolean;
  /** 失效的真实原因；current 为 true 时为空。 */
  stale_reason?: string | null;
  input_revision: number;
  snapshot: {
    task_input_revision: number;
    upload_ids: number[];
    field_value_ids: number[];
    review_ids: number[];
    rule_result_ids: number[];
  } | null;
  /** 本节报告声明的输出格式与真实支持状态；PDF/Word/Excel 当前未实现。 */
  format_support?: ReportFormatSupport[];
  /** 系统固定事实章节之外的业务附加章节数量；零业务章节不等于零报告。 */
  business_section_count?: number;
  /**
   * 报告结论的结构化形式。页面优先读它，不再靠匹配正文里的中文行首来提取结论——
   * 正文任何一次措辞调整都会让字符串匹配静默退化成「没有结论」。
   */
  conclusion?: ReportConclusion | null;
}

export interface ReportFormatSupport {
  format: string;
  supported: boolean;
  message: string;
  report_code?: string | null;
  report_name?: string | null;
}

export interface ReportConclusion {
  report_generated: boolean;
  scope_execution_complete: boolean;
  scope_execution_note: string;
  risk_conclusion_available: boolean;
  risk_note: string;
  unimplemented_formats: Array<{
    format: string;
    report_code: string | null;
    report_name: string | null;
    message: string;
  }>;
}

export interface ReportRow {
  report_no: string;
  report_name: string;
  task_no: string;
  task_name: string;
  scenario_name: string;
  scenario_version_no: number | null;
  plan_name: string | null;
  generated_at: string;
  report_generated: boolean;
  scope_execution_complete: boolean;
  risk_conclusion_available: boolean;
  status: string;
  archived_at: string | null;
  current: boolean;
  stale: boolean;
  /** 失效的真实原因；stale 为 false 时为空。 */
  stale_reason: string | null;
  input_revision: number | null;
  task_input_revision: number;
  remark: string | null;
}

export interface ReportDetail {
  report_no: string;
  report_name: string;
  task_no: string | null;
  task_name: string | null;
  scenario_name: string | null;
  scenario_version_no: number | null;
  plan_name: string | null;
  generated_at: string;
  content_type: string;
  content: string | null;
  report_generated: boolean;
  scope_execution_complete: boolean;
  risk_conclusion_available: boolean;
  reasons: Record<string, JsonValue>;
  snapshot: Record<string, JsonValue> | null;
  status: string;
  archived_at: string | null;
  remark: string | null;
  stale: boolean;
  /** 失效的真实原因；stale 为 false 时为空。 */
  stale_reason: string | null;
  input_revision: number | null;
  task_input_revision: number | null;
}

export interface RiskResultRow {
  task_no: string;
  task_name: string;
  scenario_name: string;
  scenario_version_no: number | null;
  plan_name: string | null;
  rule_key: string;
  rule_code: string | null;
  name: string;
  risk_level: string | null;
  status: string;
  message: string;
  suggestion: string | null;
  source_rule_key: string | null;
  source_rule_name: string | null;
  result_id: number;
  created_at: string;
}
