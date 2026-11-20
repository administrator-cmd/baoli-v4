/** 通用响应包装与分页：所有接口共用，不承载业务字段。 */

export type JsonValue = string | number | boolean | null | JsonValue[] | { [key: string]: JsonValue };

/** 所有 JSON 接口的统一信封；失败时 data 承载已脱敏的 details。 */
export interface ApiEnvelope<T> {
  code: string;
  message: string;
  data: T | null;
  request_id: string;
  success: boolean;
}

export interface ApiFailure {
  code: string;
  message: string;
  data: Record<string, JsonValue> | null;
  request_id: string;
  success: boolean;
}

export type OperationStatus = "PENDING" | "RUNNING" | "SUCCEEDED" | "FAILED" | "INTERRUPTED";

export interface OperationAccepted {
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
  /** 规则库模型原始返回文件，相对于后端 data 根目录。 */
  failure_code: string | null;
  failure_message: string | null;
  /** 命令当前阶段：区分已保存未执行 / 外部已受理 / 结果未知 / 业务已提交但图未推进 / 结果已就绪等。 */
  phase?: string | null;
  /** 外部作业引用（例如 OCR jobId）；本地超时后只能续查同一个作业。 */
  external_job_ref?: string | null;
  created_at: string | null;
  started_at: string | null;
  finished_at: string | null;
}

export interface Paged<T> {
  total: number;
  page: number;
  page_size: number;
  items: T[];
}
