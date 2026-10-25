/** 管理端列表：材料、上传、证据、执行记录、复核记录与操作日志。 */

import type { ApiResponse, JsonValue } from "./common";

export interface DocumentRow {
  document_no: string;
  task_no: string;
  task_name: string;
  material_key: string;
  material_name: string;
  required: boolean;
  status: string;
  filename: string | null;
  size_bytes: number | null;
  uploaded_at: string | null;
  version_no: number | null;
  version_count: number | null;
}

export interface DocumentUploadRow {
  upload_no: string;
  filename: string;
  size_bytes: number;
  content_type: string;
  status: string;
  is_current: boolean;
  version_no: number;
  created_at: string;
}

export interface DocumentDetail extends ApiResponse {
  document_no: string;
  task_no: string | null;
  task_name: string | null;
  material_key: string;
  material_name: string;
  required: boolean;
  status: string;
  current_upload_no: string | null;
  uploads: DocumentUploadRow[];
}

export interface ExecutionRow {
  execution_id: number;
  task_no: string;
  task_name: string;
  scenario_name: string;
  scenario_version_no: number;
  node_code: string;
  node_name: string;
  sequence_no: number;
  status: string;
  /** 是否已被重试、纠错或材料换版取代；不改变本次原执行结局。 */
  invalidated: boolean;
  /** 失效时间与原因；未失效时为空。 */
  invalidated_at: string | null;
  invalidation_reason: string | null;
  started_at: string | null;
  finished_at: string | null;
  duration_ms: number | null;
  failure_code: string | null;
  failure_message: string | null;
  result_summary: Record<string, JsonValue> | null;
}

export interface ExecutionDetail extends ExecutionRow {
  request_id: string;
  task_status: string | null;
  task_current_node: string | null;
  input_revision: number | null;
  plan_bound: boolean | null;
  operation_no: string | null;
  created_at: string;
}

export interface OperationLogRow {
  entry_no: string;
  source: "BUSINESS" | "COMMAND";
  object_type: string;
  object_no: string;
  object_name: string;
  operation_type: string;
  result: "SUCCEEDED" | "FAILED" | "ACCEPTED";
  duration_ms: number | null;
  command_no: string | null;
  detail: Record<string, JsonValue> | null;
  failure_code: string | null;
  failure_message: string | null;
  created_at: string;
  finished_at: string | null;
}
