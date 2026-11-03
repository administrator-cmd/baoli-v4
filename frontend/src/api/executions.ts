import type { ExecutionRow, Paged } from "./contracts";
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
