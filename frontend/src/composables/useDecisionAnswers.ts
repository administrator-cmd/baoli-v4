import { computed, onUnmounted, ref, type Ref } from "vue";
import { ElMessage } from "element-plus";

import type { ImportJob, JsonValue } from "@/api/contracts";
import { saveDecisionDraft, saveImportDecisions } from "@/api/imports";

export interface DecisionAnswersState {
  /** 逐条问题的作答值（按 issue_no）。 */
  selectedOptions: Ref<Record<string, JsonValue>>;
  initialise: (job: ImportJob, preserveAnswers?: boolean) => void;
  /** 草稿值 → 展示值：服务端存 `{ selected: ... }`，页面按 `selected` 取值。 */
  readDecisionValue: (decision: JsonValue) => JsonValue;
  update: (issueNo: string, value: JsonValue) => void;
  /** 立即落一次暂存（提交裁决、离开步骤前必须执行）。 */
  flush: () => Promise<void>;
  /**
   * 提交全部业务结论。`onAccepted` 收到服务端返回的最新作业视图。
   *
   * 回调**允许是异步的**且会被等待：回调里要做的事（例如重新读回作业与场景节点）
   * 必须在本函数返回之前完成，否则调用方会在数据还没就绪时就推进页面。
   */
  submit: (onAccepted: (updated: ImportJob) => void | Promise<void>) => Promise<boolean>;
  reset: () => void;
}

/**
 * 导入第二步的业务问题作答。
 *
 * 两条口径不要弱化：
 *
 * - **答案先暂存、再提交**：暂存会回写草稿修订号，提交裁决必须带暂存之后的最新修订，
 *   否则服务端会按过期修订拒绝。因此提交前一定先 flush。
 * - **未答完不提交**：留空、或数值类问题填了非有限/负值，都算未答完，明确提示原因，
 *   不代填默认值、不跳过。
 */
export function useDecisionAnswers(options: {
  /** 当前导入作业；未加载时为 null。 */
  job: Ref<ImportJob | null>;
  /** 暂存只回写修订号与问题状态，不打断用户当前编辑。 */
  applyRevision: (updated: ImportJob) => void;
  run: (action: () => Promise<void>) => Promise<void>;
  setFailure: (error: unknown) => void;
  formMessage: Ref<string>;
}): DecisionAnswersState {
  const selectedOptions = ref<Record<string, JsonValue>>({});
  let timer: ReturnType<typeof setTimeout> | null = null;

  function readDecisionValue(decision: JsonValue): JsonValue {
    if (decision && typeof decision === "object" && !Array.isArray(decision) && "selected" in (decision as Record<string, JsonValue>)) {
      return (decision as Record<string, JsonValue>).selected ?? "";
    }
    return decision ?? "";
  }

  const openIssues = computed(() => options.job.value?.issues.filter((item) => item.status === "OPEN") ?? []);

  const unresolved = computed(() =>
    openIssues.value.filter((issue) => {
      const value = selectedOptions.value[issue.issue_no];
      if (value === undefined || value === null || value === "") {
        return true;
      }
      if (issue.input_kind === "DECIMAL") {
        const numeric = Number(value);
        return Number.isNaN(numeric) || !Number.isFinite(numeric) || numeric < 0;
      }
      return false;
    }),
  );

  /**
   * 用作业当前的问题与草稿答案初始化表单。
   *
   * 本地已填但服务端没有的值由调用方（`preserveAnswers`）另行保留。
   */
  function initialise(job: ImportJob, preserveAnswers = false): void {
    const saved: Record<string, JsonValue> = Object.fromEntries(
      job.issues.map((issue) => [issue.issue_no, readDecisionValue(issue.decision)]),
    );
    if (preserveAnswers) {
      for (const [issueNo, value] of Object.entries(selectedOptions.value)) {
        if (saved[issueNo] === undefined || saved[issueNo] === null || saved[issueNo] === "") {
          saved[issueNo] = value;
        }
      }
    }
    selectedOptions.value = saved;
  }

  async function flush(): Promise<void> {
    if (timer) {
      clearTimeout(timer);
      timer = null;
    }
    if (!options.job.value || !openIssues.value.length) return;
    const answers: Record<string, JsonValue> = {};
    for (const issue of openIssues.value) {
      const value = selectedOptions.value[issue.issue_no];
      if (value !== undefined) {
        answers[issue.issue_no] = value;
      }
    }
    const updated = await saveDecisionDraft(options.job.value.job_no, answers);
    options.applyRevision(updated);
  }

  function update(issueNo: string, value: JsonValue): void {
    selectedOptions.value = { ...selectedOptions.value, [issueNo]: value };
    if (timer) clearTimeout(timer);
    timer = setTimeout(() => {
      timer = null;
      void flush().catch((error) => options.setFailure(error));
    }, 800);
  }

  async function submit(onAccepted: (updated: ImportJob) => void | Promise<void>): Promise<boolean> {
    if (!options.job.value) return false;
    if (unresolved.value.length) {
      options.formMessage.value = "请先为每一项业务问题给出结论；缺少阈值时必须填写真实数值，不能留空。";
      return false;
    }
    const jobNo = options.job.value.job_no;
    let accepted = false;
    await options.run(async () => {
      // 提交裁决前先落一次暂存，避免 202 受理与终态同步之间答案只存在内存里。
      await flush();
      // 暂存可能回写草稿修订号，提交裁决必须带暂存之后的最新修订。
      const revision = options.job.value?.draft_revision ?? 0;
      const decisions = openIssues.value.map((issue) => ({
        target_type: "ISSUE" as const,
        target_ref: issue.issue_no,
        action: "RESOLVE" as const,
        value: { selected: selectedOptions.value[issue.issue_no] as JsonValue },
      }));
      const updated = await saveImportDecisions(jobNo, decisions, revision);
      // 等回调做完再往下：回调可能要重新读回作业与场景节点，页面据它才切下一步。
      await onAccepted(updated);
      accepted = true;
      ElMessage.success("业务结论已保存");
    });
    return accepted;
  }

  function reset(): void {
    if (timer) {
      clearTimeout(timer);
      timer = null;
    }
    selectedOptions.value = {};
  }

  onUnmounted(() => {
    if (timer) {
      clearTimeout(timer);
      timer = null;
    }
  });

  return { selectedOptions, initialise, readDecisionValue, update, flush, submit, reset };
}
