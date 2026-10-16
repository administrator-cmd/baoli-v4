import type {
  JsonValue,
  Paged,
  PlanAdjustments,
  PlanConfig,
  PlanConfigPayload,
  PlanListItem,
  PlanSelection,
  TaskCreatedResult,
} from "./contracts";
import { request } from "./http";
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

/** 审核方案分页列表。 */
export function listPlans(query: PlanQuery): Promise<Paged<PlanListItem>> {
  return request<Paged<PlanListItem>>(`/plans${toQuery({ ...query })}`);
}

export interface PlanOption {
  plan_code: string;
  plan_name: string;
  scenario_code: string;
  scenario_version_no: number;
}

/** 方案下拉选项，可按场景编码过滤。 */
export function listPlanOptions(scenarioCode?: string): Promise<{ request_id: string; items: PlanOption[] }> {
  return request<{ request_id: string; items: PlanOption[] }>(`/plans/options${toQuery({ scenario_code: scenarioCode })}`);
}

/**
 * 创建方案：把那一版场景的配置整份复制成方案自己的配置，并立即编译一次。
 *
 * `scenario_version_no` 必须是页面看到的那一版：服务端在事务内核对它仍是允许创建方案的
 * 已发布版本，不一致时返回 `PLAN_VERSION_CHANGED`（刷新后重试），不会静默改取最新版。
 * 不再提交规则选择 —— 创建时整份复制，规则在方案里逐条勾。
 */
export function createPlan(payload: {
  plan_name: string;
  scenario_code: string;
  scenario_version_no: number;
  description?: string | null;
  enabled: boolean;
}): Promise<PlanSelection> {
  return request<PlanSelection>("/plans", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}

/** 方案详情：场景版本来源与方案自己的规则启停。 */
export function getPlan(planCode: string): Promise<PlanSelection> {
  return request<PlanSelection>(`/plans/${encodeURIComponent(planCode)}`);
}

/** 编辑方案基本资料；不修改规则范围。 */
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

/** 启用 / 停用方案；已停用的方案不能用于创建任务。 */
export function setPlanEnabled(planCode: string, enabled: boolean): Promise<PlanSelection> {
  return request<PlanSelection>(`/plans/${encodeURIComponent(planCode)}/enabled`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ enabled }),
  });
}

/** 逻辑删除方案；已创建任务保留自己的冻结配置。 */
export function deletePlan(planCode: string): Promise<PlanSelection> {
  return request<PlanSelection>(`/plans/${encodeURIComponent(planCode)}`, { method: "DELETE" });
}

/** 恢复已逻辑删除的方案。 */
export function restorePlan(planCode: string): Promise<PlanSelection> {
  return request<PlanSelection>(`/plans/${encodeURIComponent(planCode)}/restore`, { method: "POST" });
}

/** 复制方案为新方案（沿用原方案的绑定与规则选择）。 */
export function copyPlan(planCode: string, planName: string | null): Promise<PlanSelection> {
  return request<PlanSelection>(`/plans/${encodeURIComponent(planCode)}/copy`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ plan_name: planName }),
  });
}

/** 方案的可编辑配置：材料 / 字段 / 审核规则 / 完整计算定义 / 报告章节。 */
export function getPlanConfig(planCode: string): Promise<PlanConfig> {
  return request<PlanConfig>(`/plans/${encodeURIComponent(planCode)}/config`);
}

/** 旧方案相对其来源场景版本的调整，供重新生成时参考。 */
export function getPlanAdjustments(planCode: string): Promise<PlanAdjustments> {
  return request<PlanAdjustments>(`/plans/${encodeURIComponent(planCode)}/adjustments`);
}

/** 整体替换方案的配置并立即重新编译。提交的是五份完整集合。 */
export function updatePlanConfig(planCode: string, payload: PlanConfigPayload): Promise<PlanConfig> {
  return request<PlanConfig>(`/plans/${encodeURIComponent(planCode)}/config`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}

/** 使用方案创建审核任务（幂等：同一标识重发返回原任务）。 */
export function createTaskFromPlan(
  planCode: string,
  payload: {
    task_name: string;
    remark?: string | null;
    /** 页面预览到的方案范围指纹；服务端锁定方案后比对，不一致时拒绝创建。 */
    expected_scope_hash: string;
    idempotency_key: string;
  },
): Promise<TaskCreatedResult> {
  return request<TaskCreatedResult>(`/plans/${encodeURIComponent(planCode)}/tasks`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}

export type { JsonValue };
