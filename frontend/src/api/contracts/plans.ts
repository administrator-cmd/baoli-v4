/** 审核方案与任务创建：方案选择、范围预览与任务创建结果。 */

import type { ApiResponse, JsonValue } from "./common";
import type { DraftCalculationItem } from "./scenes";

export interface PlanListItem {
  plan_code: string;
  plan_name: string;
  scenario_code: string;
  scenario_name: string;
  description: string | null;
  enabled: boolean;
  deleted: boolean;
  base_scenario_version_no: number | null;
  /** 范围是否已生效：false 表示最近一次保存没算出可用范围，方案显示为「待核对」。 */
  effective: boolean;
  sync_error: { code?: string; message?: string } | null;
  enabled_rule_count: number;
  explicit_rule_count: number;
  updated_at: string;
}

export interface PlanSelection extends ApiResponse {
  plan_code: string;
  plan_name: string;
  scenario_code: string;
  description: string | null;
  enabled: boolean;
  deleted: boolean;
  /** 范围是否已生效；sync_error 非空时为 false，方案不能被任务引用。 */
  effective: boolean;
  sync_error: { code?: string; message?: string } | null;
  scope_hash: string | null;
  /** 这份方案从哪一版场景复制而来。只是溯源：执行与范围都不再读它。 */
  base_scenario_version: {
    version_no: number;
    status: string;
    published_at: string;
    scenario_name: string;
  } | null;
  /** 方案自己的全部规则与启停：这就是「这个方案跑哪些规则」的唯一来源。 */
  rules: Array<{ rule_key: string; name: string; node: string | null; enabled: boolean }>;
  calculation_rules: Array<{ rule_code: string; name: string; enabled: boolean }>;
  materials: Array<{ material_key: string; name: string; required: boolean }>;
  field_count: number;
  updated_at: string;
}

/** 方案的可编辑配置：材料、字段、普通规则、计算规则启停和报告章节。 */
export interface PlanConfig extends ApiResponse {
  plan_code: string;
  materials: PlanMaterialItem[];
  fields: PlanFieldItem[];
  rules: PlanRuleItem[];
  sections: PlanSectionItem[];
  calculation_rules: PlanCalculationRuleItem[];
  /** 只读：本方案会跑哪些节点（场景级执行结构，方案里不改）。 */
  nodes: Array<{
    node_code: string;
    node_type: string;
    name: string;
    sequence_no: number;
    depends_on: string[];
    enabled: boolean;
  }>;
  /** 只读：本方案选用了哪些 Sheet。 */
  selected_sheets: Array<{ sheet_role: string; node_code: string | null; sheet_key?: string; sheet_name?: string }>;
}

export interface PlanMaterialItem {
  material_key: string;
  name: string;
  required: boolean;
  description: string;
  source_ref?: string | null;
  sort_no?: number;
}

export interface PlanFieldItem {
  field_key: string;
  material_key: string;
  node_code?: string | null;
  name: string;
  value_type: string;
  extraction_strategy: "RAW_TEXT" | "DETERMINISTIC" | "STRUCTURED";
  structure_schema: Record<string, unknown> | null;
  unit?: string | null;
  currency?: string | null;
  artifact_source?: string | null;
  period_scope: string;
  statement_scope: string;
  required: boolean;
  instruction: string;
  source_ref?: string | null;
  sort_no?: number;
}

export interface PlanRuleItem {
  rule_key: string;
  rule_code?: string | null;
  is_system?: boolean;
  name: string;
  node_code: string;
  operator_code: string;
  enabled: boolean;
  rule_json: Record<string, unknown>;
  source_ref?: string | null;
  sort_no?: number;
}

export interface PlanCalculationRuleItem extends Pick<DraftCalculationItem, "origin" | "parameter_sources"> {
  formula: string;
  requirement: string;
  business_context: Record<string, JsonValue>;
  rule_code: string;
  rule_name: string;
  node_code: string | null;
  enabled: boolean;
}

export interface PlanAdjustmentField {
  label: string;
  before: unknown;
  after: unknown;
}

export interface PlanAdjustments extends ApiResponse {
  plan_code: string;
  plan_name: string;
  scenario_code: string;
  base_scenario_version_no: number;
  items: Array<{
    category: string;
    code: string;
    name: string;
    change: "ADDED" | "REMOVED" | "MODIFIED";
    fields: PlanAdjustmentField[];
  }>;
}

export interface PlanSectionItem {
  section_key: string;
  title: string;
  content_instruction: string;
  field_keys: string[];
  report_code?: string | null;
  report_name?: string | null;
  material_keys?: string[] | null;
  formats?: string[] | null;
  section_order?: number | null;
  content_type?: string | null;
  result_sort?: string | null;
  risk_level_order?: string[] | null;
  config_status?: string | null;
  sort_no?: number;
}

/** 整体提交方案的配置与计算规则选择。 */
export interface PlanConfigPayload {
  materials: PlanMaterialItem[];
  fields: PlanFieldItem[];
  rules: PlanRuleItem[];
  sections: PlanSectionItem[];
  calculation_rules: Array<Pick<PlanCalculationRuleItem, "rule_code" | "enabled">>;
}

export interface TaskPlanInfo {
  plan_code: string;
  plan_name: string;
  /** 任务创建时冻结的场景版本号，不是方案后来可能指向的版本。 */
  scenario_version_no: number;
}

export interface AuditTaskRow {
  task_no: string;
  task_name: string;
  remark: string | null;
  scenario_code: string;
  scenario_name: string;
  scenario_version_no: number;
  plan_code: string | null;
  plan_name: string | null;
  is_legacy_config: boolean;
  current_node: string;
  current_node_name: string;
  status: string;
  deleted: boolean;
  created_at: string;
  updated_at: string;
}

export interface TaskCreatedResult extends ApiResponse {
  task_no: string;
  task_name: string;
  task_status: string;
  scenario_code: string;
  scenario_name: string;
  scenario_version_no: number;
  plan_name: string;
  current_node_name: string;
  material_count: number;
  field_count: number;
  /** 幂等重放返回原任务时为 true。 */
  reused: boolean;
}
