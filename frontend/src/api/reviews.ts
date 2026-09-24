import type { ExecutionDetail, ExecutionRow, Paged, ReviewRow } from "./contracts";
import { apiFileUrl, request } from "./http";
import { toQuery } from "./query";

export interface ReviewQuery {
  task_no?: string;
  review_type?: string;
  status?: string;
  updated_from?: string;
  updated_to?: string;
  page: number;
  page_size: number;
}

/** 人工复核分页列表（跨任务）。 */
export function listReviews(query: ReviewQuery): Promise<Paged<ReviewRow>> {
  return request<Paged<ReviewRow>>(`/reviews${toQuery({ ...query })}`);
}

/** 人工复核列表导出地址；与当前筛选同一作用域。 */
export function reviewExportUrl(query: Omit<ReviewQuery, "page" | "page_size">): string {
  return apiFileUrl(`/reviews/export${toQuery({ ...query })}`);
}

export interface ExecutionQuery {
  task_no?: string;
  node_code?: string;
  status?: string;
  started_from?: string;
  started_to?: string;
  page: number;
  page_size: number;
}

/** 节点执行记录分页列表（跨任务）。 */
export function listExecutions(query: ExecutionQuery): Promise<Paged<ExecutionRow>> {
  return request<Paged<ExecutionRow>>(`/executions${toQuery({ ...query })}`);
}

/** 执行记录导出地址；与当前筛选同一作用域。 */
export function executionExportUrl(query: Omit<ExecutionQuery, "page" | "page_size">): string {
  return apiFileUrl(`/executions/export${toQuery({ ...query })}`);
}

/** 节点执行详情：输入快照来源、结果与错误。 */
export function getExecution(executionId: number): Promise<ExecutionDetail> {
  return request<ExecutionDetail>(`/executions/${executionId}`);
}
