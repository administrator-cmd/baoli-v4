import type {
  AuditTask,
  AuditTaskUpdateRequest,
  AuditDocumentUploadResponse,
  DocumentArtifactStatus,
  DocumentOcrStatus,
  DocumentArtifactRetryRequest,
  DocumentOcrRetryRequest,
  FieldReviewSaveRequest,
  FieldReviewConfirmRequest,
  TaskQuery,
  AuditTaskRow,
  DocumentRemoveResult,
  ExecutionRow,
  FieldCorrectionRequest,
  FieldCorrectionResponse,
  FieldReviewSaveResult,
  OperationAccepted,
  Paged,
  Report,
  RiskList,
} from "./contracts";
import { request, uploadWithProgress } from "./http";
import { toQuery } from "./query";
/** 审核任务分页列表（后端筛选 / 排序 / 分页）。 */
export function listAuditTasksPage(query: TaskQuery): Promise<Paged<AuditTaskRow>> {
  return request<Paged<AuditTaskRow>>(`/audit-tasks${toQuery({ ...query })}`);
}
/** 编辑任务基本资料（名称、备注）；执行快照不随重命名变化。 */
export function updateAuditTask(taskNo: string, payload: AuditTaskUpdateRequest): Promise<AuditTask> {
  return request<AuditTask>(`/audit-tasks/${encodeURIComponent(taskNo)}`, {
    method: "PATCH",
    data: payload,
  });
}
/** 永久删除任务及关联记录、文件。 */
export function deleteAuditTask(taskNo: string): Promise<void> {
  return request<void>(`/audit-tasks/${encodeURIComponent(taskNo)}`, { method: "DELETE" });
}
/** 移除材料位的当前文件（输入修订：旧文件保留历史，依赖结果失效）。 */
export function removeTaskDocumentFile(taskNo: string, documentNo: string): Promise<DocumentRemoveResult> {
  return request<DocumentRemoveResult>(
    `/audit-tasks/${encodeURIComponent(taskNo)}/documents/${encodeURIComponent(documentNo)}/remove`,
    { method: "POST" },
  );
}
/** 任务内的人工复核记录（含已复核与已失效）。 */
/** 任务内的执行记录（服务端分页）。 */
export function listTaskExecutions(taskNo: string, page = 1, pageSize = 50): Promise<Paged<ExecutionRow>> {
  return request<Paged<ExecutionRow>>(
    `/audit-tasks/${encodeURIComponent(taskNo)}/executions${toQuery({ page, page_size: pageSize })}`,
  );
}
/** 任务详情：节点、材料位、字段、规则与待办。 */
export function getAuditTask(taskNo: string): Promise<AuditTask> {
  return request<AuditTask>(`/audit-tasks/${encodeURIComponent(taskNo)}`);
}
export interface FieldReviewHistoryItem {
  confirmation_no: string;
  task_no: string;
  task_name: string;
  input_revision: number;
  missing_field_count: number;
  missing_field_keys: string[];
  note: string | null;
  operation_no: string | null;
  created_at: string;
  snapshot: {
    fields?: Array<{
      field_key: string;
      field_name: string;
      material_key: string;
      material_name: string;
      status: string | null;
      raw_value: string | null;
      value: unknown;
      unit: string | null;
      period_scope: string | null;
      statement_scope: string | null;
      fact_revision_no: number | null;
      candidate_revision_no: number | null;
      upload_id: number | null;
      upload_filename: string | null;
      upload_no: string | null;
      evidence: Array<Record<string, unknown>>;
    }>;
    missing_fields?: Array<Record<string, unknown>>;
  } | null;
}
export interface FieldReviewHistoryTaskItem {
  task_no: string;
  task_name: string;
  confirmation_count: number;
  latest_confirmation_no: string;
  latest_confirmed_at: string;
  latest_missing_field_count: number;
}
export function listFieldReviewHistory(page = 1, pageSize = 20): Promise<Paged<FieldReviewHistoryTaskItem>> {
  return request<Paged<FieldReviewHistoryTaskItem>>(
    `/audit-tasks/manual-review/history${toQuery({ page, page_size: pageSize })}`,
  );
}
export async function listTaskFieldReviewHistory(taskNo: string): Promise<FieldReviewHistoryItem[]> {
  const result = await request<{ items: FieldReviewHistoryItem[] }>(
    `/audit-tasks/${encodeURIComponent(taskNo)}/manual-review/history`,
  );
  return result.items;
}
/**
 * 上传或替换材料文件，并回报浏览器真实的上传进度。
 *
 * materialType 必填：用户从 8018 支持目录中选择的材料类型，随本次上传保存。
 * 材料位不声明类型，替换文件时可以换一个类型。
 */
export function uploadTaskDocument(
  taskNo: string,
  documentNo: string,
  file: File,
  materialType: string,
  replace = false,
  onProgress?: (percent: number) => void,
): Promise<AuditDocumentUploadResponse> {
  const formData = new FormData();
  formData.append("file", file);
  formData.append("material_type", materialType);
  formData.append("replace", String(replace));
  return uploadWithProgress<AuditDocumentUploadResponse>(
    `/audit-tasks/${encodeURIComponent(taskNo)}/documents/${encodeURIComponent(documentNo)}/upload`,
    formData,
    onProgress,
  );
}
/**
 * 执行当前业务节点，或手动重试失败的审核。长处理只受理不等待：返回 202 语义的受理状态，页面再用
 * operation_no 查询真实执行状态。幂等标识与任务、节点绑定，换节点不会复用。
 * input_revision 提交页面看到的任务输入修订，服务端据此拒绝过期命令。
 * 新一轮重试使用新幂等键；响应丢失后的同一次请求仍复用原键。
 */
export function executeAuditNode(
  taskNo: string,
  nodeCode: string,
  idempotencyKey: string,
  inputRevision?: number,
): Promise<OperationAccepted> {
  return request<OperationAccepted>(`/audit-tasks/${encodeURIComponent(taskNo)}/execute`, {
    method: "POST",
    data: { node_code: nodeCode, idempotency_key: idempotencyKey, input_revision: inputRevision },
  });
}
/** 从中断的执行批次继续：已完成的规则保留，只执行没有结果的规则。 */
export function resumeAuditTask(
  taskNo: string,
  idempotencyKey: string,
  inputRevision?: number,
): Promise<OperationAccepted> {
  return request<OperationAccepted>(`/audit-tasks/${encodeURIComponent(taskNo)}/resume`, {
    method: "POST",
    data: { idempotency_key: idempotencyKey, input_revision: inputRevision },
  });
}
/**
 * 字段事实纠错。PRESENT 错值与 NOT_PRESENT 漏值都能由用户主动发起；
 * 服务端会返回本次纠错使哪些复核、规则结果失效并需要重算。
 */
export function submitFieldCorrections(taskNo: string, payload: FieldCorrectionRequest): Promise<FieldCorrectionResponse> {
  return request<FieldCorrectionResponse>(`/audit-tasks/${encodeURIComponent(taskNo)}/field-corrections`, {
    method: "POST",
    data: payload,
  });
}
/**
 * 只保存字段核对期间的修改。任务仍停在等待核对，不触发后续规则；
 * input_revision 与每个字段的 expected_revision_no 由服务端核对，旧页面会被拒绝。
 */
export function saveFieldReview(
  taskNo: string,
  payload: FieldReviewSaveRequest,
): Promise<FieldReviewSaveResult> {
  return request<FieldReviewSaveResult>(`/audit-tasks/${encodeURIComponent(taskNo)}/field-review/save`, {
    method: "POST",
    data: payload,
  });
}
/**
 * 确认当前字段版本并继续审核。字段修改与确认在后台命令里先提交，再执行核对后的规则；
 * 返回 202 只代表已受理，真实结果用 operation_no 查询。
 */
export function confirmFieldReview(
  taskNo: string,
  payload: FieldReviewConfirmRequest,
): Promise<OperationAccepted> {
  return request<OperationAccepted>(`/audit-tasks/${encodeURIComponent(taskNo)}/field-review/confirm`, {
    method: "POST",
    data: payload,
  });
}
/** 只读查询某份材料的 OCR 处理状态；用于「作业过期 / 状态未知」时核对真实情况。 */
export function getDocumentOcr(taskNo: string, documentNo: string): Promise<DocumentOcrStatus> {
  return request<DocumentOcrStatus>(
    `/audit-tasks/${encodeURIComponent(taskNo)}/documents/${encodeURIComponent(documentNo)}/ocr`,
  );
}
/** 只读查询 8018/8010/8003 在当前上传文件上的真实产物状态。 */
export function getDocumentArtifact(
  taskNo: string,
  documentNo: string,
  artifactType: DocumentArtifactStatus["artifact_type"],
): Promise<DocumentArtifactStatus> {
  return request<DocumentArtifactStatus>(
    `/audit-tasks/${encodeURIComponent(taskNo)}/documents/${encodeURIComponent(documentNo)}/artifacts/${encodeURIComponent(artifactType)}`,
  );
}
/** 只续查/重试一份材料的 OCR；结果未知时需要用户明确确认重新提交。 */
export function retryDocumentOcr(
  taskNo: string,
  documentNo: string,
  payload: DocumentOcrRetryRequest,
): Promise<OperationAccepted> {
  return request<OperationAccepted>(
    `/audit-tasks/${encodeURIComponent(taskNo)}/documents/${encodeURIComponent(documentNo)}/ocr/retry`,
    {
      method: "POST",
      data: payload,
    },
  );
}
/** 只重试一份材料的校验、去章或印章识别。 */
export function retryDocumentArtifact(
  taskNo: string,
  documentNo: string,
  artifactType: DocumentArtifactStatus["artifact_type"],
  payload: DocumentArtifactRetryRequest,
): Promise<OperationAccepted> {
  return request<OperationAccepted>(
    `/audit-tasks/${encodeURIComponent(taskNo)}/documents/${encodeURIComponent(documentNo)}/artifacts/${encodeURIComponent(artifactType)}/retry`,
    { method: "POST", data: payload },
  );
}

/** 该任务的风险结果（命中只统计 ABNORMAL）。 */
export function getTaskRisks(taskNo: string): Promise<RiskList> {
  return request<RiskList>(`/audit-tasks/${encodeURIComponent(taskNo)}/risks`);
}
/** 该任务的审核报告正文与三信号。 */
export function getTaskReport(taskNo: string, reportNo?: string): Promise<Report> {
  return request<Report>(`/audit-tasks/${encodeURIComponent(taskNo)}/reports${toQuery({ report_no: reportNo })}`);
}
