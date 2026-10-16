import type { ExecutionDetail, ExecutionRow, Paged } from "./contracts";
import { request } from "./http";
import { toQuery } from "./query";

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

/** 节点执行详情：输入快照来源、结果与错误。 */
export function getExecution(executionId: number): Promise<ExecutionDetail> {
  return request<ExecutionDetail>(`/executions/${executionId}`);
}
