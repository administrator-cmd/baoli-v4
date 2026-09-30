/** 通用响应包装与分页：所有接口共用，不承载业务字段。 */

export type JsonValue = string | number | boolean | null | JsonValue[] | { [key: string]: JsonValue };

export interface ApiFailure {
  code: string;
  message: string;
  details: Record<string, JsonValue>;
  request_id: string;
}

export interface ApiResponse {
  request_id: string;
}

export type OperationStatus = "PENDING" | "RUNNING" | "SUCCEEDED" | "FAILED" | "INTERRUPTED";

export interface OperationAccepted extends ApiResponse {
  operation_no: string;
  object_no: string;
  object_type: string;
  operation_type: string;
  target_node: string | null;
  input_version: string | null;
  status: OperationStatus;
  message: string;
  reused: boolean;
  result: Record<string, JsonValue> | null;
  failure_code: string | null;
  failure_message: string | null;
  /** 命令当前阶段：区分已保存未执行 / 外部已受理 / 结果未知 / 业务已提交但图未推进等。 */
  phase?: string | null;
  /** 外部作业引用（例如 OCR jobId）；本地超时后只能续查同一个作业。 */
  external_job_ref?: string | null;
  created_at: string | null;
  started_at: string | null;
  finished_at: string | null;
}

export interface Paged<T> extends ApiResponse {
  total: number;
  page: number;
  page_size: number;
  items: T[];
}
