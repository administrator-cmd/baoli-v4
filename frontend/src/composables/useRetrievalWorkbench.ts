import { ref, shallowRef, type Ref } from "vue";
import { ElMessage } from "element-plus";

import type {
  RetrievalCandidate,
  RetrievalDecision,
  RetrievalDecisionResult,
  RetrievalResult,
  TaskEvidenceIndex,
} from "@/api/contracts";
import { createTaskRetrieval, getTaskEvidenceIndex, submitRetrievalDecision as submitRetrievalDecisionRequest } from "@/api/audits";

export interface RetrievalWorkbenchState {
  index: Ref<TaskEvidenceIndex | null>;
  indexError: Ref<string | null>;
  indexLoading: Ref<boolean>;
  query: Ref<string>;
  /** 选定的材料范围；空串表示「全部已上传材料」。 */
  materialKey: Ref<string>;
  result: Ref<RetrievalResult | null>;
  error: Ref<string | null>;
  loading: Ref<boolean>;
  decision: Ref<RetrievalDecisionResult | null>;
  decisionPending: Ref<boolean>;
  /** 已保存结论所依据的候选（用于面板回显选中项）。 */
  decidedCandidate: Ref<RetrievalCandidate | null>;
  loadIndex: () => Promise<void>;
  run: (uploadedMaterialKeys: string[]) => Promise<void>;
  submitDecision: (payload: {
    decision: RetrievalDecision;
    note: string;
    selected: RetrievalCandidate | null;
    inputRevision: number;
  }) => Promise<void>;
  reset: () => void;
}

/**
 * 主体检索取证的工作台状态：索引、一次检索、以及对该次候选的人工结论。
 *
 * 三条口径写在实现里，不要弱化：

 * - **检索范围只取当前任务已上传的材料**：没有上传材料时明确提示先上传，
 *   不在前端编造范围，也不用空范围发起跨材料检索。
 * - **新的一次检索作废上一次的结论回显**：候选变了，结论不能跨候选沿用。
 * - **候选身份必须完整提交**（索引号 + 分块号 + 产物条目序号）：服务端按三元组校验
 *   所选候选确实来自本次返回，缺一项会被判成「不在本次候选内」。
 */
export function useRetrievalWorkbench(options: { taskNo: Ref<string> }): RetrievalWorkbenchState {
  const index = shallowRef<TaskEvidenceIndex | null>(null);
  const indexError = ref<string | null>(null);
  const indexLoading = ref(false);
  const query = ref("");
  const materialKey = ref("");
  const result = shallowRef<RetrievalResult | null>(null);
  const error = ref<string | null>(null);
  const loading = ref(false);
  const decision = shallowRef<RetrievalDecisionResult | null>(null);
  const decisionPending = ref(false);

  const decidedCandidate = ref<RetrievalCandidate | null>(null);

  async function loadIndex(): Promise<void> {
    const taskNo = options.taskNo.value;
    indexLoading.value = true;
    indexError.value = null;
    try {
      index.value = await getTaskEvidenceIndex(taskNo);
    } catch (err) {
      index.value = null;
      indexError.value = err instanceof Error ? err.message : "未知原因";
    } finally {
      indexLoading.value = false;
    }
  }

  async function run(uploadedMaterialKeys: string[]): Promise<void> {
    const taskNo = options.taskNo.value;
    const text = query.value.trim();
    if (!text) {
      ElMessage.warning("请先填写要检索的内容");
      return;
    }
    if (!uploadedMaterialKeys.length) {
      ElMessage.warning("当前任务还没有已上传的材料，请先上传材料并执行文件处理节点建立索引");
      return;
    }
    loading.value = true;
    error.value = null;
    // 新的一次检索对应新的候选：上一次的结论回显必须清掉。
    decision.value = null;
    decidedCandidate.value = null;
    try {
      result.value = await createTaskRetrieval(taskNo, {
        query_text: text,
        material_keys: materialKey.value ? [materialKey.value] : uploadedMaterialKeys,
      });
    } catch (err) {
      result.value = null;
      error.value = err instanceof Error ? err.message : "未知原因";
    } finally {
      loading.value = false;
    }
  }

  async function submitDecision(payload: {
    decision: RetrievalDecision;
    note: string;
    selected: RetrievalCandidate | null;
    inputRevision: number;
  }): Promise<void> {
    const taskNo = options.taskNo.value;
    const retrievalNo = result.value?.retrieval_no;
    if (!retrievalNo) return;
    decisionPending.value = true;
    try {
      const selected = payload.selected
        ? {
            index_no: payload.selected.index_no,
            chunk_no: payload.selected.chunk_no,
            source_entry_no: payload.selected.source_entry_no,
          }
        : undefined;
      decision.value = await submitRetrievalDecisionRequest(taskNo, retrievalNo, {
        decision: payload.decision,
        note: payload.note || undefined,
        selected,
        expected_input_revision: payload.inputRevision,
      });
      decidedCandidate.value = payload.selected;
      ElMessage.success("主体核对结论已保存");
    } catch (err) {
      // 提交失败必须让用户看到真实原因（例如候选已过期），不能静默当作已保存。
      ElMessage.error(`保存主体核对结论失败：${err instanceof Error ? err.message : "未知原因"}`);
    } finally {
      decisionPending.value = false;
    }
  }

  function reset(): void {
    index.value = null;
    indexError.value = null;
    indexLoading.value = false;
    query.value = "";
    materialKey.value = "";
    result.value = null;
    error.value = null;
    loading.value = false;
    decision.value = null;
    decidedCandidate.value = null;
    decisionPending.value = false;
  }

  return {
    index,
    indexError,
    indexLoading,
    query,
    materialKey,
    result,
    error,
    loading,
    decision,
    decisionPending,
    decidedCandidate,
    loadIndex,
    run,
    submitDecision,
    reset,
  };
}
