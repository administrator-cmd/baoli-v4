/**
 * 管理端列表：材料、上传、证据、执行记录、复核记录与操作日志。
 *
 * 契约按业务域拆分：本文件只声明本域的载荷形状，跨域引用一律显式 import，
 * 不再让所有页面共同依赖一个不断扩大的中心文件。
 */

import type { ApiResponse, JsonValue } from "./common";
import type { TaskReviewJudgment } from "./audits";

export interface DocumentRow {
  document_no: string;
  task_no: string;
  task_name: string;
  material_key: string;
  material_name: string;
  required: boolean;
  status: string;
  upload_no: string | null;
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

export interface EvidenceRow {
  evidence_no: string;
  task_no: string;
  task_name: string;
  reference_type: "FIELD_FACT" | "RULE_RESULT" | "ARTIFACT";
  object_name: string | null;
  material_name: string | null;
  filename: string | null;
  document_upload_no: string | null;
  page_no: number | null;
  quote_text: string;
  locator_status: "VERIFIED" | "UNVERIFIED" | "UNLOCATABLE";
  locator_note: string | null;
  historical: boolean;
  created_at: string;
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

export interface ReviewRow {
  review_no: string;
  task_no: string;
  task_name: string;
  scenario_name: string;
  review_type: string;
  object_name: string;
  status: string;
  system_judgment_status: string | null;
  system_judgment_message: string | null;
  decision: string | null;
  effective_status: string | null;
  basis_type: string | null;
  before_value: string | null;
  after_value: string | null;
  task_status: string | null;
  task_current_node: string | null;
  updated_at: string;
}

export interface TaskReviewRow {
  review_no: string;
  review_type: string;
  object_name: string;
  object_kind: "RULE" | "FIELD" | "MATERIAL" | "UNKNOWN";
  status: string;
  system_judgment: TaskReviewJudgment;
  decision: string | null;
  business_note: string | null;
  before_value: string | null;
  after_value: string | null;
  basis_type: string | null;
  decision_command_no: string | null;
  created_at: string;
  updated_at: string;
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
