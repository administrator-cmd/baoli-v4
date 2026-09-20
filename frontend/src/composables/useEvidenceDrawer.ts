import { computed, ref, shallowRef, type ComputedRef, type Ref } from "vue";

import type { AuditField, Evidence, PendingReview } from "@/api/contracts";
import { getTaskEvidence } from "@/api/audits";

export interface EvidenceDrawerState {
  /** 抽屉是否打开；由「当前查看的证据对象」决定，不单独维护一个布尔量。 */
  open: ComputedRef<boolean>;
  /** 页面传给抽屉的业务上下文（字段名、材料名、证据编号）。 */
  field: Ref<AuditField | null>;
  evidence: Ref<Evidence | null>;
  error: Ref<string | null>;
  loading: Ref<boolean>;
  /** 按字段打开证据；没有证据编号时明确提示，不打开空抽屉。 */
  openForField: (field: AuditField) => Promise<void>;
  /** 按当前人工复核待办打开它引用的证据。 */
  openForReview: (review: PendingReview) => Promise<void>;
  close: () => void;
}

/**
 * 原文证据抽屉的取数与状态。
 *
 * 证据按「任务 + 证据编号」在服务端重新读取，页面不缓存跨任务的证据；
 * 读取失败时保留错误原文（抽屉里可见），不用空内容假装证据不存在。
 * 抽屉里的字段上下文只是展示用途，人工值不以旧引文作证明。
 */
export function useEvidenceDrawer(options: {
  taskNo: Ref<string>;
  /** 读取失败时同时进入页面的统一失败提示。 */
  run: (action: () => Promise<void>) => Promise<void>;
}): EvidenceDrawerState {
  const field = shallowRef<AuditField | null>(null);
  const evidence = shallowRef<Evidence | null>(null);
  const error = ref<string | null>(null);
  const loading = ref(false);

  async function load(taskNo: string, evidenceNo: string): Promise<void> {
    error.value = null;
    loading.value = true;
    try {
      await options.run(async () => {
        try {
          evidence.value = await getTaskEvidence(taskNo, evidenceNo);
        } catch (err) {
          error.value = err instanceof Error ? err.message : "证据读取失败";
          throw err;
        }
      });
    } finally {
      loading.value = false;
    }
  }

  async function openForField(target: AuditField): Promise<void> {
    const taskNo = options.taskNo.value;
    if (!target.evidence_no) return;
    // 先打开抽屉再取数：加载与失败都要在抽屉里可见，不能只在页面顶部提示一次。
    field.value = target;
    evidence.value = null;
    await load(taskNo, target.evidence_no);
  }

  async function openForReview(review: PendingReview): Promise<void> {
    const taskNo = options.taskNo.value;
    const evidenceNo = review.evidence_no;
    if (!evidenceNo) return;
    // 显式标注类型：避免对含递归 JsonValue 的对象字面量做深层可赋值性推导（TS2589）。
    const placeholder: AuditField = {
      name: review.field_name ?? review.rule_name ?? "人工复核",
      material_name: review.material_name ?? "",
      status: "PENDING_REVIEW",
      raw_value: null,
      normalized_value: null,
      evidence_no: evidenceNo,
    };
    field.value = placeholder;
    evidence.value = null;
    await load(taskNo, evidenceNo);
  }

  function close(): void {
    field.value = null;
    evidence.value = null;
    error.value = null;
  }

  return {
    open: computed(() => field.value !== null),
    field,
    evidence,
    error,
    loading,
    openForField,
    openForReview,
    close,
  };
}
