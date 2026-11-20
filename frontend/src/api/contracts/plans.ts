/** 审核方案与任务创建：方案选择、范围预览与任务创建结果。 */

import type { JsonValue } from "./common";
import type { ConfigOrigin } from "./scenes";

export interface PlanListItem {
  plan_code: string;
  plan_name: string;
  scenario_code: string;
  scenario_name: string;
  description: string | null;
  enabled: boolean;
  deleted: boolean;
  base_scenario_version_no: number | null;
  /** 范围是否已生效：false 表示尚未生成可用配置，具体原因见 sync_error。 */
  effective: boolean;
  sync_error: { code?: string; message?: string } | null;
  enabled_rule_count: number;
  explicit_rule_count: number;
  updated_at: string;
}

/** 方案的基本资料与范围状态；配置视图和生命周期接口共用这一组字段。 */
export interface PlanStatus {
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
}

export interface PlanSelection extends PlanStatus {
  /** 方案自己的全部规则与启停：这就是「这个方案跑哪些规则」的唯一来源。 */
  rules: Array<{ rule_key: string; name: string; node: string | null; enabled: boolean }>;
  calculation_rules: Array<{ rule_code: string; name: string; enabled: boolean }>;
  materials: Array<{ material_key: string; name: string; required: boolean }>;
  field_count: number;
  updated_at: string;
}

/** 方案的可编辑配置：方案状态、材料、字段、审核规则、计算定义和报告章节。 */
export interface PlanConfig extends PlanStatus {
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
}

export interface PlanMaterialItem {
  material_key: string;
  name: string;
  required: boolean;
  description: string;
  config_status?: string | null;
  business_context?: Record<string, unknown>;
  source_ref?: string | null;
  sort_no?: number;
}

export interface PlanFieldItem {
  field_key: string;
  enabled?: boolean;
  config_status?: string | null;
  business_context?: Record<string, unknown>;
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
  entity_scope?: string | null;
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
  origin?: ConfigOrigin | null;
  sort_no?: number;
}

export interface PlanCalculationRuleItem {
  formula: string;
  requirement: string;
  business_context: Record<string, JsonValue>;
  rule_code: string;
  rule_name: string;
  result_code: string;
  source_id: string | null;
  result_type: string;
  unit: string | null;
  currency: string | null;
  period_policy: string | null;
  statement_scope: string | null;
  status: "CLEAR" | "PENDING" | "DISABLED";
  remark: string;
  parameters: PlanCalculationParameter[];
  depends_on_result_codes: string[];
  node_code: string | null;
  enabled: boolean;
  origin?: ConfigOrigin | null;
}

export interface PlanCalculationParameter {
  parameter_code: string;
  source_type: string;
  source_code: string | null;
  constant_value?: JsonValue;
  [key: string]: unknown;
}

export interface PlanSectionItem {
  section_key: string;
  title: string;
  content_instruction: string;
  field_keys: string[];
  data_refs: string[];
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

/** 整体提交方案的业务配置。 */
export interface PlanConfigPayload {
  materials: PlanMaterialItem[];
  fields: PlanFieldItem[];
  rules: PlanRuleItem[];
  sections: PlanSectionItem[];
  calculation_rules: PlanCalculationRuleItem[];
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
  current_node: string;
  current_node_name: string;
  status: string;
  block_reason: string | null;
  deleted: boolean;
  created_at: string;
  updated_at: string;
}

export interface TaskCreatedResult {
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
