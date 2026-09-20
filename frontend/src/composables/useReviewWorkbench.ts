import { onUnmounted, ref, type Ref } from "vue";

import type { PendingReview } from "@/api/contracts";
import { getTaskReviewDrafts, saveTaskReviewDrafts } from "@/api/audits";
import type { ReviewDraft } from "@/components/audit/PendingReviewCard.vue";

export interface ReviewWorkbenchState {
  drafts: Ref<Record<string, ReviewDraft>>;
  /** 用户真正编辑过的待办编号。 */
  touched: Ref<Set<string>>;
  saveState: Ref<"idle" | "saving" | "saved" | "error">;
  saveMessage: Ref<string | null>;
  /**
   * 用任务当前待办初始化表单（系统建议值预填，不算用户输入）。
   *
   * `preserveExisting` 用于同一任务刷新：保留用户已经填过的内容。
   */
  initialise: (reviews: PendingReview[], preserveExisting?: boolean) => void;
  /** 读取某个待办的当前草稿；没有时返回一份空白草稿（不写回状态）。 */
  draftOf: (reviewNo: string) => ReviewDraft;
  /** 覆盖某个待办的草稿（用户在表单里编辑）。 */
  setDraft: (reviewNo: string, draft: ReviewDraft) => void;
  /** 恢复服务端保存的草稿；已被用户编辑过的条目不覆盖。 */
  restore: () => Promise<void>;
  /** 提交前调用：标记用户编辑。 */
  markTouched: (reviewNo: string) => void;
  /** 清空本地草稿状态（提交成功或切换任务）。 */
  clear: () => void;
  /** 组装提交载荷：只提交用户编辑过的条目。 */
  payloadOf: (reviews: PendingReview[]) => Array<Record<string, unknown>>;
  /** 立即写入服务端（页面「保存草稿」按钮）。 */
  flush: () => Promise<void>;
}

/**
 * 人工复核表单的本地状态与服务端草稿暂存。
 *
 * 关键区分（改动前先读）：**页面预填 ≠ 用户输入**。
 * 页面加载会用「系统建议值」预填表单，那不是用户输入：不加区分的话，刷新时预填值会被
 * 当成「本地已有内容」，既挡住服务端草稿恢复，又会被去抖保存写回（把已保存的人工结论清空）。
 * 因此只有 `markTouched` 标记过的条目才参与恢复与保存。
 *
 * 保存失败明确提示「未保存」并保留真实原因，不伪装已保存。
 */
export function useReviewWorkbench(options: { taskNo: Ref<string> }): ReviewWorkbenchState {
  const drafts = ref<Record<string, ReviewDraft>>({});
  const touched = ref<Set<string>>(new Set());
  const saveState = ref<"idle" | "saving" | "saved" | "error">("idle");
  const saveMessage = ref<string | null>(null);
  let timer: ReturnType<typeof setTimeout> | null = null;
  /** 当前草稿所属的任务：保存响应回来时用它判断是否已经切了任务。 */
  let currentTaskNo: string | null = null;

  const EMPTY_DRAFT: ReviewDraft = { decision: "" as ReviewDraft["decision"], note: "", fieldValue: "", basisType: "HUMAN_STATEMENT" };

  function initialise(reviews: PendingReview[], preserveExisting = false): void {
    const previous = preserveExisting ? drafts.value : {};
    const next: Record<string, ReviewDraft> = {};
    for (const review of reviews) {
      const suggested = review.proposed_value ?? review.system_judgment?.raw_value ?? "";
      next[review.review_no] =
        previous[review.review_no] ?? {
          decision: "" as ReviewDraft["decision"],
          note: "",
          fieldValue: typeof suggested === "string" ? suggested : "",
          basisType: "HUMAN_STATEMENT",
        };
    }
    drafts.value = next;
    // 初始化只重建表单，不改「用户是否编辑过」：那是用户行为的记录，不随重载清空。
  }

  function draftOf(reviewNo: string): ReviewDraft {
    return drafts.value[reviewNo] ?? EMPTY_DRAFT;
  }

  function setDraft(reviewNo: string, draft: ReviewDraft): void {
    drafts.value = { ...drafts.value, [reviewNo]: draft };
    markTouched(reviewNo);
  }

  async function restore(): Promise<void> {
    const taskNo = options.taskNo.value;
    currentTaskNo = taskNo;
    try {
      const payload = await getTaskReviewDrafts(taskNo);
      if (currentTaskNo !== taskNo) return;
      const merged = { ...drafts.value };
      for (const [reviewNo, draft] of Object.entries(payload.drafts)) {
        // 用户本次会话已编辑过的条目不被服务端草稿覆盖；其余（含仅由系统建议值预填的）用草稿恢复。
        if (touched.value.has(reviewNo)) continue;
        merged[reviewNo] = {
          decision: (draft.decision ?? "") as ReviewDraft["decision"],
          note: draft.business_note ?? "",
          fieldValue: draft.field_value ?? "",
          basisType: (draft.basis_type ?? "HUMAN_STATEMENT") as ReviewDraft["basisType"],
        };
      }
      drafts.value = merged;
    } catch (error) {
      // 草稿恢复失败不阻塞复核；明确说明当前输入只在本次会话内保留。
      if (currentTaskNo !== taskNo) return;
      saveState.value = "error";
      saveMessage.value = `复核草稿读取失败：${error instanceof Error ? error.message : "未知原因"}（当前输入仅在本次页面会话内保留）`;
    }
  }

  function markTouched(reviewNo: string): void {
    touched.value = new Set(touched.value).add(reviewNo);
    schedule();
  }

  /** 去抖保存：输入停顿后写入服务端。 */
  function schedule(): void {
    if (!touched.value.size) return;
    if (timer) clearTimeout(timer);
    timer = setTimeout(() => {
      timer = null;
      void flush();
    }, 800);
  }

  async function flush(): Promise<void> {
    const taskNo = currentTaskNo;
    if (!taskNo || !touched.value.size) return;
    const items = Object.entries(drafts.value)
      .filter(([reviewNo]) => touched.value.has(reviewNo))
      .map(([reviewNo, draft]) => ({
        review_no: reviewNo,
        decision: draft.decision || null,
        business_note: draft.note || null,
        field_value: draft.fieldValue || null,
        basis_type: draft.basisType || null,
      }));
    if (!items.length) return;
    saveState.value = "saving";
    try {
      await saveTaskReviewDrafts(taskNo, items);
      if (currentTaskNo !== taskNo) return;
      saveState.value = "saved";
      saveMessage.value = null;
    } catch (error) {
      if (currentTaskNo !== taskNo) return;
      saveState.value = "error";
      saveMessage.value = `复核草稿未保存：${error instanceof Error ? error.message : "未知原因"}`;
    }
  }

  function payloadOf(reviews: PendingReview[]): Array<Record<string, unknown>> {
    return reviews
      .filter((review) => touched.value.has(review.review_no))
      .map((review) => {
        const draft = drafts.value[review.review_no];
        return {
          review_no: review.review_no,
          decision: draft?.decision || null,
          business_note: draft?.note || null,
          field_value: draft?.fieldValue || null,
          basis_type: draft?.basisType || null,
        };
      });
  }

  function clear(): void {
    if (timer) {
      clearTimeout(timer);
      timer = null;
    }
    currentTaskNo = null;
    drafts.value = {};
    touched.value = new Set();
    saveState.value = "idle";
    saveMessage.value = null;
  }

  onUnmounted(() => {
    if (timer) {
      clearTimeout(timer);
      timer = null;
    }
  });

  return { drafts, touched, saveState, saveMessage, initialise, draftOf, setDraft, restore, markTouched, clear, payloadOf, flush };
}
