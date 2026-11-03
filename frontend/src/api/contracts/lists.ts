/** 管理端列表：材料、上传、证据、执行记录、复核记录与操作日志。 */

import type { JsonValue } from "./common";

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

export interface DocumentDetail {
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

export interface ExecutionProgress {
  /** 本批次已经产生规则结果的数量。 */
  executed: number;
  /** 本批次规则结果中标记为执行阻断的数量。 */
  blocked: number;
  /** 计划内没有产生结果、尚未执行的数量。 */
  not_executed: number;
}

export interface ExecutionModelTurns {
  /** 按 evaluation_id 去重后的模型请求轮次；历史数据无法还原时为 null。 */
  count: number | null;
  available: boolean;
}

export interface ExecutionBlockReason {
  code: string | null;
  /** 由错误码或结构化字段生成的简短原因；完整原因由接口的 failure_message 保留。 */
  text: string;
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
  progress: ExecutionProgress;
  model_turns: ExecutionModelTurns;
  /** 当前仍属于本批次的规则级阻断数量；已失效或等待字段核对的旧阻断不计入。 */
  current_blocked: number;
  /** 列表与顶部提示用的简短阻断原因；等待核对、执行中与已失效批次为空。 */
  block_reason: ExecutionBlockReason | null;
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
  failure_code: string | null;
  failure_message: string | null;
  created_at: string;
  finished_at: string | null;
}
