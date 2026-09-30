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
  OcrReconcileResponse,
  OperationAccepted,
  Paged,
  Report,
  ReviewDraftPayloadItem,
  ReviewDraftResult,
  RetrievalDecision,
  RetrievalDecisionResult,
  RetrievalResult,
  RiskList,
  TaskEvidenceIndex,
  TaskReviewRow,
} from "./contracts";
import { apiFileUrl, request, uploadWithProgress, type OperationView } from "./http";
import { toQuery } from "./query";

/**
 * 字段结果视图。契约文件不在这里改写，只补齐后端已返回、而 contracts.ts 尚未声明的字段：
 * - field_key / material_key：字段纠错接口必须用稳定标识，不能用展示用的中文名；
 * - fact_revision_no / origin / evidence_locator_status：人工纠错与文档抽取要分开呈现，
 *   证据定位未核实要显式标注；
 * - material_uploaded / material_required：页面显示「材料未上传」状态时需要。
 * 这些都是服务端已持久化的事实，不是前端推断出来的。
 */
export type AuditFieldView = AuditField & {
  material_uploaded?: boolean;
  material_required?: boolean;
  field_key?: string | null;
  material_key?: string | null;
  fact_revision_no?: number | null;
  /** 事实来源：人工纠错 / 文档抽取。 */
  origin?: string | null;
  /** 证据定位核对状态：VERIFIED / UNVERIFIED / UNLOCATABLE。 */
  evidence_locator_status?: string | null;
};

/**
 * OCR 对账请求。ADOPT_JOB 用于把结果未知的提交人工绑定到已知 jobId；
 * RETRY_SUBMIT 会明确重新提交外部作业，两者都必须填写理由。
 */
export interface OcrReconcileRequest {
  action: "ADOPT_JOB" | "RETRY_SUBMIT";
  artifact_no: string;
  /** ADOPT_JOB 时填写的供应商作业号。 */
  job_id?: string;
  reason: string;
  idempotency_key: string;
}
/** 只读 OCR 状态查询结果。 */
export interface DocumentOcrStatus {
  document_no: string;
  artifact_no: string | null;
  status: string;
  job_id: string | null;
  deadline_at: string | null;
  message: string;
}
/** 当前上传文件的同步处理产物状态。 */
export interface DocumentArtifactStatus {
  document_no: string;
  artifact_type: "MATERIAL_CLASSIFY" | "DOCUMENT_PARSE";
  artifact_no: string | null;
  status: string;
  message: string;
}

export interface ExternalArtifactReconcileRequest {
  artifact_no: string;
  reason: string;
  idempotency_key: string;
}

export interface ExternalArtifactReconcileResponse {
  document_no: string;
  artifact_type: string;
  artifact_no: string;
  status: string;
  message: string;
}

/**
 * 节点视图。契约文件不在这里改写，只补齐后端已返回、而 contracts.ts 尚未声明的字段：
 * result_summary 与 failure_code 是页面显示节点真实产物、失败原因所需的，
 * 不是前端推断出来的。
 */
export type AuditNodeView = AuditNode & {
  failure_code?: string | null;
  result_summary?: Record<string, JsonValue> | null;
};

/** 当前复核批次的身份（服务端给出，页面只读不自己算）。 */
export interface PendingReviewBatch {
  /** 本批全部待办编号：**页面只能提交这些**。 */
  review_nos: string[];
  count: number;
  /** 本批哈希。它与服务端提交校验、检查点校验用的是同一个值。 */
  hash: string | null;
  /**
   * true 表示本批哈希与进入等待时落库的哈希不一致：待办集合在等待期间被改动过，
   * 页面必须刷新后再提交，不能拿旧批次继续（服务端会以 409 拒绝）。
   */
  stale: boolean;
  review_types: string[];
  /** 排队中的待办总数（含不属于本批的批次）。 */
  all_pending_count: number;
}

/** 任务视图：nodes / fields 换成本文件的 View 形状，其余字段沿用契约。 */
export type AuditTaskView = AuditTask & {
  latest_operation?: OperationView | null;
  nodes: AuditNodeView[];
  fields: AuditFieldView[];
  /**
   * 全部待办（含不属于本批的批次）。`pending_reviews` 是**当前批次**，
   * 只有它允许提交；这个字段只用于展示「还有多少项在排队」。
   */
  all_pending_reviews?: TaskReviewRow[];
  pending_review_batch?: PendingReviewBatch;
  pending_review_hash?: string | null;
  pending_review_count?: number;
};

/** 一条 Agent 工具调用账本记录（只读，后端真实记录）。 */
export interface AgentToolCallRow {
  tool_call_id: string;
  retry_of_tool_call_id: string | null;
  tool_name: string;
  tool_version: string | null;
  status: string;
  evaluation_id: string;
  rule_instance_id: string;
  /** 冻结时的工具目录身份与策略版本：回答「模型当时看到的是哪一版工具定义」。 */
  tool_catalog_version: string;
  tool_catalog_hash: string;
  tool_policy_version: string | null;
  fact_snapshot_ref: string;
  input_snapshot_hash: string;
  caller_identity: string | null;
  arguments: Record<string, JsonValue> | null;
  result: Record<string, JsonValue> | null;
  error: Record<string, JsonValue> | null;
  started_at: string | null;
  completed_at: string | null;
}

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
  legacy?: boolean;
  status?: string;
  current_node?: string;
  created_from?: string;
  created_to?: string;
  deleted?: boolean;
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
 * 复制为新任务：按源任务所属方案的**当前范围**重新创建。
 *
 * 它不是「保留原任务执行快照」——新任务冻结的是方案此刻的范围，材料与结果重新开始。
 * `idempotencyKey` 与新建任务同一口径：响应丢失后重发返回原任务。
 */
export function copyAuditTask(taskNo: string, taskName: string, idempotencyKey: string): Promise<AuditTaskView> {
  return request<AuditTaskView>(`/audit-tasks/${encodeURIComponent(taskNo)}/copy`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ task_name: taskName, idempotency_key: idempotencyKey }),
  });
}

/** 逻辑删除任务；历史事实、文件、证据与报告保留。 */
export function deleteAuditTask(taskNo: string): Promise<AuditTaskView> {
  return request<AuditTaskView>(`/audit-tasks/${encodeURIComponent(taskNo)}`, { method: "DELETE" });
}

/** 恢复已逻辑删除的任务。 */
export function restoreAuditTask(taskNo: string): Promise<AuditTaskView> {
  return request<AuditTaskView>(`/audit-tasks/${encodeURIComponent(taskNo)}/restore`, { method: "POST" });
}

/** 移除材料位的当前文件（输入修订：旧文件保留历史，依赖结果失效）。 */
export function removeTaskDocumentFile(taskNo: string, documentNo: string): Promise<DocumentRemoveResult> {
  return request<DocumentRemoveResult>(
    `/audit-tasks/${encodeURIComponent(taskNo)}/documents/${encodeURIComponent(documentNo)}/remove`,
    { method: "POST" },
  );
}

/** 下载/预览某个上传版本（含历史版本）；preview 时由浏览器自行判断能否预览。 */
export function documentFileUrl(taskNo: string, documentNo: string, uploadNo: string, preview = false): string {
  return apiFileUrl(
    `/audit-tasks/${encodeURIComponent(taskNo)}/documents/${encodeURIComponent(documentNo)}/files/${encodeURIComponent(uploadNo)}/download${preview ? "?preview=true" : ""}`,
  );
}

/** 任务内的人工复核记录（含已复核与已失效）。 */
export function listTaskReviews(taskNo: string): Promise<{ request_id: string; task_no: string; items: TaskReviewRow[] }> {
  return request(`/audit-tasks/${encodeURIComponent(taskNo)}/reviews`);
}

/** 读取当前待办版本下的复核草稿；旧待办版本的草稿不会返回。 */
export function getTaskReviewDrafts(taskNo: string): Promise<ReviewDraftResult> {
  return request<ReviewDraftResult>(`/audit-tasks/${encodeURIComponent(taskNo)}/review-drafts`);
}

/** 保存复核草稿（独立于正式提交的持久化暂存）。 */
export function saveTaskReviewDrafts(taskNo: string, items: ReviewDraftPayloadItem[]): Promise<ReviewDraftResult> {
  return request<ReviewDraftResult>(`/audit-tasks/${encodeURIComponent(taskNo)}/review-drafts`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ items }),
  });
}

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
 * 执行当前业务节点。长处理只受理不等待：返回 202 语义的受理状态，页面再用
 * operation_no 查询真实执行状态。幂等标识与任务、节点绑定，换节点不会复用。
 * input_revision 提交页面看到的任务输入修订，服务端据此拒绝过期命令。
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
export function recoverFailedAuditNode(taskNo: string, nodeCode: string, inputRevision: number): Promise<AuditTaskView> {
  return request<AuditTaskView>(`/audit-tasks/${encodeURIComponent(taskNo)}/recover-failed-node`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ node_code: nodeCode, input_revision: inputRevision }),
  });
}

/** 提交人工复核结论；幂等标识绑定任务与待办集合，重发复用同一后台操作。 */
export function submitManualReviews(
  taskNo: string,
  reviews: Array<{
    review_no: string;
    decision: "CONFIRM_ABNORMAL" | "MARK_NORMAL" | "EVIDENCE_INSUFFICIENT" | "CONFIRM_FIELD" | "MODIFY_FIELD" | "MARK_NOT_PRESENT";
    business_note: string;
    field_value?: string;
    /** 人工结论的依据类型：人工说明 / 文档证据 / 上传记录。 */
    basis_type?: "HUMAN_STATEMENT" | "DOCUMENT_EVIDENCE" | "UPLOAD_RECORD";
  }>,
  idempotencyKey: string,
  inputRevision?: number,
): Promise<OperationAccepted> {
  return request<OperationAccepted>(`/audit-tasks/${encodeURIComponent(taskNo)}/manual-review`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ reviews, idempotency_key: idempotencyKey, input_revision: inputRevision }),
  });
}

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

/** 只读查询 8018/8010 在当前上传文件上的真实产物状态。 */
export function getDocumentArtifact(
  taskNo: string,
  documentNo: string,
  artifactType: DocumentArtifactStatus["artifact_type"],
): Promise<DocumentArtifactStatus> {
  return request<DocumentArtifactStatus>(
    `/audit-tasks/${encodeURIComponent(taskNo)}/documents/${encodeURIComponent(documentNo)}/artifacts/${encodeURIComponent(artifactType)}`,
  );
}

/** 人工确认同步外部调用结果未知后，解除当前占位以重新执行节点。 */
export function reconcileDocumentArtifact(
  taskNo: string,
  documentNo: string,
  artifactType: DocumentArtifactStatus["artifact_type"],
  payload: ExternalArtifactReconcileRequest,
): Promise<ExternalArtifactReconcileResponse> {
  return request<ExternalArtifactReconcileResponse>(
    `/audit-tasks/${encodeURIComponent(taskNo)}/documents/${encodeURIComponent(documentNo)}/artifacts/${encodeURIComponent(artifactType)}/reconcile`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    },
  );
}

/** OCR 对账：绑定已知 jobId 继续查询，或明确重新提交外部作业。 */
export function reconcileDocumentOcr(
  taskNo: string,
  documentNo: string,
  payload: OcrReconcileRequest,
): Promise<OcrReconcileResponse> {
  return request<OcrReconcileResponse>(
    `/audit-tasks/${encodeURIComponent(taskNo)}/documents/${encodeURIComponent(documentNo)}/ocr/reconcile`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    },
  );
}

/** 该任务的风险结果（命中只统计 ABNORMAL）。 */
export function getTaskRisks(taskNo: string): Promise<RiskList> {
  return request<RiskList>(`/audit-tasks/${encodeURIComponent(taskNo)}/risks`);
}

/** 该任务的审核报告正文与三信号。 */
export function getTaskReport(taskNo: string): Promise<Report> {
  return request<Report>(`/audit-tasks/${encodeURIComponent(taskNo)}/reports`);
}

/** 单条证据详情：引用原文与定位核对结论。 */
export function getTaskEvidence(taskNo: string, evidenceNo: string): Promise<Evidence> {
  return request<Evidence>(`/audit-tasks/${encodeURIComponent(taskNo)}/evidence/${encodeURIComponent(evidenceNo)}`);
}


/**
 * 用待核对主体在当前任务、给定材料范围内检索相似印章主体，返回候选。
 *
 * 检索范围由服务端严格限定在本任务、材料范围、当前上传版本、产物内容版本与当前模型内；
 * 相似度只用于候选排序，不构成主体一致的结论，结论由 submitRetrievalDecision 单独提交。
 */
export function createTaskRetrieval(
  taskNo: string,
  payload: { query_text: string; material_keys: string[]; rule_key?: string; top_k?: number },
): Promise<RetrievalResult> {
  return request<RetrievalResult>(`/audit-tasks/${encodeURIComponent(taskNo)}/retrievals`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}

/**
 * 保存工作人员对一次主体检索候选的结论（一致 / 不一致 / 证据不足）。
 *
 * 不存在“自动采纳候选”的取值：相似度不能代替审核结论。expected_input_revision
 * 用于发现候选已过期（任务输入在检索之后发生了变化）。
 */
export function submitRetrievalDecision(
  taskNo: string,
  retrievalNo: string,
  payload: {
    decision: RetrievalDecision;
    note?: string;
    selected?: Record<string, unknown>;
    expected_input_revision?: number;
  },
): Promise<RetrievalDecisionResult> {
  const path = `/audit-tasks/${encodeURIComponent(taskNo)}/retrievals/${encodeURIComponent(retrievalNo)}/decision`;
  return request<RetrievalDecisionResult>(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}

/** 本任务各上传版本的证据索引状态；只读，不触发建索引。 */
export function getTaskEvidenceIndex(taskNo: string): Promise<TaskEvidenceIndex> {
  return request<TaskEvidenceIndex>(`/audit-tasks/${encodeURIComponent(taskNo)}/evidence-index`);
}

/**
 * Agent 工具调用账本（只读）：模型请求了哪些工具、依据哪一版事实与工具定义、返回了什么。
 *
 * 规则详情要展示的「确定性比较结果」以它为准，**不由页面重新计算比较过程**。
 * `evaluationId` 给出时只取该评估批次。
 */
export function listAgentToolCalls(
  taskNo: string,
  evaluationId?: string,
): Promise<{ request_id: string; task_no: string; evaluations: string[]; items: AgentToolCallRow[] }> {
  return request(
    `/audit-tasks/${encodeURIComponent(taskNo)}/agent-tool-calls${toQuery({ evaluation_id: evaluationId })}`,
  );
}
