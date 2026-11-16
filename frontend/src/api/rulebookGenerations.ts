import type {
  GenerationCreated,
  GenerationDocument,
  GenerationExport,
  GenerationImportResult,
  GenerationJob,
  GenerationJobRow,
  GenerationQuery,
  GenerationRetryResult,
  GenerationMessages,
  GenerationRevision,
  Paged,
  OperationAccepted,
} from "./contracts";
import { apiFileUrl, request, requestBlob, uploadWithProgress } from "./http";
import { toQuery } from "./query";

/** 上传原始 Excel、业务背景和补充要求，创建生成作业；此时不调用模型。 */
export function createGenerationJob(
  file: File,
  input: { businessBackground: string; generationRequirements: string; idempotencyKey: string },
  onProgress?: (percent: number) => void,
): Promise<GenerationCreated> {
  const formData = new FormData();
  formData.append("file", file);
  formData.append("business_background", input.businessBackground);
  formData.append("generation_requirements", input.generationRequirements);
  formData.append("idempotency_key", input.idempotencyKey);
  return uploadWithProgress<GenerationCreated>("/rulebook-generations", formData, onProgress);
}

/** 生成记录分页；状态由最近后台操作与已保存文档推导。 */
export function listGenerationJobsPage(query: GenerationQuery): Promise<Paged<GenerationJobRow>> {
  return request<Paged<GenerationJobRow>>(`/rulebook-generations/page${toQuery({ ...query })}`);
}

/** 生成作业详情：当前文档、修订号与最近后台操作。 */
export function getGenerationJob(jobNo: string): Promise<GenerationJob> {
  return request<GenerationJob>(`/rulebook-generations/${encodeURIComponent(jobNo)}`);
}

/** 原始 Excel 下载地址。 */
export function generationSourceFileUrl(jobNo: string): string {
  return apiFileUrl(`/rulebook-generations/${encodeURIComponent(jobNo)}/source-file`);
}

/** 开始生成或失败后显式重试；返回 202 只代表已受理。 */
export function startGeneration(jobNo: string, idempotencyKey: string): Promise<OperationAccepted> {
  return request<OperationAccepted>(`/rulebook-generations/${encodeURIComponent(jobNo)}/generate`, {
    method: "POST",
    data: { idempotency_key: idempotencyKey },
  });
}

/** 使用原资料显式重试；旧版本返回新作业，响应丢失后的重发复用原请求。 */
export function retryGeneration(jobNo: string, idempotencyKey: string): Promise<GenerationRetryResult> {
  return request<GenerationRetryResult>(`/rulebook-generations/${encodeURIComponent(jobNo)}/retry`, {
    method: "POST",
    data: { idempotency_key: idempotencyKey },
  });
}

export function sendGenerationMessage(jobNo: string, input: {
  message: string; expectedRevision: number; idempotencyKey: string; issueId?: string;
}): Promise<OperationAccepted> {
  return request<OperationAccepted>(`/rulebook-generations/${encodeURIComponent(jobNo)}/messages`, {
    method: "POST",
    data: { message: input.message, expected_revision: input.expectedRevision, idempotency_key: input.idempotencyKey, issue_id: input.issueId },
  });
}

export function listGenerationMessages(jobNo: string, beforeId?: number): Promise<GenerationMessages> {
  return request<GenerationMessages>(`/rulebook-generations/${encodeURIComponent(jobNo)}/messages${toQuery({ before_id: beforeId })}`);
}

export function listGenerationRevisions(jobNo: string): Promise<GenerationRevision[]> {
  return request<GenerationRevision[]>(`/rulebook-generations/${encodeURIComponent(jobNo)}/revisions`);
}

export function restoreGenerationRevision(jobNo: string, expectedRevision: number, targetRevision: number): Promise<GenerationJob> {
  return request<GenerationJob>(`/rulebook-generations/${encodeURIComponent(jobNo)}/restore`, {
    method: "POST",
    data: { expected_revision: expectedRevision, target_revision: targetRevision },
  });
}

/** 保存完整编辑文档；旧修订保存明确冲突。 */
export function saveGenerationDocument(
  jobNo: string, expectedRevision: number, document: GenerationDocument,
): Promise<GenerationJob> {
  return request<GenerationJob>(`/rulebook-generations/${encodeURIComponent(jobNo)}/document`, {
    method: "PUT",
    data: { expected_revision: expectedRevision, document },
  });
}

/** 按当前修订创建或取得导出；重复导出复用已有文件。 */
export function createGenerationExport(jobNo: string, expectedRevision: number): Promise<GenerationExport> {
  return request<GenerationExport>(`/rulebook-generations/${encodeURIComponent(jobNo)}/exports`, {
    method: "POST",
    data: { expected_revision: expectedRevision },
  });
}

/** 查询该作业已有的导出修订。 */
export function listGenerationExports(jobNo: string): Promise<GenerationExport[]> {
  return request<GenerationExport[]>(`/rulebook-generations/${encodeURIComponent(jobNo)}/exports`);
}

/** 下载冻结版本的业务内容，排版沿用上传的 Excel。 */
export function generationExportFileUrl(jobNo: string, exportNo: string): string {
  return apiFileUrl(
    `/rulebook-generations/${encodeURIComponent(jobNo)}/exports/${encodeURIComponent(exportNo)}/file?layout=source`,
  );
}

/** 收到真实文件后发起浏览器下载；缺失、损坏和通信错误交给页面展示。 */
export async function downloadGenerationExportFile(
  jobNo: string, item: Pick<GenerationExport, "export_no" | "filename">,
): Promise<void> {
  const blob = await requestBlob(
    `/rulebook-generations/${encodeURIComponent(jobNo)}/exports/${encodeURIComponent(item.export_no)}/file?layout=source`,
  );
  if (blob.type.split(";", 1)[0] !== "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet") {
    throw new Error(`下载响应不是规则库 Excel（${blob.type || "未提供文件类型"}）`);
  }
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = item.filename;
  document.body.appendChild(link);
  try {
    link.click();
  } finally {
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }
}

/** 选择已有场景导入冻结导出，创建真实导入作业。 */
export function importGenerationExport(
  jobNo: string,
  input: { exportNo: string; scenarioCode: string; idempotencyKey: string },
): Promise<GenerationImportResult> {
  return request<GenerationImportResult>(`/rulebook-generations/${encodeURIComponent(jobNo)}/imports`, {
    method: "POST",
    data: {
      export_no: input.exportNo,
      scenario_code: input.scenarioCode,
      idempotency_key: input.idempotencyKey,
    },
  });
}
