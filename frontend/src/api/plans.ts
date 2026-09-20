import type {
  JsonValue,
  Paged,
  PlanListItem,
  PlanScopePreview,
  PlanSelection,
  TaskCreatedResult,
} from "./contracts";
import { apiFileUrl, request } from "./http";
import { toQuery } from "./query";

export interface PlanQuery {
  name?: string;
  scenario_code?: string;
  enabled?: boolean;
  effective?: boolean;
  deleted?: boolean;
  page: number;
  page_size: number;
}

export function listPlans(query: PlanQuery): Promise<Paged<PlanListItem>> {
  return request<Paged<PlanListItem>>(`/plans${toQuery({ ...query })}`);
}

export function planExportUrl(query: Omit<PlanQuery, "page" | "page_size">): string {
  return apiFileUrl(`/plans/export${toQuery({ ...query })}`);
}

export interface PlanOption {
  plan_code: string;
  plan_name: string;
  scenario_code: string;
  scenario_version_no: number;
}

export function listPlanOptions(scenarioCode?: string): Promise<{ request_id: string; items: PlanOption[] }> {
  return request<{ request_id: string; items: PlanOption[] }>(`/plans/options${toQuery({ scenario_code: scenarioCode })}`);
}

export function createPlan(payload: {
  plan_name: string;
  scenario_code: string;
  description?: string | null;
  enabled: boolean;
}): Promise<PlanSelection> {
  return request<PlanSelection>("/plans", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}

export function getPlan(planCode: string): Promise<PlanSelection> {
  return request<PlanSelection>(`/plans/${encodeURIComponent(planCode)}`);
}

export function updatePlan(
  planCode: string,
  payload: { plan_name?: string; description?: string | null; enabled?: boolean },
): Promise<PlanSelection> {
  return request<PlanSelection>(`/plans/${encodeURIComponent(planCode)}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}

export function setPlanEnabled(planCode: string, enabled: boolean): Promise<PlanSelection> {
  return request<PlanSelection>(`/plans/${encodeURIComponent(planCode)}/enabled`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ enabled }),
  });
}

export function deletePlan(planCode: string): Promise<PlanSelection> {
  return request<PlanSelection>(`/plans/${encodeURIComponent(planCode)}`, { method: "DELETE" });
}

export function restorePlan(planCode: string): Promise<PlanSelection> {
  return request<PlanSelection>(`/plans/${encodeURIComponent(planCode)}/restore`, { method: "POST" });
}

export function copyPlan(planCode: string, planName: string | null): Promise<PlanSelection> {
  return request<PlanSelection>(`/plans/${encodeURIComponent(planCode)}/copy`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ plan_name: planName }),
  });
}

/** 保存方案（绑定场景版本 + 显式规则选择）并立即重算范围；返回被移除的旧选择。 */
export function savePlan(
  planCode: string,
  payload: { rule_keys: string[] },
): Promise<PlanSelection> {
  return request<PlanSelection>(`/plans/${encodeURIComponent(planCode)}/save`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}

/** 范围预览：显式选择 + 自动补入依赖 + 未启用规则（保存前就能看到）。 */
export function previewPlanScope(payload: {
  scenario_code: string;
  rule_keys: string[] | null;
}): Promise<PlanScopePreview> {
  return request<PlanScopePreview>("/plans/scope-preview", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}

/** 使用方案创建审核任务（幂等：同一标识重发返回原任务）。 */
export function createTaskFromPlan(
  planCode: string,
  payload: { task_name: string; remark?: string | null; idempotency_key: string },
): Promise<TaskCreatedResult> {
  return request<TaskCreatedResult>(`/plans/${encodeURIComponent(planCode)}/tasks`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}

/** 把旧任务的执行范围显式另存为方案。 */
export function saveTaskRangeAsPlan(payload: { task_no: string; plan_name: string }): Promise<PlanSelection> {
  return request<PlanSelection>("/plans/from-task", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}

export type { JsonValue };
