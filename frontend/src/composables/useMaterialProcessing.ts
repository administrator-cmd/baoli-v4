import type {
  AuditTask,
  AuditDocument,
} from "@/api/contracts";
import { computed, ref, type Ref, type ShallowUnwrapRef } from "vue";
import { ElMessage, ElMessageBox } from "element-plus";


import { getDocumentArtifact, getDocumentOcr, retryDocumentOcr, retryDocumentArtifact } from "@/api/audits";
import { isOperationActive, operationIdempotencyKey, type OperationView } from "@/api/http";

import { processingStatusLabel, processingStatusTone, processingArtifactLabel, type ProcessingArtifact } from "@/constants/materialProcessing";

interface MaterialProcessingOptions {
  task: Readonly<Ref<AuditTask | null>>;
  watching: Readonly<Ref<boolean>>;
  pending: Readonly<Ref<boolean>>;
  generation: () => number;
  isStale: (token: number, taskNo: string) => boolean;
  refresh: () => Promise<void>;
  onOperationAccepted: (operation: OperationView) => void;
}

/** 材料处理状态查询与单项重试，沿用任务页面的响应代次。 */
export function useMaterialProcessing(options: MaterialProcessingOptions) {
  const processingDialogVisible = ref(false);
  const selectedDocumentNo = ref<string | null>(null);
  const processingDocument = computed(() => options.task.value?.documents.find(
    (document) => document.document_no === selectedDocumentNo.value,
  ) ?? null);
  const processingStatuses = ref<ProcessingArtifact[]>([]);
  const processingLoading = ref(false);
  const processingErrors = ref<Array<{ artifact_type: ProcessingArtifact["artifact_type"]; message: string }>>([]);
  const processingStatusError = ref<string | null>(null);
  const processingSubmitting = ref(false);
  const processingError = ref<string | null>(null);
  const processingActive = computed(() => isOperationActive(options.task.value?.latest_operation?.status));
  // 受理响应丢失时复用同一提交；收到受理结果后，下一次手动重试才创建新命令。
  const retryAttempts = new Map<string, { idempotency_key: string; resubmit_unknown: boolean }>();
  let processingRequestGeneration = 0;

  const processingRetryBlockedReason = computed(() => {
    const current = options.task.value;
    if (!current) return "尚未读取到任务状态。";
    if (processingDocument.value?.quality_precheck?.processing_allowed !== true) return "当前材料尚未通过前置质量检查。";
    if (processingSubmitting.value) return "正在提交重试。";
    if (processingStatusError.value) return "未能确认任务状态，暂不能重试。";
    if (current.status === "RUNNING" || processingActive.value || options.watching.value) {
      return "任务仍有后台操作，结束后可单独重试。";
    }
    if (current.status === "COMPLETED") return "当前审核已结束，不能在本批次重试材料处理。";
    if (options.pending.value || processingLoading.value) return "正在更新状态。";
    return null;
  });

  function processingRetryable(artifact: ProcessingArtifact): boolean {
    if (!artifact.artifact_no) return false;
    if (artifact.artifact_type !== "OCR") return ["FAILED", "PROCESSING"].includes(artifact.status);
    // 已受理的作业续查原作业；提交结果未知时由用户明确确认后重新提交。
    return artifact.status === "FAILED" || artifact.status === "SUBMITTING"
      || (artifact.status === "PENDING" && Boolean(artifact.job_id));
  }

  function processingRetryLabel(artifact: ProcessingArtifact): string {
    if (artifact.artifact_type !== "OCR") return artifact.status === "PROCESSING" ? "确认后重试" : "重试此项";
    if (artifact.status === "PENDING") return "继续查询 OCR";
    if (artifact.status === "SUBMITTING") return "重新提交 OCR";
    return "重试 OCR";
  }

  async function loadProcessingStatuses(refreshTask = false): Promise<void> {
    const document = processingDocument.value;
    const currentTaskNo = options.task.value?.task_no;
    if (!document || !currentTaskNo) return;
    const token = options.generation();
    const generation = ++processingRequestGeneration;
    processingLoading.value = true;
    processingErrors.value = [];
    try {
      if (refreshTask) {
        try {
          await options.refresh();
          if (options.isStale(token, currentTaskNo) || generation !== processingRequestGeneration) return;
          processingStatusError.value = null;
        } catch (error) {
          if (options.isStale(token, currentTaskNo) || generation !== processingRequestGeneration) return;
          processingStatusError.value = error instanceof Error ? error.message : "未能读取任务状态";
        }
        if (options.isStale(token, currentTaskNo) || generation !== processingRequestGeneration) return;
      }
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
    } finally {
      if (!options.isStale(token, currentTaskNo) && generation === processingRequestGeneration) processingLoading.value = false;
    }
  }

  function openProcessingDialog(document: AuditDocument): void {
    selectedDocumentNo.value = document.document_no;
    processingDialogVisible.value = true;
    processingStatuses.value = [];
    processingStatusError.value = null;
    processingError.value = null;
    void loadProcessingStatuses(true);
  }

  function pollProcessingStatuses(): void {
    if (processingLoading.value || processingSubmitting.value || options.pending.value) return;
    // 已有操作追踪时由该追踪刷新任务，否则同步弹窗所依赖的任务状态，不能只刷新产物。
    const refreshTask = Boolean(processingStatusError.value) || (!options.watching.value
      && (options.task.value?.status === "RUNNING" || processingActive.value));
    void loadProcessingStatuses(refreshTask);
  }

  async function retrySingleArtifact(artifact: ProcessingArtifact): Promise<void> {
    const document = processingDocument.value;
    const currentTaskNo = options.task.value?.task_no;
    const currentTask = options.task.value;
    if (!document || !currentTaskNo || !currentTask || !artifact.artifact_no || !processingRetryable(artifact)
      || processingRetryBlockedReason.value) return;
    const token = options.generation();
    const inputRevision = currentTask.input_revision;
    const resubmitUnknown = artifact.status === "SUBMITTING" || artifact.status === "PROCESSING";
    const message = artifact.artifact_type !== "OCR"
      ? `只重试「${document.material_name}」的${processingArtifactLabel(artifact.artifact_type)}，其他已完成结果会保留。${resubmitUnknown ? "上次处理结果未知，再次提交可能产生重复调用或再次计费。" : ""}确认继续？`
      : resubmitUnknown
      ? `「${document.material_name}」上次 OCR 提交未拿到作业号，无法确认云端是否已受理。重新提交可能创建重复作业并再次计费。确认只重新提交这份材料的 OCR？已完成的其他处理结果会保留。`
      : artifact.status === "PENDING"
        ? `继续查询「${document.material_name}」已有的 OCR 作业，不创建新作业。其他材料的已完成结果会保留。`
        : `只重新提交「${document.material_name}」的 PaddleOCR，其他材料和已完成的去章、印章识别结果会保留。确认继续？`;
    processingSubmitting.value = true;
    try {
      await ElMessageBox.confirm(
        message, processingRetryLabel(artifact),
        { type: "warning", confirmButtonText: artifact.status === "PENDING" ? "继续查询" : "确认提交", cancelButtonText: "取消" },
      );
    } catch {
      return;
    } finally {
      if (!options.isStale(token, currentTaskNo)) processingSubmitting.value = false;
    }
    if (options.isStale(token, currentTaskNo) || !processingDialogVisible.value
      || processingDocument.value?.document_no !== document.document_no || processingRetryBlockedReason.value) return;
    const attemptId = JSON.stringify([currentTaskNo, document.document_no, artifact.artifact_type, artifact.artifact_no, inputRevision]);
    const attempt = retryAttempts.get(attemptId) ?? {
      idempotency_key: operationIdempotencyKey("DOCUMENT_RETRY", artifact.artifact_no),
      resubmit_unknown: resubmitUnknown,
    };
    retryAttempts.set(attemptId, attempt);
    processingSubmitting.value = true;
    processingError.value = null;
    try {
      const payload = {
        artifact_no: artifact.artifact_no,
        input_revision: inputRevision,
        ...attempt,
      };
      const accepted = artifact.artifact_type === "OCR"
        ? await retryDocumentOcr(currentTaskNo, document.document_no, payload)
        : await retryDocumentArtifact(currentTaskNo, document.document_no, artifact.artifact_type, payload);
      if (options.isStale(token, currentTaskNo)) return;
      retryAttempts.delete(attemptId);
      options.onOperationAccepted(accepted);
      ElMessage.success("已受理该材料的单项处理，完成后可重新执行审核");
      await loadProcessingStatuses(true);
    } catch (error) {
      if (!options.isStale(token, currentTaskNo)) processingError.value = error instanceof Error ? error.message : "材料处理重试失败";
    } finally {
      if (!options.isStale(token, currentTaskNo)) processingSubmitting.value = false;
    }
  }

  function reset(): void {
    processingDialogVisible.value = false;
    selectedDocumentNo.value = null;
    processingStatuses.value = [];
    processingLoading.value = false;
    processingErrors.value = [];
    processingStatusError.value = null;
    processingError.value = null;
    processingSubmitting.value = false;
    retryAttempts.clear();
    processingRequestGeneration += 1;
  }

  return {
    processingDialogVisible,
    processingDocument,
    processingStatuses,
    processingLoading,
    processingErrors,
    processingStatusError,
    processingSubmitting,
    processingError,
    processingRetryBlockedReason,
    processingActive,
    processingStatusLabel: (artifact: ProcessingArtifact) => processingStatusLabel(artifact, processingActive.value),
    processingStatusTone: (artifact: ProcessingArtifact) => processingStatusTone(artifact, processingActive.value),
    processingArtifactLabel,
    processingRetryable,
    processingRetryLabel,
    loadProcessingStatuses,
    pollProcessingStatuses,
    openProcessingDialog,
    retrySingleArtifact,
    reset,
  };
}

export type MaterialProcessingState = ShallowUnwrapRef<ReturnType<typeof useMaterialProcessing>>;
