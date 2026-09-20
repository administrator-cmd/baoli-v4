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

export function listReviews(query: ReviewQuery): Promise<Paged<ReviewRow>> {
  return request<Paged<ReviewRow>>(`/reviews${toQuery({ ...query })}`);
}

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

export function listExecutions(query: ExecutionQuery): Promise<Paged<ExecutionRow>> {
  return request<Paged<ExecutionRow>>(`/executions${toQuery({ ...query })}`);
}

export function executionExportUrl(query: Omit<ExecutionQuery, "page" | "page_size">): string {
  return apiFileUrl(`/executions/export${toQuery({ ...query })}`);
}

export function getExecution(executionId: number): Promise<ExecutionDetail> {
  return request<ExecutionDetail>(`/executions/${executionId}`);
}
