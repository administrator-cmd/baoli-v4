import { computed, ref, type Ref, type ShallowUnwrapRef } from "vue";
import { ElMessage, ElMessageBox } from "element-plus";

import type { AuditDocument } from "@/api/contracts";
import {
  getDocumentArtifact,
  getDocumentOcr,
  reconcileDocumentArtifact,
  reconcileDocumentOcr,
  retryDocumentOcr,
  type AuditTaskView,
  type DocumentArtifactStatus,
  type DocumentOcrStatus,
} from "@/api/audits";
import { isOperationActive, operationIdempotencyKey, type OperationView } from "@/api/http";

interface MaterialProcessingOptions {
  task: Readonly<Ref<AuditTaskView | null>>;
  watching: Readonly<Ref<boolean>>;
  pending: Readonly<Ref<boolean>>;
  generation: () => number;
  isStale: (token: number, taskNo: string) => boolean;
  refresh: () => Promise<void>;
  onOperationAccepted: (operation: OperationView) => void;
}

type ProcessingArtifact = {
  artifact_type: DocumentArtifactStatus["artifact_type"] | "OCR";
  artifact_no: string | null;
  status: string;
  message: string;
  job_id?: string | null;
  deadline_at?: string | null;
  provider_state?: string | null;
  extract_progress?: DocumentOcrStatus["extract_progress"];
  progress_updated_at?: string | null;
};
type ProcessingAction = "ADOPT_JOB" | "RETRY_SUBMIT" | "CLEAR";

/** 材料处理状态查询与人工对账，沿用任务页面的响应代次。 */
export function useMaterialProcessing(options: MaterialProcessingOptions) {
  const processingDialogVisible = ref(false);
  const processingDocument = ref<AuditDocument | null>(null);
  const processingStatuses = ref<ProcessingArtifact[]>([]);
  const processingLoading = ref(false);
  const processingErrors = ref<Array<{ artifact_type: ProcessingArtifact["artifact_type"]; message: string }>>([]);
  const processingAction = ref<{ artifact: ProcessingArtifact; action: ProcessingAction } | null>(null);
  const processingReason = ref("");
  const processingJobId = ref("");
  const processingSubmitting = ref(false);
  const processingActionError = ref<string | null>(null);
  let processingRequestGeneration = 0;
  let processingAttempt: { signature: string; key: string } | null = null;

  const processingActionAllowed = computed(() => {
    const current = options.task.value;
    return Boolean(current
      && !["RUNNING", "COMPLETED", "FAILED"].includes(current.status)
      && !isOperationActive(current.latest_operation?.status)
      && !options.watching.value && !options.pending.value && !processingLoading.value && !processingSubmitting.value);
  });

  function processingStatusLabel(artifact: ProcessingArtifact): string {
    if (artifact.artifact_type === "OCR" && artifact.status === "PENDING") {
      if (artifact.provider_state === "running") return "识别中";
      if (artifact.provider_state === "pending") return "排队中";
      if (artifact.provider_state === "done") return "识别完成，正在保存";
      if (artifact.job_id) return "可继续查询";
    }
    const labels: Record<string, string> = {
      NOT_SUBMITTED: "未提交",
      SUBMITTING: "提交结果未知",
      PROCESSING: "处理结果未知",
      PENDING: "等待完成",
      READY: "已就绪",
      FAILED: "失败",
      INVALIDATED: "已失效",
    };
    return labels[artifact.status] ?? artifact.status;
  }

  function processingArtifactLabel(artifactType: ProcessingArtifact["artifact_type"]): string {
    return {
      MATERIAL_CLASSIFY: "8018 文件校验",
      DOCUMENT_PARSE: "8010 去除印章",
      OCR: "PaddleOCR 正文识别",
      SEAL: "8003 印章识别",
    }[artifactType];
  }

  function processingRecoverable(artifact: ProcessingArtifact): boolean {
    if (!artifact.artifact_no) return false;
    if (artifact.artifact_type !== "OCR") return artifact.status === "PROCESSING";
    if (["SUBMITTING", "PROCESSING", "FAILED"].includes(artifact.status)) return true;
    // PENDING 可以直接续查同一个远端作业；不必等本地等待截止，也不会创建新作业。
    return artifact.status === "PENDING" && Boolean(artifact.job_id);
  }

  async function loadProcessingStatuses(): Promise<void> {
    const document = processingDocument.value;
    const currentTaskNo = options.task.value?.task_no;
    if (!document || !currentTaskNo) return;
    const token = options.generation();
    const generation = ++processingRequestGeneration;
    processingLoading.value = true;
    processingErrors.value = [];
    try {
      const results = await Promise.allSettled([
        getDocumentArtifact(currentTaskNo, document.document_no, "MATERIAL_CLASSIFY"),
        getDocumentArtifact(currentTaskNo, document.document_no, "DOCUMENT_PARSE"),
        getDocumentOcr(currentTaskNo, document.document_no),
        getDocumentArtifact(currentTaskNo, document.document_no, "SEAL"),
      ]);
      if (options.isStale(token, currentTaskNo) || generation !== processingRequestGeneration) return;
      const types: ProcessingArtifact["artifact_type"][] = ["MATERIAL_CLASSIFY", "DOCUMENT_PARSE", "OCR", "SEAL"];
      const statuses: ProcessingArtifact[] = [];
      const errors: typeof processingErrors.value = [];
      results.forEach((result, index) => {
        const artifactType = types[index]!;
        if (result.status === "rejected") {
          errors.push({
            artifact_type: artifactType,
            message: result.reason instanceof Error ? result.reason.message : "状态读取失败",
          });
        } else {
          statuses.push({ ...result.value, artifact_type: artifactType });
        }
      });
      processingStatuses.value = statuses;
      processingErrors.value = errors;
      const selected = processingAction.value;
      if (selected) {
        const current = statuses.find((item) => item.artifact_no === selected.artifact.artifact_no && item.artifact_type === selected.artifact.artifact_type);
        processingAction.value = current && processingRecoverable(current) ? { ...selected, artifact: current } : null;
        if (!processingAction.value) processingActionError.value = null;
      }
    } finally {
      if (!options.isStale(token, currentTaskNo) && generation === processingRequestGeneration) processingLoading.value = false;
    }
  }

  function openProcessingDialog(document: AuditDocument): void {
    processingDocument.value = document;
    processingDialogVisible.value = true;
    processingStatuses.value = [];
    processingAction.value = null;
    processingActionError.value = null;
    void loadProcessingStatuses();
  }

  function chooseProcessingAction(artifact: ProcessingArtifact, action: ProcessingAction): void {
    processingAction.value = { artifact, action };
    processingReason.value = "";
    processingJobId.value = action === "ADOPT_JOB" ? artifact.job_id ?? "" : "";
    processingActionError.value = null;
  }

  async function submitProcessingAction(): Promise<void> {
    const selected = processingAction.value;
    const document = processingDocument.value;
    const currentTaskNo = options.task.value?.task_no;
    if (!selected || !document || !currentTaskNo || !selected.artifact.artifact_no || !processingActionAllowed.value) return;
    const reason = processingReason.value.trim();
    const jobId = processingJobId.value.trim();
    if (!reason) {
      processingActionError.value = "请填写本次人工对账的原因";
      return;
    }
    if (selected.action === "ADOPT_JOB" && !jobId) {
      processingActionError.value = "请填写已向供应商确认的作业号";
      return;
    }
    const label = processingArtifactLabel(selected.artifact.artifact_type);
    const warning = selected.action === "ADOPT_JOB"
      ? `确认已向供应商核实「${label}」的作业号？系统将按此作业号继续查询。`
      : selected.action === "RETRY_SUBMIT" && selected.artifact.status === "FAILED"
        ? `「${label}」的作业已明确失败。确认重新提交该材料进行识别？`
        : `确认「${label}」的当前请求结果未知，并已核实外部处理情况？继续操作可能再次调用外部服务。`;
    try {
      await ElMessageBox.confirm(warning, "确认人工对账", {
        type: "warning", confirmButtonText: "确认提交", cancelButtonText: "取消",
      });
    } catch {
      return;
    }
    const token = options.generation();
    if (options.isStale(token, currentTaskNo) || !processingDialogVisible.value) return;
    const artifactNo = selected.artifact.artifact_no;
    const signature = JSON.stringify([currentTaskNo, document.document_no, selected.artifact.artifact_type, artifactNo, selected.action, reason, jobId]);
    if (processingAttempt?.signature !== signature) {
      processingAttempt = {
        signature,
        key: operationIdempotencyKey("ARTIFACT_RECONCILE", currentTaskNo, document.document_no, artifactNo),
      };
    }
    processingSubmitting.value = true;
    processingActionError.value = null;
    let successMessage: string;
    try {
      const response = selected.artifact.artifact_type === "OCR"
        ? await reconcileDocumentOcr(currentTaskNo, document.document_no, {
            action: selected.action as "ADOPT_JOB" | "RETRY_SUBMIT",
            artifact_no: artifactNo,
            job_id: selected.action === "ADOPT_JOB" ? jobId : undefined,
            reason,
            idempotency_key: processingAttempt.key,
          })
        : await reconcileDocumentArtifact(currentTaskNo, document.document_no, selected.artifact.artifact_type, {
            artifact_no: artifactNo,
            reason,
            idempotency_key: processingAttempt.key,
          });
      successMessage = response.message;
    } catch (error) {
      if (!options.isStale(token, currentTaskNo)) processingActionError.value = error instanceof Error ? error.message : "人工对账失败";
      return;
    } finally {
      if (!options.isStale(token, currentTaskNo)) processingSubmitting.value = false;
    }
    if (options.isStale(token, currentTaskNo)) return;
    processingAttempt = null;
    processingAction.value = null;
    processingReason.value = "";
    processingJobId.value = "";
    ElMessage.success(successMessage);
    await Promise.all([loadProcessingStatuses(), options.refresh()]);
  }

  async function retrySingleOcr(artifact: ProcessingArtifact): Promise<void> {
    const document = processingDocument.value;
    const currentTaskNo = options.task.value?.task_no;
    if (!document || !currentTaskNo || artifact.artifact_type !== "OCR" || !artifact.artifact_no
      || !processingActionAllowed.value) return;
    try {
      await ElMessageBox.confirm(
        `只重新处理「${document.material_name}」的 PaddleOCR，其他材料的已完成结果会保留。确认继续？`,
        "确认单份材料重试", { type: "warning", confirmButtonText: "确认重试", cancelButtonText: "取消" },
      );
    } catch {
      return;
    }
    const token = options.generation();
    const key = operationIdempotencyKey("OCR_DOCUMENT_RETRY", currentTaskNo, document.document_no, artifact.artifact_no);
    processingSubmitting.value = true;
    processingActionError.value = null;
    try {
      const accepted = await retryDocumentOcr(currentTaskNo, document.document_no, {
        artifact_no: artifact.artifact_no,
        input_revision: options.task.value?.input_revision ?? 0,
        idempotency_key: key,
      });
      if (options.isStale(token, currentTaskNo)) return;
      options.onOperationAccepted(accepted);
      ElMessage.success("已受理该材料的 OCR 重试，其他材料结果已保留");
      await options.refresh();
      await loadProcessingStatuses();
    } catch (error) {
      if (!options.isStale(token, currentTaskNo)) processingActionError.value = error instanceof Error ? error.message : "OCR 重试失败";
    } finally {
      if (!options.isStale(token, currentTaskNo)) processingSubmitting.value = false;
    }
  }

  function reset(): void {
    processingDialogVisible.value = false;
    processingDocument.value = null;
    processingStatuses.value = [];
    processingLoading.value = false;
    processingErrors.value = [];
    processingAction.value = null;
    processingActionError.value = null;
    processingSubmitting.value = false;
    processingAttempt = null;
    processingRequestGeneration += 1;
  }

  return {
    processingDialogVisible,
    processingDocument,
    processingStatuses,
    processingLoading,
    processingErrors,
    processingAction,
    processingReason,
    processingJobId,
    processingSubmitting,
    processingActionError,
    processingActionAllowed,
    processingStatusLabel,
    processingArtifactLabel,
    processingRecoverable,
    loadProcessingStatuses,
    openProcessingDialog,
    chooseProcessingAction,
    submitProcessingAction,
    retrySingleOcr,
    reset,
  };
}

export type MaterialProcessingState = ShallowUnwrapRef<ReturnType<typeof useMaterialProcessing>>;
