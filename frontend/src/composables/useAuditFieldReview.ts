import type {
  AuditTask,
  FieldReviewCorrection,
} from "@/api/contracts";
import { onScopeDispose, ref, type ShallowRef } from "vue";
import { useRoute, useRouter } from "vue-router";
import { ElMessage } from "element-plus";

import { confirmFieldReview, getAuditTask, saveFieldReview } from "@/api/audits";

import { ApiError, operationIdempotencyKey, type OperationView } from "@/api/http";

interface AuditFieldReviewOptions {
  task: Readonly<ShallowRef<AuditTask | null>>;
  generation: () => number;
  isStale: (token: number, taskNo: string) => boolean;
  refresh: (taskNo: string, token: number) => Promise<void>;
  applyTask: (task: AuditTask) => void;
  trackOperation: (operation: OperationView, token: number) => void;
  resumeLatestOperation: (token: number) => void;
  setFailure: (error: unknown) => void;
}

/** 字段核对只负责保存与确认；确认结果未知时保留幂等身份并查询真实操作。 */
export function useAuditFieldReview(options: AuditFieldReviewOptions) {
  const { task, applyTask, trackOperation, resumeLatestOperation, setFailure } = options;
  const fetchTaskState = options.refresh;
  const route = useRoute();
  const router = useRouter();
  let disposed = false;
  const isStale = (token: number, taskNo: string) => disposed || options.isStale(token, taskNo);

  const reviewSaving = ref(false);
  const reviewConfirming = ref(false);
  /** 确认请求身份：受理结果未知时保留，重发同一请求由服务端返回原受理记录。 */
  let reviewAttemptKey: string | null = null;

  async function submitFieldReviewSave(corrections: FieldReviewCorrection[]): Promise<void> {
    if (!task.value || !corrections.length) return;
    const currentTaskNo = task.value.task_no;
    const inputRevision = task.value.input_revision;
    const token = options.generation();
    reviewSaving.value = true;
    try {
      const result = await saveFieldReview(currentTaskNo, { input_revision: inputRevision, corrections });
      if (isStale(token, currentTaskNo)) return;
      await fetchTaskState(currentTaskNo, token);
      if (isStale(token, currentTaskNo)) return;
      ElMessage.success(`已保存 ${result.applied.length} 项字段修改，任务仍等待核对`);
    } catch (error) {
      if (isStale(token, currentTaskNo)) return;
      setFailure(error);
    } finally {
      if (!isStale(token, currentTaskNo)) reviewSaving.value = false;
    }
  }

  async function submitFieldReviewConfirm(payload: {
    corrections: FieldReviewCorrection[];
    acknowledgeMissing: boolean;
    note: string | null;
  }): Promise<void> {
    if (!task.value) return;
    const currentTaskNo = task.value.task_no;
    const inputRevision = task.value.input_revision;
    const token = options.generation();
    const attemptKey = reviewAttemptKey ?? operationIdempotencyKey("FIELD_REVIEW_CONFIRM", currentTaskNo);
    reviewAttemptKey = attemptKey;
    reviewConfirming.value = true;
    try {
      const accepted = await confirmFieldReview(currentTaskNo, {
        idempotency_key: attemptKey,
        input_revision: inputRevision,
        corrections: payload.corrections,
        acknowledge_missing: payload.acknowledgeMissing,
        note: payload.note,
      });
      if (isStale(token, currentTaskNo)) return;
      trackOperation(accepted, token);
      await router.replace({
        name: "audit-task-detail",
        params: { taskNo: currentTaskNo },
        query: { ...route.query, operation: accepted.operation_no },
      });
    } catch (error) {
      if (isStale(token, currentTaskNo)) return;
      // 只有「结果未知」才保留请求身份：明确被服务端拒绝时原命令没有受理，
      // 保留旧键会让用户改完再提交时被当成「同一标识的不同请求」。
      if (!(error instanceof ApiError) || (error.code !== "REQUEST_TIMEOUT" && error.code !== "NETWORK_ERROR")) {
        reviewAttemptKey = null;
      }
      setFailure(error);
      // 受理结果未知时只查询真实状态：详情里的 latest_operation 指向已受理的操作。
      // 查询返回后要重新校验任务身份：期间切到别的任务时，这次响应不能覆盖当前页面。
      try {
        const latest = await getAuditTask(currentTaskNo);
        if (isStale(token, currentTaskNo)) return;
        applyTask(latest);
        resumeLatestOperation(token);
      } catch (queryError) {
        if (!isStale(token, currentTaskNo)) setFailure(queryError);
      }
    } finally {
      if (!isStale(token, currentTaskNo)) reviewConfirming.value = false;
    }
  }

  function clearAttempt(): void {
    reviewAttemptKey = null;
  }

  function reset(): void {
    clearAttempt();
    reviewSaving.value = false;
    reviewConfirming.value = false;
  }

  onScopeDispose(() => {
    disposed = true;
    reset();
  });

  return { reviewSaving, reviewConfirming, submitFieldReviewSave, submitFieldReviewConfirm, clearAttempt, reset };
}
