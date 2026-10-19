import type {
  AuditField,
  AuditNode,
  AuditTask,
  AuditTaskRow,
  DocumentRemoveResult,
  DocumentUploadResponse,
  Evidence,
  ExecutionRow,
  FieldCorrectionRequest,
  FieldCorrectionResponse,
  JsonValue,
  OperationAccepted,
  Paged,
  Report,
  RiskList,
} from "./contracts";
import { request, uploadWithProgress, type OperationView } from "./http";
import { toQuery } from "./query";
/**
 * 字段结果视图。契约文件不改写，这里只补 contracts.ts 尚未声明的字段。
 * 都是服务端已持久化的事实，不是前端推断的。
 */
export type AuditFieldView = AuditField & {
  /** 本任务是否启用该字段的自动抽取，独立于已有人工或历史有效值。 */
  extraction_enabled?: boolean;
  material_uploaded?: boolean;
  material_required?: boolean;
  field_key?: string | null;
  material_key?: string | null;
  fact_revision_no?: number | null;
  /** 事实来源：人工纠错 / 文档抽取。 */
  origin?: string | null;
  /** 抽取规则没有形成字段值时保存的真实执行原因。 */
  extraction_message?: string | null;
  extraction_presentation_version?: number | null;
};
/** 只读 OCR 状态查询结果。 */
export interface DocumentOcrStatus {
  document_no: string;
  artifact_no: string | null;
  status: string;
  job_id: string | null;
  deadline_at: string | null;
  provider_state: string | null;
  extract_progress: { totalPages?: number; extractedPages?: number; [key: string]: unknown } | null;
  progress_updated_at: string | null;
  message: string;
}
/** 当前上传文件的同步处理产物状态。 */
export interface DocumentArtifactStatus {
  document_no: string;
  artifact_type: "MATERIAL_CLASSIFY" | "DOCUMENT_PARSE" | "SEAL";
  artifact_no: string | null;
  status: string;
  message: string;
}
/**
 * 节点视图。契约文件不改写，这里只补 contracts.ts 尚未声明的字段。
 */
export type AuditNodeView = AuditNode & {
  failure_code?: string | null;
  result_summary?: Record<string, JsonValue> | null;
};
/** 任务视图：nodes / fields 换成本文件的 View 形状，其余字段沿用契约。 */
export type AuditTaskView = AuditTask & {
  latest_operation?: OperationView | null;
  nodes: AuditNodeView[];
  fields: AuditFieldView[];

};
/** 替换上传的真实影响范围：由服务端计算，页面只负责显示。 */
export interface ReplacementImpact {
  affected_artifacts: number;
  stale_field_values: number;
  invalidated_rule_results: number;
  invalidated_node_executions: number;
  invalidated_reviews: number;
  reset_to_node: string | null;
  description: string;
}
export type DocumentUploadView = DocumentUploadResponse & {
  impact?: ReplacementImpact | null;
};
/** 审核任务列表查询（真实后端筛选/排序/分页）。 */
export interface TaskQuery {
  name?: string;
  scenario_code?: string;
  plan_code?: string;
  status?: string;
  current_node?: string;
  created_from?: string;
  created_to?: string;
  page: number;
  page_size: number;
}
/** 审核任务分页列表（后端筛选 / 排序 / 分页）。 */
export function listAuditTasksPage(query: TaskQuery): Promise<Paged<AuditTaskRow>> {
  return request<Paged<AuditTaskRow>>(`/audit-tasks${toQuery({ ...query })}`);
}
/** 编辑任务基本资料（名称、备注）；执行快照不随重命名变化。 */
export function updateAuditTask(taskNo: string, payload: { task_name?: string; remark?: string | null }): Promise<AuditTaskView> {
  return request<AuditTaskView>(`/audit-tasks/${encodeURIComponent(taskNo)}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}
/**
 * 复制为新任务：按源任务所属方案的当前范围重新创建。
 *
 * 不是保留原任务的执行快照——新任务冻结的是方案此刻的范围，材料与结果重新开始。
 * `idempotencyKey` 与新建任务同一口径：响应丢失后重发返回原任务。
 */
export function copyAuditTask(taskNo: string, taskName: string, idempotencyKey: string): Promise<AuditTaskView> {
  return request<AuditTaskView>(`/audit-tasks/${encodeURIComponent(taskNo)}/copy`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ task_name: taskName, idempotency_key: idempotencyKey }),
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
export function getAuditTask(taskNo: string): Promise<AuditTaskView> {
  return request<AuditTaskView>(`/audit-tasks/${encodeURIComponent(taskNo)}`);
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
): Promise<DocumentUploadView> {
  const formData = new FormData();
  formData.append("file", file);
  formData.append("material_type", materialType);
  formData.append("replace", String(replace));
  return uploadWithProgress<DocumentUploadView>(
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
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ node_code: nodeCode, idempotency_key: idempotencyKey, input_revision: inputRevision }),
  });
}
/** 恢复因审核检查点缺失而终止的当前节点；恢复后仍需用户重新执行节点。 */
/**
 * 字段事实纠错。PRESENT 错值与 NOT_PRESENT 漏值都能由用户主动发起；
 * 服务端会返回本次纠错使哪些复核、规则结果失效并需要重算。
 */
export function submitFieldCorrections(taskNo: string, payload: FieldCorrectionRequest): Promise<FieldCorrectionResponse> {
  return request<FieldCorrectionResponse>(`/audit-tasks/${encodeURIComponent(taskNo)}/field-corrections`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
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
  payload: { artifact_no?: string | null; input_revision: number; idempotency_key: string; resubmit_unknown: boolean },
): Promise<OperationAccepted> {
  return request<OperationAccepted>(
    `/audit-tasks/${encodeURIComponent(taskNo)}/documents/${encodeURIComponent(documentNo)}/ocr/retry`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    },
  );
}
/** 只重试一份材料的校验、去章或印章识别。 */
export function retryDocumentArtifact(
  taskNo: string,
  documentNo: string,
  artifactType: DocumentArtifactStatus["artifact_type"],
  payload: { artifact_no: string; input_revision: number; idempotency_key: string; resubmit_unknown: boolean },
): Promise<OperationAccepted> {
  return request<OperationAccepted>(
    `/audit-tasks/${encodeURIComponent(taskNo)}/documents/${encodeURIComponent(documentNo)}/artifacts/${encodeURIComponent(artifactType)}/retry`,
    { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) },
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
/** 单条证据详情：引用原文、来源、字段属性与内容核对结论。 */
export function getTaskEvidence(taskNo: string, evidenceNo: string): Promise<Evidence> {
  return request<Evidence>(`/audit-tasks/${encodeURIComponent(taskNo)}/evidence/${encodeURIComponent(evidenceNo)}`);
}
