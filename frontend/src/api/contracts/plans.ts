/**
 * 审核方案与任务创建：方案选择、范围预览与任务创建结果。
 *
 * 契约按业务域拆分：本文件只声明本域的载荷形状，跨域引用一律显式 import，
 * 不再让所有页面共同依赖一个不断扩大的中心文件。
 */

import type { ApiResponse } from "./common";

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
  base_scenario_version: {
    version_no: number;
    status: string;
    published_at: string;
    scenario_name: string;
  } | null;
  selected_rule_keys: string[];
  selected_rules: Array<{ rule_key: string; name: string; node: string | null; missing: boolean }>;
  materials: Array<{ material_key: string; name: string; required: boolean }>;
  field_count: number;
  updated_at: string;
  /** 切换场景版本时被移除的旧选择（中文名），如实返回，不静默丢弃。 */
  dropped_rule_keys: string[];
}

export interface PlanScopeRow {
  rule_key: string;
  name: string;
  node: string | null;
  selection: "EXPLICIT" | "AUTO" | "UNENABLED";
  system: boolean;
  /** selection=AUTO 时的真实纳入原因；其余情况为空。 */
  reason?: string | null;
}

export interface PlanScopePreview extends ApiResponse {
  scenario_code: string;
  version_no: number;
  rows: PlanScopeRow[];
  explicit_rule_keys: string[];
  auto_included_rule_keys: string[];
  unenabled_rule_keys: string[];
  materials: Array<{ material_key: string; name: string; required: boolean; material_type: string }>;
  field_count: number;
  unconfigured_check_nodes: string[];
}

export interface TaskPlanInfo {
  plan_code: string;
  plan_name: string;
  version_no: number;
  scenario_version_no: number;
  published_at: string;
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
