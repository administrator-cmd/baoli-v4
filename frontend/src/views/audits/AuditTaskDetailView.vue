<script setup lang="ts">
import { computed, onMounted, onUnmounted, reactive, ref, shallowRef, watch } from "vue";
import { useRoute, useRouter } from "vue-router";
import { ElMessage, ElMessageBox } from "element-plus";

import type {
  AuditDocument,
  AuditField,
  Evidence,
  ExecuteNodeResponse,
  ExecutionRow,
  FieldCorrectionRequest,
  FieldCorrectionResponse,
  PendingReview,
  Report,
  RiskList,
  TaskReviewRow,
} from "@/api/contracts";
import {
  ApiError,
  apiFileUrl,
  isOperationActive,
  isOperationFinished,
  operationIdempotencyKey,
  OPERATION_POLL_INTERVAL_MS,
  type OperationView,
} from "@/api/http";
import {
  copyAuditTask,
  deleteAuditTask,
  createTaskRetrieval,
  documentFileUrl,
  executeAuditNode,
  getAuditTask,
  getTaskEvidence,
  getTaskEvidenceIndex,
  getTaskReport,
  getTaskReviewDrafts,
  getTaskRisks,
  listTaskExecutions,
  listTaskReviews,
  removeTaskDocumentFile,
  saveTaskReviewDrafts,
  submitFieldCorrections,
  submitRetrievalDecision as submitRetrievalDecisionRequest,
  submitManualReviews,
  updateAuditTask,
  uploadTaskDocument,
  type AuditTaskView,
  type ReplacementImpact,
} from "@/api/audits";
import type {
  RetrievalCandidate,
  RetrievalDecision,
  RetrievalDecisionResult,
  RetrievalResult,
  TaskEvidenceIndex,
} from "@/api/contracts";

import { getExecution } from "@/api/reviews";
import AuditFieldResults from "@/components/audit/AuditFieldResults.vue";
import AuditReportPanel from "@/components/audit/AuditReportPanel.vue";
import AuditRiskResults from "@/components/audit/AuditRiskResults.vue";
import AuditRuleResults from "@/components/audit/AuditRuleResults.vue";
import RetrievalEvidencePanel from "@/components/audit/RetrievalEvidencePanel.vue";
import FieldCorrectionDialog from "@/components/audit/FieldCorrectionDialog.vue";
import PendingReviewCard, { type ReviewDraft } from "@/components/audit/PendingReviewCard.vue";
import TaskMaterialRow from "@/components/audit/TaskMaterialRow.vue";
import DrawerPanel from "@/components/DrawerPanel.vue";
import EvidenceDrawer from "@/components/EvidenceDrawer.vue";
import FailureNotice from "@/components/FailureNotice.vue";
import OperationPanel from "@/components/OperationPanel.vue";
import StateBlock from "@/components/StateBlock.vue";
import StatusBadge from "@/components/StatusBadge.vue";
import { useEvidenceDrawer } from "@/composables/useEvidenceDrawer";
import { useMaterialActions } from "@/composables/useMaterialActions";
import { useOperationTracker } from "@/composables/useOperationTracker";
import { useRequestRunner } from "@/composables/useRequestRunner";
import { useRetrievalWorkbench } from "@/composables/useRetrievalWorkbench";
import { useReviewWorkbench } from "@/composables/useReviewWorkbench";
import {
  isDetailTab,
  manualReviewNodeType,
  workspacesOfNodes,
  type DetailTab,
  type DisplayNode,
} from "@/constants/auditWorkspaces";
import {
  basisTypeLabel,
  evidenceIndexStatusLabel,
  evidenceIndexStatusTone,
  formatDate,
  nodeLabel,
  progressPercent,
  reviewDecisionLabel,
  reviewObjectKindLabel,
  statusLabel,
  statusTone,
} from "@/constants/labels";

/**
 * 审核任务详情。
 *
 * 页头保留任务身份与流程，待办和概览用表格呈现；材料、结果与记录由页签承载。
 *
 * 执行、复核、报告都由服务端真实状态驱动；响应丢失先查询原命令，不重复提交。
 */

const route = useRoute();
const router = useRouter();
const { operation, watching, track, requery, reset: resetTracking } = useOperationTracker();
const { pending, failure, formMessage, run, setFailure } = useRequestRunner();

const taskNo = computed(() => String(route.params.taskNo ?? ""));

// 任务详情整体替换（applyTask），用 shallowRef 避免对含递归 JsonValue 的类型做深解包（TS2589）。
const task = shallowRef<AuditTaskView | null>(null);
/* 材料位操作与原文证据抽屉：各自独立的状态与操作流程，见对应 composable。 */
const {
  busyDocumentNo: uploadingDocumentNo,
  uploadPercent: materialUploadPercent,
  replacementImpact,
  upload: uploadMaterial,
  download: downloadDocument,
  preview: previewDocument,
  remove: removeDocument,
  reset: resetMaterialActions,
} = useMaterialActions({ taskNo, run, refresh: refreshTask });
const {
  field: evidenceField,
  evidence,
  error: evidenceError,
  loading: evidenceLoading,
  openForField: openEvidence,
  openForReview: openReviewEvidence,
  close: closeEvidence,
} = useEvidenceDrawer({ taskNo, run });

const risks = ref<RiskList | null>(null);
const risksError = ref<string | null>(null);
const risksLoading = ref(false);

/* 检索取证 */
const {
  index: evidenceIndex,
  indexError: evidenceIndexError,
  indexLoading: evidenceIndexLoading,
  query: retrievalQuery,
  materialKey: retrievalMaterialKey,
  result: retrievalResult,
  error: retrievalError,
  loading: retrievalLoading,
  decision: retrievalDecision,
  decisionPending: retrievalDecisionPending,
  decidedCandidate,
  loadIndex: loadEvidenceIndex,
  run: runRetrieval,
  submitDecision: submitRetrievalDecision,
  reset: resetRetrieval,
} = useRetrievalWorkbench({ taskNo });

/** 当前已上传材料的材料键：检索范围默认取它们，不在前端编造材料。 */
const uploadableMaterialKeys = computed<string[]>(() =>
  (task.value?.documents ?? [])
    .filter((item) => item.filename)
    .map((item) => item.material_key)
    .filter((key): key is string => typeof key === "string" && key.length > 0),
);

const report = ref<Report | null>(null);
const reportError = ref<string | null>(null);
const reportLoading = ref(false);
const lastExecution = ref<ExecuteNodeResponse | null>(null);
const executingNode = ref<{ code: string; name: string } | null>(null);
const {
  drafts: reviewDrafts,
  touched: draftTouched,
  saveState: draftSaveState,
  saveMessage: draftSaveMessage,
  initialise: initialiseReviewDrafts,
  draftOf: reviewDraftOf,
  setDraft: setReviewDraft,
  payloadOf: reviewDraftPayload,
  restore: restoreReviewDrafts,
  markTouched: markReviewTouched,
  clear: clearReviewWorkbench,
  flush: flushReviewDrafts,
} = useReviewWorkbench({ taskNo });
const selectedReviewNo = ref<string | null>(null);
const tab = ref<DetailTab>("materials");
/** 规则结果页的校验阶段筛选；由工作区入口设置，不由当前节点隐式改写。 */
const loadingTask = ref(false);
const correctionField = ref<AuditField | null>(null);
const correctionResult = ref<FieldCorrectionResponse | null>(null);
const correctionPending = ref(false);
const lastOperationType = ref<string | null>(null);
let executionAttemptKey: string | null = null;

/* 弹窗与抽屉的可见性
 * 这些状态在 resetWorkspace() 里会被统一清理，因此必须与其它页面状态一起声明在
 * 调用之前——声明在后面会让「切换任务清理抽屉」在第一次调用时就踩到暂时性死区。 */
const snapshotOpen = ref(false);
const editVisible = ref(false);
const copyVisible = ref(false);


/** 页面状态代次：切换任务时递增，过期响应不得覆盖当前页面。 */
let stateGeneration = 0;
const activeTaskNo = ref<string | null>(null);

function isStale(token: number, currentTaskNo: string): boolean {
  return token !== stateGeneration || activeTaskNo.value !== currentTaskNo;
}

/* 复核记录 / 执行记录（页签） */
const reviewRecords = ref<TaskReviewRow[]>([]);
const reviewRecordsError = ref<string | null>(null);
const reviewRecordsLoading = ref(false);
const executions = ref<ExecutionRow[]>([]);
const executionsError = ref<string | null>(null);
const executionsLoading = ref(false);
const executionDetail = ref<Awaited<ReturnType<typeof getExecution>> | null>(null);
const executionDetailVisible = ref(false);

async function loadReviewRecords(token: number): Promise<void> {
  const currentTaskNo = taskNo.value;
  reviewRecordsLoading.value = true;
  reviewRecordsError.value = null;
  try {
    const payload = await listTaskReviews(currentTaskNo);
    if (isStale(token, currentTaskNo)) return;
    reviewRecords.value = payload.items;
  } catch (error) {
    if (isStale(token, currentTaskNo)) return;
    reviewRecords.value = [];
    reviewRecordsError.value = error instanceof Error ? error.message : "复核记录读取失败";
  } finally {
    if (!isStale(token, currentTaskNo)) reviewRecordsLoading.value = false;
  }
}

async function loadExecutions(token: number): Promise<void> {
  const currentTaskNo = taskNo.value;
  executionsLoading.value = true;
  executionsError.value = null;
  try {
    const payload = await listTaskExecutions(currentTaskNo);
    if (isStale(token, currentTaskNo)) return;
    executions.value = payload.items;
  } catch (error) {
    if (isStale(token, currentTaskNo)) return;
    executions.value = [];
    executionsError.value = error instanceof Error ? error.message : "执行记录读取失败";
  } finally {
    if (!isStale(token, currentTaskNo)) executionsLoading.value = false;
  }
}

async function loadReviewDrafts(token: number): Promise<void> {
  if (!task.value || task.value.status !== "WAITING_REVIEW" || !task.value.pending_reviews.length) {
    return;
  }
  const currentTaskNo = task.value.task_no;
  await restoreReviewDrafts();
  if (isStale(token, currentTaskNo)) return;
}

/** 表单里的「保存草稿」按钮：立即写入服务端。 */
function saveDraftsNow(): void {
  void flushReviewDrafts();
}

async function openExecutionDetail(row: ExecutionRow): Promise<void> {
  executionDetailVisible.value = true;
  executionDetail.value = null;
  try {
    executionDetail.value = await getExecution(row.execution_id);
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : "执行详情读取失败");
    executionDetailVisible.value = false;
  }
}

/** 模板里的重试按钮：用当前页代次重新读取对应列表。 */
function retryLoadReviewRecords(): void {
  void loadReviewRecords(stateGeneration);
}

function retryLoadExecutions(): void {
  void loadExecutions(stateGeneration);
}

/* 节点流程 */
const currentNodeCode = computed(() => task.value?.current_node.node_code ?? null);
const currentNode = computed(() => task.value?.current_node ?? null);
/**
 * 当前任务的工作区：按后端返回的**展示结构**（`display.nodes`）算出。
 *
 * 只展示当前场景实际配置的节点对应的工作区；统一模板协议里未被本场景选中的
 * 能力类型不出现。旧任务没有 `display` 时退回任务自己的 `nodes`——
 * 那里同样只有本任务真实配置的节点，不凭空补一份固定阶段表。
 */
/** 本任务真实配置的校验阶段（能力类型），供规则结果页的阶段下拉使用。 */
const taskWorkspaceNodeTypes = computed(() => {
  const types = taskWorkspaces.value.map((item) => item.nodeType);
  return types.filter((item, index) => types.indexOf(item) === index);
});

const taskWorkspaces = computed(() =>
  workspacesOfNodes(task.value?.display?.nodes ?? task.value?.nodes ?? []),
);
const currentNodeName = computed(() => task.value?.current_node.name || nodeLabel(currentNodeCode.value));
const isManualReviewNode = computed(
  () => currentNode.value?.node_type === manualReviewNodeType,
);
const currentStepIndex = computed(() => {
  if (!task.value) return 0;
  const index = task.value.nodes.findIndex((node) => node.node_code === task.value?.current_node.node_code);
  return index < 0 ? 0 : index;
});

/**
 * 节点在流程条上的呈现状态。
 *
 * SKIPPED 必须与「还没执行」分开：它已经执行过并给出了结论（例如本模板未配置此类检查），
 * 显示成灰色等待会让人以为流程停在该节点。
 */
function stepStatus(node: { node_code: string; status: string }): "wait" | "process" | "finish" | "error" | "success" {
  const isCurrent = node.node_code === task.value?.current_node.node_code && task.value?.status !== "COMPLETED";
  if (node.status === "FAILED" || node.status === "BLOCKED") return "error";
  if (isCurrent) return "process";
  if (node.status === "SUCCEEDED") return "success";
  // 已明确跳过（未配置该节点检查）：用 finish 表示流程已走过，不用 gray 的 wait。
  if (node.status === "SKIPPED") return "finish";
  // 被失效等待重算的记录不代表最终状态，按未执行呈现。
  return "wait";
}

/** 节点被跳过时的真实原因；没有记录时不臆造说明。 */
function stepDescription(node: { node_code: string; status?: string; skip_reason?: string | null }): string | undefined {
  if (node.skip_reason) {
    return node.skip_reason;
  }
  if (node.status === "SKIPPED" && !node.skip_reason) {
    return "已执行并显式跳过，不代表该检查通过";
  }
  return undefined;
}

const uploadedDocumentCount = computed(() => task.value?.documents.filter((document) => document.filename).length ?? 0);
const missingRequiredMaterials = computed(
  () => task.value?.documents.filter((document) => document.required && !document.filename) ?? [],
);
const hasAnyUpload = computed(() => uploadedDocumentCount.value > 0);
/**
 * 节点实例码 → 能力类型。
 *
 * `node_code` 是用户配置的**实例码**（例如 `INT_A`），`node_type` 才是能力类型
 * （`INTERNAL_VALIDATE`）。页面判断「这是哪一类节点」必须用能力类型：拿实例码与
 * 能力类型比较，在用户改了实例码之后恒为假，对应的工作区与进度会静默失效。
 *
 * 历史任务没有 `node_type` 时按后端同一口径回退（那时实例码本身就是能力类型）。
 */
function nodeTypeOf(code: string | null | undefined): string | null {
  if (!code) {
    return null;
  }
  const node = task.value?.nodes.find((item) => item.node_code === code);
  return node?.node_type ?? code;
}
const extractionFinished = computed(
  () =>
    task.value?.nodes.some(
      (node) => nodeTypeOf(node.node_code) === "FIELD_EXTRACTION" && node.status === "SUCCEEDED",
    ) ?? false,
);
const riskNode = computed(() => task.value?.nodes.find((node) => nodeTypeOf(node.node_code) === "RISK_RATING") ?? null);
const riskNodeExecuted = computed(() => riskNode.value?.status === "SUCCEEDED");
// 未配置的检查类别：类型级汇总用于「这类检查有没有配」；实例级清单用于
// 「当前这个节点是不是未配置的类别」。两者口径不同，不能交叉比较。
const riskConfigured = computed(
  () => !(task.value?.unconfigured_check_node_types ?? []).includes("RISK_RATING"),
);
const currentNodeUnconfigured = computed(
  () => !!task.value && (task.value.unconfigured_check_node_codes ?? []).includes(task.value.current_node.node_code),
);
const canEnterManualReview = computed(() => task.value?.status === "READY" && isManualReviewNode.value);
const atWaitingReview = computed(() => task.value?.status === "WAITING_REVIEW");

const prerequisites = computed(() => {
  if (!task.value) {
    return [] as Array<{ label: string; satisfied: boolean }>;
  }
  // 前置条件按**能力类型**判断：当前节点的 `node_code` 是用户配置的实例码。
  const nodeType = nodeTypeOf(task.value.current_node.node_code);
  const items: Array<{ label: string; satisfied: boolean }> = [
    { label: "任务不在后台执行中", satisfied: task.value.status !== "RUNNING" && !watching.value },
    {
      label: "任务未结束（未全部完成、未失败）",
      satisfied: task.value.status !== "COMPLETED" && task.value.status !== "FAILED",
    },
  ];
  if (nodeType === "MATERIAL_CHECK") {
    items.push({
      label: `必传材料已全部上传（还缺 ${missingRequiredMaterials.value.length} 项）`,
      satisfied: missingRequiredMaterials.value.length === 0,
    });
  }
  if (nodeType === "DOCUMENT_PREPARE") {
    items.push({ label: "至少已上传一份材料", satisfied: hasAnyUpload.value });
  }
  if (nodeType === "MANUAL_REVIEW" && task.value.status === "WAITING_REVIEW") {
    items.push({ label: "任务已停在人工复核等待点（提交复核结论，不再执行节点）", satisfied: true });
  }
  return items;
});

const unsatisfiedPrerequisites = computed(() => prerequisites.value.filter((item) => !item.satisfied));
const canExecuteNode = computed(
  () => !pending.value && !watching.value && unsatisfiedPrerequisites.value.length === 0 && task.value?.status !== "WAITING_REVIEW",
);

const executeDisabledReason = computed(() => {
  if (!task.value) return null;
  if (atWaitingReview.value) return "任务正停在人工复核等待点，请在「人工复核」页签提交复核结论；这里不再提供节点执行。";
  if (task.value.status === "COMPLETED") return "任务已全部完成，没有可继续执行的节点。";
  if (task.value.status === "FAILED") return "任务已失败，请按失败原因处理后复制为新任务或联系管理员。";
  if (watching.value) return "正在查询上一次执行的服务端状态，请等待查询结束。";
  if (unsatisfiedPrerequisites.value.length) {
    return `前置条件未满足：${unsatisfiedPrerequisites.value.map((item) => item.label).join("；")}。`;
  }
  return null;
});

/* 唯一主操作 */

interface PrimaryAction {
  label: string;
  /** 主操作要打开的页签；为空表示调用 run()。 */
  tab?: DetailTab;
  run?: () => void;
  disabled: boolean;
  /** 不能执行的原因；按钮必须能说清为什么点不了。 */
  reason: string | null;
  loading: boolean;
}

/**
 * 页头唯一的主操作。
 *
 * 同一个位置在不同状态下给出唯一明确的下一步：
 *   执行中 → 查看进度；等待复核 → 进入复核；已完成 → 查看结果；
 *   失败/阻断 → 查看原因；可执行 → 执行当前节点。
 * 任务未到可执行状态时不显示一个点不动的「执行当前节点」。
 */
const primaryAction = computed<PrimaryAction>(() => {
  if (!task.value) {
    return { label: "刷新任务", run: () => void refreshTask(), disabled: false, reason: null, loading: loadingTask.value };
  }
  if (atWaitingReview.value) {
    const pendingCount = task.value.pending_reviews.length;
    return {
      label: pendingCount ? `进入复核（本批 ${pendingCount} 项）` : "查看复核状态",
      tab: "reviews",
      disabled: false,
      reason: null,
      loading: false,
    };
  }
  if (watching.value || task.value.status === "RUNNING") {
    return { label: "查看执行进度", tab: "executions", disabled: false, reason: null, loading: false };
  }
  if (task.value.status === "COMPLETED") {
    return { label: "查看审核结果", tab: "rules", disabled: false, reason: null, loading: false };
  }
  if (task.value.status === "FAILED" || task.value.status === "BLOCKED") {
    return {
      label: "查看失败原因",
      tab: "executions",
      disabled: false,
      reason: null,
      loading: false,
    };
  }
  return {
    label: canEnterManualReview.value ? "进入人工复核" : "执行当前节点",
    run: () => void executeCurrentNode(),
    disabled: !canExecuteNode.value,
    reason: executeDisabledReason.value,
    loading: watching.value || pending.value,
  };
});

function runPrimaryAction(): void {
  const action = primaryAction.value;
  if (action.disabled) {
    formMessage.value = action.reason ?? "当前节点暂时不能执行。";
    return;
  }
  if (action.tab) {
    setTab(action.tab);
    return;
  }
  action.run?.();
}

/* 当前待办与阻断原因 */

interface BlockerItem {
  tone: "error" | "warning" | "info";
  title: string;
  description: string;
  /** 可选的下一步：跳转到能处理它的页签。 */
  tab?: DetailTab;
  tabLabel?: string;
}

/**
 * 当前需要用户处理的事项与阻断原因。
 *
 * 只列服务端真实状态推出的原因：必传缺件、节点未配置、执行失败、前置条件不满足、
 * 待复核项、读取失败。没有阻断时不显示这一节，不用「一切正常」占位。
 */
const blockers = computed<BlockerItem[]>(() => {
  const items: BlockerItem[] = [];
  if (!task.value) {
    return items;
  }
  if (missingRequiredMaterials.value.length) {
    items.push({
      tone: "error",
      title: `还有 ${missingRequiredMaterials.value.length} 项必传材料没有上传`,
      description: missingRequiredMaterials.value.map((item) => item.material_name).join("、"),
      tab: "materials",
      tabLabel: "去材料页上传",
    });
  }
  if (task.value.current_node.failure_message) {
    items.push({
      tone: "error",
      title: `上次执行失败：${task.value.current_node.failure_message}`,
      description: "节点执行失败不是业务结论，需要按失败原因处理后重新执行。",
      tab: "executions",
      tabLabel: "查看执行记录",
    });
  }
  if (currentNodeUnconfigured.value) {
    items.push({
      tone: "warning",
      title: `本模板未配置「${currentNodeName.value}」的检查规则`,
      description: "执行后会记为跳过并说明原因，不代表该检查通过。",
    });
  }
  if (unsatisfiedPrerequisites.value.length) {
    items.push({
      tone: "warning",
      title: "当前节点还不能执行",
      description: unsatisfiedPrerequisites.value.map((item) => item.label).join("；"),
    });
  }
  if (task.value.pending_reviews.length) {
    items.push({
      tone: "info",
      title: queuedReviewCount.value
        ? `本批 ${task.value.pending_reviews.length} 项待复核，其后还有 ${queuedReviewCount.value} 项排队`
        : `有 ${task.value.pending_reviews.length} 项等待人工复核`,
      description: "系统无法自动判定的项需要逐条给出业务结论；先提交字段事实，重算后再裁决规则。页面只提交本批。",
      tab: "reviews",
      tabLabel: "进入复核",
    });
  }
  if (risksError.value) {
    items.push({
      tone: "error",
      title: "风险结果读取失败",
      description: `${risksError.value}。这不代表没有风险。`,
      tab: "risks",
      tabLabel: "查看风险页",
    });
  }
  if (reportError.value) {
    items.push({
      tone: "error",
      title: "报告读取失败",
      description: `${reportError.value}。这不代表报告尚未生成。`,
      tab: "report",
      tabLabel: "查看报告页",
    });
  }
  return items;
});

/**
 * 检索结论提交：把「本次结论对应的输入修订」并入载荷。
 *
 * 输入修订是任务级事实（页面持有），候选与结论是检索工作台的状态；
 * 服务端据此拒绝基于过期输入的结论。
 */
/** 人工复核卡片上的「查看原文依据」：打开当前待办所引用的证据。 */
function openActiveReviewEvidence(): void {
  const review = activeReview.value;
  if (review) void openReviewEvidence(review);
}

function onSubmitRetrievalDecision(payload: {
  decision: RetrievalDecision;
  note: string;
  selected: RetrievalCandidate | null;
}): void {
  void submitRetrievalDecision({ ...payload, inputRevision: task.value?.input_revision ?? 0 });
}

/* 页签与 URL */

/**
 * 切换页签。
 *
 * 页签写回 URL：这样刷新、分享链接、以及轮询结束后的地址清理都不会把用户
 * 手动选择的页签丢掉——页签的真实来源只有两个（URL 与用户点击），轮询不参与。
 *
 * 入参放宽为 string，因为 el-tabs 的 v-model 事件载荷是 string | number；
 * 非法值直接忽略，不写入页签状态（与 URL 的处理口径一致）。
 */
function setTab(next: string | number): void {
  if (!isDetailTab(next)) {
    return;
  }
  tab.value = next;
  if (route.query.tab === next) {
    return;
  }
  void router.replace({
    name: "audit-task-detail",
    params: { taskNo: taskNo.value },
    query: { ...route.query, tab: next },
  });
}

/** URL 里的 tab 只有合法时才生效；非法值不写入页签状态。 */
function tabFromRoute(): DetailTab | null {
  const value = route.query.tab;
  return isDetailTab(value) ? value : null;
}

/* 关键统计 */

const fieldExtractedCount = computed(
  () => task.value?.fields.filter((field) => field.status === "PRESENT" || field.status === "REVIEWED").length ?? 0,
);
const ruleAbnormalCount = computed(() => task.value?.rules.filter((rule) => rule.status === "ABNORMAL").length ?? 0);
/* 阶段与进度 */

const executionPercent = computed(() => {
  const execution = lastExecution.value;
  const executedNode = executingNode.value;
  if (!execution || !executedNode || !task.value) return null;
  const summary = execution.result_summary;
  const counter = (key: string) => (typeof summary[key] === "number" ? (summary[key] as number) : null);
  const executedType = nodeTypeOf(executedNode.code);
  if (executedType === "MATERIAL_CHECK") return progressPercent(counter("uploaded"), task.value.documents.length);
  if (executedType === "DOCUMENT_PREPARE") return progressPercent(counter("processed_documents"), uploadedDocumentCount.value);
  if (executedType === "FIELD_EXTRACTION") return progressPercent(counter("field_results"), task.value.fields.length);
  return null;
});

const reportDownloadUrl = computed(() => {
  if (!task.value || !report.value?.content || !report.value.report_generated || !report.value.current) return null;
  return apiFileUrl(`/audit-tasks/${task.value.task_no}/reports/download`);
});

/**
 * 本批可提交的复核项。
 *
 * **以服务端给出的 `pending_review_batch.review_nos` 为准**，不是把页面看到的
 * 全部待办一起提交：复核是分批判决的（字段事实先于规则结论），不属于本批的待办
 * 留在服务端，一次性全提交会被服务端以 409 拒绝，而页面没有任何自救手段。
 */
const reviewBatch = computed<PendingReview[]>(() => {
  const batch = task.value?.pending_review_batch;
  if (!batch) return [];
  const wanted = new Set(batch.review_nos);
  return (task.value?.pending_reviews ?? []).filter((item) => wanted.has(item.review_no));
});

/** 本批哈希与等待点落库的哈希不一致：待办集合被改动过，必须刷新后再提交。 */
const reviewBatchStale = computed(() => task.value?.pending_review_batch?.stale === true);

/** 排队中但不属于本批的待办数（字段批与规则批并存时非零）。 */
const queuedReviewCount = computed(() => {
  const batch = task.value?.pending_review_batch;
  if (!batch) return 0;
  return Math.max(0, batch.all_pending_count - batch.count);
});

const reviewSubmitDisabledReason = computed(() => {
  if (!task.value) return null;
  if (task.value.status !== "WAITING_REVIEW") {
    return `任务当前状态为「${statusLabel(task.value.status)}」，未到人工复核等待点，不能提交复核结论。`;
  }
  if (watching.value) return "正在查询上一次复核提交的服务端状态，请在本次操作结束后再提交。";
  if (reviewBatchStale.value) {
    return "待复核集合已变化（本批哈希与等待点不一致），请先「刷新」重新读取当前批次再提交。";
  }
  if (!reviewBatch.value.length) {
    return "当前没有待复核项；请先「刷新」确认服务端是否已生成待办，页面不会自行跳过。";
  }
  const incomplete = reviewBatch.value.filter((review) => {
    const draft = reviewDraftOf(review.review_no);
    return !draft.decision || !draft.note.trim() || (requiresConfirmedFieldValue(draft.decision) && !draft.fieldValue.trim());
  });
  if (incomplete.length) return `还有 ${incomplete.length} 项复核没有选择结论或填写业务说明。`;
  return null;
});

const retryableOperation = computed(
  () => operation.value?.status === "FAILED" || operation.value?.status === "INTERRUPTED"
    ? lastOperationType.value === "MANUAL_REVIEW_RESUME" || lastOperationType.value === "NODE_EXECUTE"
    : false,
);

const activeReview = computed(
  () => task.value?.pending_reviews.find((review) => review.review_no === selectedReviewNo.value) ?? null,
);
const activeReviewDraft = computed<ReviewDraft>(() =>
  activeReview.value ? reviewDraftOf(activeReview.value.review_no) : reviewDraftOf(""),
);

function updateActiveReviewDraft(draft: ReviewDraft): void {
  if (!activeReview.value) return;
  // 写草稿与标记「用户已编辑」是同一件事，由 workbench 一起做，页面不分开调。
  setReviewDraft(activeReview.value.review_no, draft);
}

/**
 * 用任务当前待办初始化复核表单。
 *
 * 保留用户已经填过的内容与已选中的待办；页面预填的「系统建议值」不算用户输入
 * （是否算用户输入由 workbench 的 touched 决定，见 useReviewWorkbench）。
 */
function seedReviewDrafts(auditTask: AuditTaskView): void {
  // 预填口径由 workbench 统一负责（含「保留用户已填内容」），页面不再自己拼草稿对象。
  initialiseReviewDrafts(auditTask.pending_reviews, true);
  const stillPresent = auditTask.pending_reviews.some((review) => review.review_no === selectedReviewNo.value);
  if (!stillPresent) {
    selectedReviewNo.value = auditTask.pending_reviews[0]?.review_no ?? null;
  }
}

function applyTask(auditTask: AuditTaskView): void {
  task.value = auditTask;
  seedReviewDrafts(auditTask);
}

/**
 * 切换任务时的完整清理。
 *
 * 旧任务的风险、报告、复核草稿、执行记录、检索结果与所有抽屉状态都必须清掉：
 * 留着会让用户在新任务上看到上一个任务的数字和结论，比空页面危险得多。
 */
function resetWorkspace(): void {
  resetTracking();
  stateGeneration += 1;
  activeTaskNo.value = null;
  task.value = null;
  risks.value = null;
  risksError.value = null;
  risksLoading.value = false;
  report.value = null;
  reportError.value = null;
  reportLoading.value = false;
  lastExecution.value = null;
  executingNode.value = null;
  selectedReviewNo.value = null;
  /* 独立业务区块各自重置：证据抽屉、检索取证、材料操作、复核草稿。 */
  closeEvidence();
  resetRetrieval();
  resetMaterialActions();
  clearReviewWorkbench();
  correctionField.value = null;
  correctionResult.value = null;
  correctionPending.value = false;
  lastOperationType.value = null;
  executionAttemptKey = null;
  reviewRecords.value = [];
  executions.value = [];
  reviewRecordsError.value = null;
  executionsError.value = null;
  reviewRecordsLoading.value = false;
  executionsLoading.value = false;
  executionDetail.value = null;
  executionDetailVisible.value = false;
  /* 抽屉与弹窗 */
  snapshotOpen.value = false;
  editVisible.value = false;
  copyVisible.value = false;
  formMessage.value = "";
  loadingTask.value = false;
}

async function loadRisks(currentTaskNo: string, token: number): Promise<void> {
  risksLoading.value = true;
  risksError.value = null;
  try {
    const value = await getTaskRisks(currentTaskNo);
    if (isStale(token, currentTaskNo)) return;
    risks.value = value;
  } catch (error) {
    if (isStale(token, currentTaskNo)) return;
    risks.value = null;
    risksError.value = error instanceof Error ? error.message : "未知原因";
  } finally {
    if (!isStale(token, currentTaskNo)) risksLoading.value = false;
  }
}

async function loadReport(currentTaskNo: string, token: number): Promise<void> {
  reportLoading.value = true;
  reportError.value = null;
  try {
    const value = await getTaskReport(currentTaskNo);
    if (isStale(token, currentTaskNo)) return;
    report.value = value;
  } catch (error) {
    if (isStale(token, currentTaskNo)) return;
    report.value = null;
    reportError.value = error instanceof Error ? error.message : "未知原因";
  } finally {
    if (!isStale(token, currentTaskNo)) reportLoading.value = false;
  }
}

async function fetchTaskState(currentTaskNo: string, token = stateGeneration): Promise<void> {
  activeTaskNo.value = currentTaskNo;
  const auditTask = await getAuditTask(currentTaskNo);
  if (isStale(token, currentTaskNo)) return;
  applyTask(auditTask);
  await Promise.allSettled([
    loadRisks(currentTaskNo, token),
    loadReport(currentTaskNo, token),
    loadEvidenceIndex(),
    loadReviewRecords(token),
    loadExecutions(token),
    loadReviewDrafts(token),
  ]);
}

async function loadTask(currentTaskNo: string): Promise<void> {
  loadingTask.value = true;
  try {
    await run(async () => {
      await fetchTaskState(currentTaskNo);
    });
  } finally {
    loadingTask.value = false;
  }
}

async function refreshTask(): Promise<void> {
  if (!task.value) return;
  const currentTaskNo = task.value.task_no;
  loadingTask.value = true;
  try {
    await run(async () => {
      await fetchTaskState(currentTaskNo);
    });
  } finally {
    loadingTask.value = false;
  }
}

/* 后台操作查询 */
function trackOperation(value: OperationView, token = stateGeneration): void {
  // 提交后到响应之间可能已经切了任务：代次或对象不符时不得写入本页状态。
  if (token !== stateGeneration || value.object_no !== activeTaskNo.value) {
    return;
  }
  executionAttemptKey = null;
  lastOperationType.value = value.operation_type;
  setExecutionResult(value);
  track(value, {
    onFinished: async (finished) => {
      if (token !== stateGeneration) return;
      await refreshAfterOperation(finished, token);
    },
    onError: (error) => {
      if (token !== stateGeneration) return;
      setFailure(error);
    },
  });
}

function setExecutionResult(value: OperationView): void {
  const result = value.result;
  if (result && typeof result === "object" && "result_summary" in result) {
    lastExecution.value = result as unknown as ExecuteNodeResponse;
  }
}

async function refreshAfterOperation(finished: OperationView, token = stateGeneration): Promise<void> {
  if (token !== stateGeneration) return;
  setExecutionResult(finished);
  const currentTaskNo = task.value?.task_no;
  if (currentTaskNo) {
    await fetchTaskState(currentTaskNo, token);
    if (isStale(token, currentTaskNo)) return;
  }
  if (isOperationFinished(finished.status)) {
    await dropOperationQuery();
  }
}

/** 清理地址里的 operation 参数；tab 必须保留，否则刷新会丢掉用户选择的页签。 */
async function dropOperationQuery(): Promise<void> {
  if (!task.value || typeof route.query.operation !== "string") return;
  const { operation: _dropped, ...rest } = route.query;
  await router.replace({ name: "audit-task-detail", params: { taskNo: task.value.task_no }, query: rest });
}

async function resumeOperation(operationNo: string, currentTaskNo: string | null, token = stateGeneration): Promise<void> {
  try {
    const { getOperation } = await import("@/api/http");
    const current = await getOperation(operationNo);
    if (isStale(token, currentTaskNo ?? "")) return;
    lastOperationType.value = current.operation_type;
    setExecutionResult(current);
    if (isOperationFinished(current.status)) {
      operation.value = current;
      if (currentTaskNo) await fetchTaskState(currentTaskNo, token);
      await dropOperationQuery();
      return;
    }
    trackOperation(current, token);
  } catch (error) {
    if (token !== stateGeneration) return;
    setFailure(error);
  }
}

function resumeLatestOperation(token = stateGeneration): void {
  const latest = task.value?.latest_operation;
  if (latest && isOperationActive(latest.status)) {
    executingNode.value = { code: latest.target_node ?? "", name: nodeLabel(latest.target_node) };
    trackOperation(latest, token);
    return;
  }
  if (latest) {
    lastOperationType.value = latest.operation_type;
    operation.value = latest;
  }
}

/**
 * 从路由同步页面状态。
 *
 * 任务切换的处理规则（与页签的真实来源一致）：
 *   URL 没有 tab   → 重置为材料页；
 *   URL 有合法 tab → 使用 URL 的 tab；
 *   URL 有非法 tab → 不写入，退回材料页。
 * 轮询与刷新只调用同一个任务的 fetchTaskState，不触碰 tab，因此不会覆盖用户的选择。
 */
async function syncFromRoute(): Promise<void> {
  const currentTaskNo = taskNo.value;
  if (!currentTaskNo) {
    return;
  }
  const routeTab = tabFromRoute();
  if (currentTaskNo !== task.value?.task_no) {
    resetWorkspace();
    tab.value = routeTab ?? "materials";
    await loadTask(currentTaskNo);
    if (!task.value) return;
    const operationNo = typeof route.query.operation === "string" ? route.query.operation : null;
    if (operationNo) {
      await resumeOperation(operationNo, currentTaskNo, stateGeneration);
    } else {
      resumeLatestOperation(stateGeneration);
    }
    return;
  }
  // 同一任务：URL 里显式给出的合法 tab 生效（浏览器前进/后退、外部链接）。
  if (routeTab && routeTab !== tab.value) {
    tab.value = routeTab;
  }
  const operationNo = typeof route.query.operation === "string" ? route.query.operation : null;
  if (operationNo && operationNo !== operation.value?.operation_no) {
    await resumeOperation(operationNo, currentTaskNo, stateGeneration);
  }
}

/* 任务动作 */
async function executeCurrentNode(): Promise<void> {
  if (!task.value || !canExecuteNode.value) {
    formMessage.value = executeDisabledReason.value ?? "当前节点暂时不能执行。";
    return;
  }
  const currentTaskNo = task.value.task_no;
  const nodeCode = task.value.current_node.node_code;
  const nodeName = currentNodeName.value;
  const token = stateGeneration;
  await run(async () => {
    const attemptKey = executionAttemptKey ?? operationIdempotencyKey("NODE_EXECUTE", currentTaskNo, nodeCode);
    executionAttemptKey = attemptKey;
    executingNode.value = { code: nodeCode, name: nodeName };
    lastExecution.value = null;
    replacementImpact.value = null;
    try {
      const accepted = await executeAuditNode(currentTaskNo, nodeCode, attemptKey, task.value?.input_revision);
      if (isStale(token, currentTaskNo)) {
        return;
      }
      trackOperation(accepted, token);
      await router.replace({
        name: "audit-task-detail",
        params: { taskNo: currentTaskNo },
        query: { ...route.query, operation: accepted.operation_no },
      });
    } catch (error) {
      if (isStale(token, currentTaskNo)) {
        return;
      }
      setFailure(error);
      if (error instanceof Error && "code" in error && (error as ApiError).code === "OPERATION_ALREADY_ACTIVE") {
        applyTask(await getAuditTask(currentTaskNo));
        resumeLatestOperation(token);
      }
    }
  });
}

async function retryOperation(operationType: string): Promise<void> {
  if (operationType === "MANUAL_REVIEW_RESUME") {
    await submitReviews();
    return;
  }
  if (operationType === "NODE_EXECUTE") {
    await executeCurrentNode();
    return;
  }
  formMessage.value = "该后台操作没有可用的重试入口，请用「再次查询」查看服务端真实状态。";
}


/* 字段纠错与证据 */
function openCorrection(field: AuditField): void {
  correctionResult.value = null;
  correctionField.value = field;
}

async function submitCorrection(payload: FieldCorrectionRequest): Promise<void> {
  if (!task.value) return;
  const currentTaskNo = task.value.task_no;
  const token = stateGeneration;
  correctionPending.value = true;
  try {
    const result = await submitFieldCorrections(currentTaskNo, { ...payload, input_revision: task.value.input_revision });
    if (isStale(token, currentTaskNo)) return;
    correctionResult.value = result;
    await fetchTaskState(currentTaskNo, token);
  } catch (error) {
    if (isStale(token, currentTaskNo)) return;
    setFailure(error);
  } finally {
    correctionPending.value = false;
  }
}

function closeCorrection(): void {
  correctionField.value = null;
  correctionResult.value = null;
}



/* 人工复核提交 */
function requiresConfirmedFieldValue(decision: string): boolean {
  return decision === "CONFIRM_FIELD" || decision === "MODIFY_FIELD";
}

async function submitReviews(): Promise<void> {
  if (!task.value || reviewSubmitDisabledReason.value) {
    formMessage.value = reviewSubmitDisabledReason.value ?? "当前不能提交复核结论。";
    return;
  }
  const currentTaskNo = task.value.task_no;
  const token = stateGeneration;
  const currentRevision = task.value.input_revision;
  // 提交载荷与草稿暂存共用同一份组装口径（workbench.payloadOf），不再各写一套。
  // 集合来自**本批**：不属于本批的待办留在服务端，下一批再处理。
  const reviews = reviewBatch.value.map((review) => {
    const draft = reviewDraftOf(review.review_no);
    return {
      review_no: review.review_no,
      decision: draft.decision as "CONFIRM_ABNORMAL" | "MARK_NORMAL" | "EVIDENCE_INSUFFICIENT" | "CONFIRM_FIELD" | "MODIFY_FIELD",
      business_note: draft.note.trim(),
      ...(requiresConfirmedFieldValue(draft.decision) ? { field_value: draft.fieldValue.trim() } : {}),
      basis_type: (draft.basisType || "HUMAN_STATEMENT") as "HUMAN_STATEMENT" | "DOCUMENT_EVIDENCE" | "UPLOAD_RECORD",
    };
  });
  await run(async () => {
    // 人工复核等待点的**实例码**由用户配置：从任务冻结的节点里按能力类型取出，
    // 不写死 MANUAL_REVIEW 当实例码（用户改过实例码后那条记录就找不到对应节点）。
    const reviewNode = task.value?.nodes.find((node) => nodeTypeOf(node.node_code) === manualReviewNodeType) ?? null;
    const attemptKey = executionAttemptKey ?? operationIdempotencyKey(reviewNode?.node_code ?? manualReviewNodeType, currentTaskNo);
    executionAttemptKey = attemptKey;
    executingNode.value = { code: reviewNode?.node_code ?? "", name: reviewNode?.name ?? "人工复核" };
    replacementImpact.value = null;
    try {
      const accepted = await submitManualReviews(currentTaskNo, reviews, attemptKey, currentRevision);
      if (isStale(token, currentTaskNo)) {
        return;
      }
      // 本批已提交：清空「用户已编辑」记录并按当前待办重建表单，避免把上一批的
      // 输入带到下一批（服务端已落库，重载后由草稿恢复重新填入）。
      clearReviewWorkbench();
      initialiseReviewDrafts(task.value?.pending_reviews ?? []);
      trackOperation(accepted, token);
      await router.replace({
        name: "audit-task-detail",
        params: { taskNo: currentTaskNo },
        query: { ...route.query, operation: accepted.operation_no },
      });
    } catch (error) {
      if (isStale(token, currentTaskNo)) {
        return;
      }
      setFailure(error);
      if (error instanceof Error && "code" in error && (error as ApiError).code === "OPERATION_ALREADY_ACTIVE") {
        applyTask(await getAuditTask(currentTaskNo));
        resumeLatestOperation(token);
      }
    }
  });
}

/* 任务资料 / 复制 / 删除。任务不能反向生成方案。 */
const editForm = reactive({ task_name: "", remark: "" });
const editSaving = ref(false);

function openEdit(): void {
  if (!task.value) return;
  editForm.task_name = task.value.task_name;
  editForm.remark = task.value.remark ?? "";
  editVisible.value = true;
}

async function submitEdit(): Promise<void> {
  if (!task.value || !editForm.task_name.trim()) {
    ElMessage.warning("请填写任务名称");
    return;
  }
  editSaving.value = true;
  try {
    await updateAuditTask(task.value.task_no, { task_name: editForm.task_name.trim(), remark: editForm.remark.trim() || null });
    ElMessage.success("任务资料已保存（执行快照不受影响）");
    editVisible.value = false;
    await refreshTask();
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : "保存失败");
  } finally {
    editSaving.value = false;
  }
}

const copyName = ref("");
const copySaving = ref(false);
let copyAttemptKey: string | null = null;

function openCopy(): void {
  if (!task.value) return;
  copyName.value = `${task.value.task_name} 副本`;
  copyVisible.value = true;
}

async function submitCopy(): Promise<void> {
  if (!task.value || !copyName.value.trim()) {
    ElMessage.warning("请填写新任务名称");
    return;
  }
  copySaving.value = true;
  try {
    // 与新建任务同一口径：幂等标识在本次尝试内复用，响应丢失后重发返回原任务。
    copyAttemptKey =
      copyAttemptKey ?? operationIdempotencyKey("TASK_COPY", task.value.task_no, copyName.value.trim());
    const created = await copyAuditTask(task.value.task_no, copyName.value.trim(), copyAttemptKey);
    copyAttemptKey = null;
    ElMessage.success("已按方案当前范围创建新任务（材料与结果重新开始，原任务报告保留）");
    copyVisible.value = false;
    await router.push({ name: "audit-task-detail", params: { taskNo: created.task_no } });
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : "复制失败");
  } finally {
    copySaving.value = false;
  }
}

async function removeTask(): Promise<void> {
  if (!task.value) return;
  try {
    await ElMessageBox.confirm(
      `确认删除任务「${task.value.task_name}」？删除为逻辑删除：历史事实、文件、证据与报告保留；执行中的任务会被服务端拒绝。`,
      "删除审核任务",
      { type: "warning", confirmButtonText: "删除", cancelButtonText: "取消" },
    );
  } catch {
    return;
  }
  try {
    await deleteAuditTask(task.value.task_no);
    ElMessage.success("任务已移入已删除列表");
    await router.push({ name: "audit-tasks" });
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : "删除失败");
  }
}

const pollIntervalSeconds = OPERATION_POLL_INTERVAL_MS / 1000;

const planText = computed(() => {
  if (!task.value) return "";
  if (task.value.is_legacy_config || !task.value.plan) return "历史任务配置（无方案关联）";
  return `${task.value.plan.plan_name} · 冻结场景第 ${task.value.plan.scenario_version_no} 版`;
});

/* 任务规则快照：执行范围就是创建任务时勾选的那些规则 */
const enabledRules = computed(() => task.value?.scope?.enabled_rules ?? []);
const enabledCalculationRules = computed(() => task.value?.scope?.enabled_calculation_rules ?? []);
const unenabledRules = computed(() => task.value?.scope?.unenabled_rules ?? []);
// 未配置的检查类别按**能力类型**汇总展示：拿实例码与能力类型列表比较恒为假。
const unconfiguredNodes = computed(() => task.value?.scope?.unconfigured_node_types ?? []);

/** 本任务的证据记录与操作记录：跳转到对应页面并按任务编号预筛选。 */
function openTaskEvidence(): void {
  if (!task.value) return;
  void router.push({ name: "evidence", query: { task_no: task.value.task_no } });
}

function openTaskOperations(): void {
  if (!task.value) return;
  void router.push({ name: "system-operations", query: { keyword: task.value.task_no } });
}

onMounted(() => {
  void syncFromRoute();
});

watch(
  () => [route.params.taskNo, route.query.tab, route.query.operation],
  () => {
    void syncFromRoute();
  },
);
</script>

<template>
  <div>
    <FailureNotice :error="failure" @dismiss="failure = null" />

    <!-- 1. 返回 / 任务名称 / 总体状态；2. 场景与方案版本 -->
    <div class="app-panel">
      <div class="app-detail-head">
        <el-button text aria-label="返回任务列表" @click="router.push({ name: 'audit-tasks' })">← 返回</el-button>
        <span class="app-detail-head__name">{{ task?.task_name ?? "任务详情" }}</span>
        <StatusBadge v-if="task" :status="task.status" size="default" />
        <el-tag v-if="task?.deleted" type="danger" size="small" effect="light" disable-transitions>已删除</el-tag>
        <div class="card-actions">
          <el-button :loading="loadingTask" @click="refreshTask">刷新</el-button>
          <el-dropdown trigger="click">
            <el-button>更多</el-button>
            <template #dropdown>
              <el-dropdown-menu>
                <el-dropdown-item @click="openTaskEvidence">证据记录</el-dropdown-item>
                <el-dropdown-item @click="openTaskOperations">操作记录</el-dropdown-item>
                <el-dropdown-item @click="snapshotOpen = true">任务规则快照</el-dropdown-item>
                <el-dropdown-item v-if="task && !task.deleted" divided @click="openEdit">编辑资料</el-dropdown-item>
                <el-dropdown-item v-if="task && !task.deleted" @click="openCopy">复制任务</el-dropdown-item>

                <el-dropdown-item v-if="task && !task.deleted" divided @click="removeTask">删除任务</el-dropdown-item>
              </el-dropdown-menu>
            </template>
          </el-dropdown>
        </div>
      </div>

      <StateBlock v-if="!task && loadingTask" kind="loading" />
      <el-alert v-else-if="!task" type="warning" show-icon :closable="false" title="任务不存在或读取失败">
        <el-button size="small" @click="refreshTask">重试</el-button>
        <el-button size="small" @click="router.push({ name: 'audit-tasks' })">返回任务列表</el-button>
      </el-alert>

      <!-- 场景 / 方案 / 版本：一行业务信息，不再重复出现在描述表格里 -->
      <div v-if="task" class="task-meta">
        <span>场景：{{ task.scenario_name }}<span class="text-hint"> · 第 {{ task.version_no }} 版</span></span>
        <span>方案：{{ planText }}</span>
        <span v-if="task.created_at" class="text-hint">创建于 {{ formatDate(task.created_at) }}</span>
        <span v-if="task.remark" class="task-meta__remark">备注：{{ task.remark }}</span>
      </div>

      <!-- 3. 流程进度 -->
      <el-steps class="block-gap-sm" :active="currentStepIndex" align-center>
        <el-step
          v-for="node in task?.nodes ?? []"
          :key="node.node_code"
          :title="node.name"
          :status="stepStatus(node)"
          :description="stepDescription(node)"
        />
      </el-steps>

      <template v-if="task">
        <!-- 4. 当前待办与阻断原因；5. 唯一主操作 -->
        <div class="task-actions">
        <div class="app-toolbar">
          <div class="app-toolbar__left">
            <el-button
              type="primary"
              size="large"
              :disabled="primaryAction.disabled"
              :loading="primaryAction.loading"
              :title="primaryAction.reason ?? ''"
              @click="runPrimaryAction"
            >
              {{ primaryAction.label }}
            </el-button>
            <span v-if="primaryAction.reason && !blockers.length" class="text-danger primary-reason">{{ primaryAction.reason }}</span>
          </div>
        </div>

        <el-table v-if="blockers.length" :data="blockers" size="small" border class="block-gap-sm">
          <el-table-column label="待办与阻断" min-width="240">
            <template #default="{ row }">
              <el-tag :type="row.tone === 'error' ? 'danger' : row.tone" size="small" effect="plain">
                {{ row.tone === "error" ? "需处理" : row.tone === "warning" ? "注意" : "待办" }}
              </el-tag>
              <span class="blocker-title">{{ row.title }}</span>
            </template>
          </el-table-column>
          <el-table-column prop="description" label="说明" min-width="280" />
          <el-table-column label="操作" width="130">
            <template #default="{ row }">
              <el-button v-if="row.tab && tab !== row.tab" link type="primary" size="small" @click="setTab(row.tab)">
                {{ row.tabLabel || "前往处理" }}
              </el-button>
            </template>
          </el-table-column>
        </el-table>

        <OperationPanel
          v-if="operation"
          class="block-gap"
          :operation="operation"
          :watching="watching"
          :label="executingNode?.name ?? null"
          :poll-seconds="pollIntervalSeconds"
          :can-retry="retryableOperation"
          :percent="executionPercent"
          @requery="requery()"
          @retry="retryOperation"
        />
        </div>

        <!-- 6. 紧凑概览 -->
        <div class="task-overview">
        <h3 class="app-section-title">任务概览</h3>
        <el-table :data="[task]" size="small" border>
          <el-table-column label="材料已上传" min-width="120">
            <template #default>{{ uploadedDocumentCount }} / {{ task.documents.length }}</template>
          </el-table-column>
          <el-table-column label="必传待上传" min-width="120">
            <template #default>{{ missingRequiredMaterials.length }}</template>
          </el-table-column>
          <el-table-column label="字段已抽取" min-width="120">
            <template #default>{{ fieldExtractedCount }} / {{ task.fields.length }}</template>
          </el-table-column>
          <el-table-column label="规则不合格" min-width="120">
            <template #default>{{ ruleAbnormalCount }}</template>
          </el-table-column>
          <el-table-column label="待人工复核" min-width="120">
            <template #default>{{ task.pending_reviews.length }}</template>
          </el-table-column>
          <el-table-column label="已成立风险" min-width="120">
            <template #default>{{ risksError ? "读取失败" : risks ? risks.hit_count : riskConfigured ? "未评定" : "未配置" }}</template>
          </el-table-column>
        </el-table>
        </div>
      </template>
    </div>

    <template v-if="task">
      <!-- 7. 详细页签 -->
      <div class="app-panel">
        <el-tabs :model-value="tab" @update:model-value="setTab">
          <el-tab-pane label="材料" name="materials">
            <div v-if="materialUploadPercent !== null && uploadingDocumentNo" class="block-gap-sm">
              <el-progress :percentage="materialUploadPercent" :status="materialUploadPercent >= 100 ? 'success' : undefined" />
              <span class="text-hint">
                {{ materialUploadPercent >= 100 ? "文件已发送，正在等待服务端处理并保存。" : "正在上传材料文件…" }}
              </span>
            </div>

            <div v-if="task.documents.length" class="material-table-scroll block-gap-sm">
              <table class="material-table">
                <thead>
                  <tr><th scope="col">材料</th><th scope="col">当前文件</th><th scope="col">处理状态</th><th scope="col">上传类型</th><th scope="col">操作</th></tr>
                </thead>
                <tbody>
                  <TaskMaterialRow
                    v-for="document in task.documents"
                    :key="document.document_no"
                    :document="document"
                    :busy="uploadingDocumentNo === document.document_no"
                    :disabled="pending && uploadingDocumentNo !== document.document_no"
                    :needs-attention="currentNodeCode === 'MATERIAL_CHECK'"
                    :replacement-impact="
                      replacementImpact && replacementImpact.materialName === document.material_name ? replacementImpact : null
                    "
                    @pick="(file, materialType) => uploadMaterial(file, document, materialType)"
                    @download="downloadDocument"
                    @preview="previewDocument"
                    @remove="removeDocument"
                  />
                </tbody>
              </table>
            </div>
            <p v-else class="text-muted">该场景没有定义材料位。</p>
          </el-tab-pane>

          <el-tab-pane label="字段结果" name="fields">
            <p class="text-muted">
              每个字段分列显示原文内容与标准化结果；标准化结果不代表审核通过。纠正字段事实会使依赖它的规则与报告失效。
            </p>
            <AuditFieldResults
              :fields="task.fields"
              :extraction-finished="extractionFinished"
              :correction-enabled="task.status !== 'COMPLETED' && task.status !== 'FAILED'"
              @open-evidence="openEvidence"
              @correct="openCorrection"
            />
          </el-tab-pane>

          <el-tab-pane label="规则结果" name="rules">
            <AuditRuleResults
              :rules="task.rules"
              :unconfigured-nodes="task.unconfigured_check_node_types ?? []"
              :task-no="task.task_no"
              :node-type-options="taskWorkspaceNodeTypes"
            />
          </el-tab-pane>

          <el-tab-pane name="reviews">
            <template #label>人工复核（{{ task.pending_reviews.length }}）</template>
            <template v-if="task.pending_reviews.length">
              <p class="text-muted">
                系统无法自动判定的项在这里逐条给出业务结论；先提交字段事实，重算后再裁决规则。
                页面只提交<strong>本批</strong>（{{ task.pending_reviews.length }} 项）<template v-if="queuedReviewCount">
                  ，另有 {{ queuedReviewCount }} 项在本批之后处理</template>；提交后由服务端恢复流程。
              </p>
              <el-alert
                v-if="reviewBatchStale"
                type="warning"
                show-icon
                :closable="false"
                class="block-gap-sm"
                title="待复核集合已变化"
                description="本批哈希与进入等待时不一致，请先「刷新」重新读取当前批次再提交；直接提交会被服务端以 409 拒绝。"
              />
              <div class="review-layout block-gap">
                <div class="review-list">
                  <div
                    v-for="review in task.pending_reviews"
                    :key="review.review_no"
                    class="review-list__item"
                    :class="{ 'is-active': selectedReviewNo === review.review_no }"
                    @click="selectedReviewNo = review.review_no"
                  >
                    <strong>{{ review.material_name || "未关联材料" }}</strong>
                    <small>{{ review.field_name || review.rule_name || "人工复核" }}</small>
                  </div>
                </div>
                <div class="review-detail">
                  <PendingReviewCard
                    v-if="activeReview"
                    :review="activeReview"
                    :draft="activeReviewDraft"
                    :disabled="task.status !== 'WAITING_REVIEW' || pending || watching"
                    @update:draft="updateActiveReviewDraft"
                    @open-evidence="openActiveReviewEvidence"
                  />
                  <el-empty v-else description="请选择左侧的待复核项" />
                </div>
              </div>
              <p v-if="formMessage" class="text-danger block-gap">{{ formMessage }}</p>
              <div class="sticky-actions">
                <div class="sticky-actions__bar">
                  <div>
                    <div :class="reviewSubmitDisabledReason ? 'text-danger' : 'text-muted'">
                      {{ reviewSubmitDisabledReason ?? "全部复核项已填写完整，可以提交。" }}
                    </div>
                    <div class="text-hint">提交后会按有效结论继续后续节点；提交本身是一次后台操作，页面会查询它的真实状态。</div>
                  </div>
                  <div class="card-actions">
                    <span
                      class="text-hint"
                      :class="{ 'text-danger': draftSaveState === 'error' }"
                      role="status"
                    >
                      {{ draftSaveState === "saving" ? "正在保存草稿…" : draftSaveState === "saved" ? "草稿已保存（按待办版本暂存，刷新不丢失）" : draftSaveMessage ?? "" }}
                    </span>
                    <el-button :loading="draftSaveState === 'saving'" @click="saveDraftsNow">保存草稿</el-button>
                    <el-button
                      type="primary"
                      :disabled="pending || watching || Boolean(reviewSubmitDisabledReason)"
                      :title="reviewSubmitDisabledReason ?? ''"
                      @click="submitReviews"
                    >
                      提交复核结论
                    </el-button>
                  </div>
                </div>
              </div>
            </template>
            <el-alert v-else type="info" show-icon :closable="false" title="当前没有待复核项">
              <template v-if="canEnterManualReview">
                任务停在人工复核节点但还没有待办；执行「进入人工复核」后，由服务端确认是否跳过（待办为空时会直接进入下一节点）。
              </template>
              <template v-else-if="atWaitingReview">
                任务停在人工复核等待点，但服务端没有返回待办；请用「刷新」确认服务端是否已生成待办，页面不会自行跳过。
              </template>
              <template v-else>当前节点不是人工复核节点，也没有待复核项。</template>
            </el-alert>

            <h4 class="app-section-title block-gap">复核记录</h4>
            <el-alert v-if="reviewRecordsError" type="error" show-icon :closable="false" :title="`复核记录读取失败：${reviewRecordsError}`">
              <el-button size="small" @click="retryLoadReviewRecords()">重试</el-button>
            </el-alert>
            <el-table
              v-else
              :data="reviewRecords"
              border
              stripe
              size="small"
              v-loading="reviewRecordsLoading"
              empty-text="暂无复核记录"
            >
              <el-table-column prop="object_name" label="复核对象" min-width="180" show-overflow-tooltip />
              <el-table-column label="类型" width="120">
                <template #default="{ row }">{{ reviewObjectKindLabel(row.object_kind) }}</template>
              </el-table-column>
              <el-table-column label="系统原判断" min-width="220" show-overflow-tooltip>
                <template #default="{ row }">
                  {{ statusLabel(row.system_judgment.status) }}
                  <span v-if="row.system_judgment.message">· {{ row.system_judgment.message }}</span>
                </template>
              </el-table-column>
              <el-table-column label="人工结论" width="140">
                <template #default="{ row }">{{ reviewDecisionLabel(row.decision) }}</template>
              </el-table-column>
              <el-table-column label="依据类型" width="130">
                <template #default="{ row }">{{ row.basis_type ? basisTypeLabel(row.basis_type) : "—" }}</template>
              </el-table-column>
              <el-table-column label="状态" width="110">
                <template #default="{ row }"><StatusBadge :status="row.status" /></template>
              </el-table-column>
              <el-table-column label="更新时间" width="170">
                <template #default="{ row }">{{ String(row.updated_at).replace("T", " ").slice(0, 19) }}</template>
              </el-table-column>
            </el-table>
          </el-tab-pane>

          <el-tab-pane label="风险" name="risks">
            <AuditRiskResults
              :risks="risks"
              :risk-node-executed="riskNodeExecuted"
              :risk-configured="riskConfigured"
              :loading="risksLoading"
              :error="risksError"
            />
          </el-tab-pane>

          <el-tab-pane label="报告" name="report">
            <AuditReportPanel
              :report="report"
              :download-url="reportDownloadUrl"
              :running="watching && nodeTypeOf(executingNode?.code) === 'REPORT_OUTPUT'"
              :loading="reportLoading"
              :error="reportError"
            />
          </el-tab-pane>

          <el-tab-pane label="取证检索" name="retrieval">
            <el-alert
              type="info"
              show-icon
              :closable="false"
              class="block-gap-sm"
              title="本页只用于核对印章主体"
            >
              <div>
                以印章识别产物中的主体名称为检索对象，按当前任务、材料范围、当前上传版本与当前产物限定范围；
                相似度只用于候选排序。
              </div>
              <div>没有候选不等于合同不存在该主体；结论由工作人员选择一致、不一致或证据不足后保存。</div>
            </el-alert>

            <el-alert
              v-if="evidenceIndexError"
              type="error"
              show-icon
              :closable="false"
              :title="`索引状态读取失败：${evidenceIndexError}`"
            >
              <el-button size="small" @click="refreshTask()">重试</el-button>
            </el-alert>

            <el-descriptions v-else :column="2" size="small" border class="block-gap-sm" v-loading="evidenceIndexLoading">
              <el-descriptions-item label="索引状态">
                <StatusBadge
                  :status="evidenceIndex?.indexed ? 'READY' : 'UNCONFIGURED'"
                  :label="evidenceIndex?.indexed ? '已建立索引' : '尚未建立索引'"
                  :tone="evidenceIndex?.indexed ? 'success' : 'warning'"
                />
              </el-descriptions-item>
              <el-descriptions-item label="嵌入模型">{{ evidenceIndex?.model_id || "未配置" }}</el-descriptions-item>
              <el-descriptions-item label="模型修订">{{ evidenceIndex?.model_revision || "未记录" }}</el-descriptions-item>
              <el-descriptions-item label="分块版本">{{ evidenceIndex?.chunk_version || "未记录" }}</el-descriptions-item>
            </el-descriptions>

            <el-table
              v-if="evidenceIndex?.items?.length"
              :data="evidenceIndex.items"
              border
              size="small"
              class="block-gap-sm"
              empty-text="当前任务还没有任何索引记录"
            >
              <el-table-column label="材料" min-width="140">
                <template #default="{ row }">{{ row.material_name || "—" }}</template>
              </el-table-column>
              <el-table-column label="产物" width="130" prop="artifact_type" />
              <el-table-column label="索引状态" width="200">
                <template #default="{ row }">
                  <StatusBadge
                    :status="row.status"
                    :label="evidenceIndexStatusLabel(row.status)"
                    :tone="evidenceIndexStatusTone(row.status)"
                  />
                  <div v-if="!row.current_for_artifact" class="text-muted">
                    不适用于当前产物内容（需重新建立索引）
                  </div>
                  <div v-if="row.failure_message" class="text-muted">{{ row.failure_message }}</div>
                </template>
              </el-table-column>
              <el-table-column label="主体条数" width="100" prop="chunk_count" />
              <el-table-column label="建立时间" width="170">
                <template #default="{ row }">{{ row.built_at ? formatDate(row.built_at) : "—" }}</template>
              </el-table-column>
            </el-table>

            <el-form label-position="top" class="block-gap">
              <el-form-item label="检索范围">
                <el-select v-model="retrievalMaterialKey" clearable placeholder="默认检索全部已上传材料" style="max-width: 320px">
                  <el-option
                    v-for="item in task?.documents || []"
                    :key="item.document_no"
                    :label="item.material_name"
                    :value="item.material_key || ''"
                    :disabled="!item.material_key || !item.filename"
                  />
                </el-select>
                <div class="text-hint">只能选择本任务已上传的材料；未上传的材料不在检索范围内。</div>
              </el-form-item>
              <el-form-item label="待核对主体">
                <el-input
                  v-model="retrievalQuery"
                  maxlength="200"
                  show-word-limit
                  placeholder="例如：主合同甲方名称（合同主体全称）"
                />
                <div class="text-hint">只检索印章识别出的主体名称；合同正文本轮不参与检索。</div>
              </el-form-item>
              <el-form-item>
                <el-button type="primary" :loading="retrievalLoading" @click="runRetrieval(uploadableMaterialKeys)">检索印章主体</el-button>
              </el-form-item>
            </el-form>

            <RetrievalEvidencePanel
              v-if="retrievalResult || retrievalError"
              :retrieval-no="retrievalResult?.retrieval_no || ''"
              :status="retrievalResult?.status || ''"
              :message="retrievalResult?.message || ''"
              :candidates="retrievalResult?.candidates || []"
              :uncovered-materials="retrievalResult?.uncovered_materials || []"
              :decision="retrievalDecision?.decision ?? null"
              :decision-note="retrievalDecision?.note ?? null"
              :decision-selected="decidedCandidate"
              :decision-input-revision="retrievalDecision?.decision_input_revision ?? null"
              :current-input-revision="task?.input_revision ?? null"
              :error-message="retrievalError"
              :loading="retrievalLoading"
              :submitting="retrievalDecisionPending"
              @submit="onSubmitRetrievalDecision"
            />
            <StateBlock
              v-else
              kind="empty"
              title="还没有发起检索"
              description="填写待核对主体后点击「检索印章主体」，系统会在当前任务已上传材料的印章识别结果中查找候选主体。"
            />
          </el-tab-pane>

          <el-tab-pane label="执行记录" name="executions">
            <el-alert v-if="executionsError" type="error" show-icon :closable="false" :title="`执行记录读取失败：${executionsError}`">
              <el-button size="small" @click="retryLoadExecutions()">重试</el-button>
            </el-alert>
            <el-table
              v-else
              :data="executions"
              border
              stripe
              size="small"
              v-loading="executionsLoading"
              empty-text="暂无执行记录"
            >
              <el-table-column label="执行节点" width="140">
                <template #default="{ row }">{{ row.node_name }}</template>
              </el-table-column>
              <el-table-column label="执行状态" width="120">
                <template #default="{ row }">
                  <StatusBadge :status="row.status" :tone="statusTone(row.status)" />
                </template>
              </el-table-column>
              <el-table-column label="开始时间" width="170">
                <template #default="{ row }">{{ row.started_at ? String(row.started_at).replace("T", " ").slice(0, 19) : "—" }}</template>
              </el-table-column>
              <el-table-column label="结束时间" width="170">
                <template #default="{ row }">{{ row.finished_at ? String(row.finished_at).replace("T", " ").slice(0, 19) : "—" }}</template>
              </el-table-column>
              <el-table-column label="耗时" width="100">
                <template #default="{ row }">{{ row.duration_ms === null ? "—" : `${(row.duration_ms / 1000).toFixed(1)} 秒` }}</template>
              </el-table-column>
              <el-table-column prop="failure_message" label="失败原因" min-width="200" show-overflow-tooltip>
                <template #default="{ row }">{{ row.failure_message || "—" }}</template>
              </el-table-column>
              <el-table-column label="操作" width="90" fixed="right">
                <template #default="{ row }">
                  <el-button link type="primary" size="small" @click="openExecutionDetail(row)">详情</el-button>
                </template>
              </el-table-column>
            </el-table>
          </el-tab-pane>
        </el-tabs>
      </div>
    </template>

    <EvidenceDrawer
      :open="Boolean(evidenceField)"
      :material-name="evidenceField?.material_name ?? ''"
      :field-name="evidenceField?.name ?? ''"
      :evidence="evidence"
      :loading="evidenceLoading"
      :error-message="evidenceError"
      @close="closeEvidence"
    />

    <FieldCorrectionDialog
      :open="Boolean(correctionField)"
      :field="correctionField"
      :result="correctionResult"
      :pending="correctionPending"
      @close="closeCorrection"
      @submit="submitCorrection"
    />

    <el-drawer v-model="executionDetailVisible" title="执行详情" size="560px">
      <el-descriptions v-if="executionDetail" :column="1" border size="small">
        <el-descriptions-item label="执行节点">{{ executionDetail.node_name }}（第 {{ executionDetail.sequence_no }} 次执行）</el-descriptions-item>
        <el-descriptions-item label="执行状态"><StatusBadge :status="executionDetail.status" /></el-descriptions-item>
        <el-descriptions-item label="开始时间">{{ String(executionDetail.started_at ?? "—").replace("T", " ").slice(0, 19) }}</el-descriptions-item>
        <el-descriptions-item label="结束时间">{{ String(executionDetail.finished_at ?? "—").replace("T", " ").slice(0, 19) }}</el-descriptions-item>
        <el-descriptions-item label="任务输入修订">{{ executionDetail.input_revision ?? "—" }}</el-descriptions-item>
        <el-descriptions-item label="关联操作编号">{{ executionDetail.operation_no ?? "—" }}</el-descriptions-item>
        <el-descriptions-item label="失败码">{{ executionDetail.failure_code ?? "—" }}</el-descriptions-item>
        <el-descriptions-item label="失败原因">{{ executionDetail.failure_message ?? "—" }}</el-descriptions-item>
        <el-descriptions-item label="结果摘要">
          <el-collapse v-if="executionDetail.result_summary">
            <el-collapse-item title="查看本次执行结果的技术明细" name="execution-summary">
              <pre class="execution-tech">{{ JSON.stringify(executionDetail.result_summary, null, 2) }}</pre>
            </el-collapse-item>
          </el-collapse>
          <span v-else>—</span>
        </el-descriptions-item>
      </el-descriptions>
      <el-alert class="block-gap-sm" type="info" show-icon :closable="false" title="重试说明">
        节点重试只在任务处于「待执行且停在当前节点、无活跃操作」时由服务端允许；如当前不满足，请回到任务按真实状态处理。
      </el-alert>
    </el-drawer>

    <DrawerPanel
      :open="snapshotOpen"
      title="任务规则快照"
      description="范围在创建任务时冻结；规则名称与自动纳入原因取自任务绑定的场景版本，不读取场景最新版本。"
      size="46%"
      @close="snapshotOpen = false"
    >
      <el-alert
        v-if="unconfiguredNodes.length"
        class="block-gap-sm"
        type="warning"
        show-icon
        :closable="false"
        :title="`本模板未配置的校验节点：${unconfiguredNodes.map((code) => nodeLabel(code)).join('、')}`"
      >
        执行时会记为跳过并说明原因，不代表该检查通过。
      </el-alert>

      <el-alert
        class="block-gap-sm"
        type="info"
        show-icon
        :closable="false"
        :title="`本次任务启用 ${enabledRules.length + enabledCalculationRules.length} 条规则`"
      >
        执行范围来自创建任务时冻结的方案配置；未启用的检查规则不参与本次审核。
      </el-alert>

      <h4 class="app-section-title">已启用检查规则（{{ enabledRules.length }} 条）</h4>
      <el-table :data="enabledRules" size="small" border empty-text="没有启用规则">
        <el-table-column prop="name" label="规则名称" min-width="200" show-overflow-tooltip />
        <el-table-column label="规则键" width="160" show-overflow-tooltip>
          <template #default="{ row }"><span class="mono">{{ row.rule_key }}</span></template>
        </el-table-column>
      </el-table>

      <h4 class="app-section-title">已启用计算规则（{{ enabledCalculationRules.length }} 条）</h4>
      <el-table :data="enabledCalculationRules" size="small" border empty-text="没有启用计算规则">
        <el-table-column prop="name" label="规则名称" min-width="200" show-overflow-tooltip />
        <el-table-column label="规则编号" width="160" show-overflow-tooltip>
          <template #default="{ row }"><span class="mono">{{ row.rule_key }}</span></template>
        </el-table-column>
      </el-table>

      <h4 class="app-section-title">未启用检查规则（{{ unenabledRules.length }} 条）</h4>
      <el-table :data="unenabledRules" size="small" border empty-text="没有未启用的规则">
        <el-table-column prop="name" label="规则名称" min-width="200" show-overflow-tooltip />
        <el-table-column label="规则键" width="160" show-overflow-tooltip>
          <template #default="{ row }"><span class="mono">{{ row.rule_key }}</span></template>
        </el-table-column>
      </el-table>
      <p class="text-hint">这些规则保留在任务冻结的方案配置中，本任务不执行。</p>

      <h4 class="app-section-title">任务技术信息</h4>
      <el-descriptions :column="1" size="small" border>
        <el-descriptions-item label="任务编号"><span class="mono">{{ task?.task_no }}</span></el-descriptions-item>
        <el-descriptions-item label="任务输入修订"><span class="mono">{{ task?.input_revision }}</span></el-descriptions-item>
        <el-descriptions-item label="场景版本">{{ task?.scenario_name }} · 第 {{ task?.version_no }} 版</el-descriptions-item>
        <el-descriptions-item label="方案">{{ planText }}</el-descriptions-item>
        <el-descriptions-item label="更新时间">{{ task?.updated_at ? formatDate(task.updated_at) : "—" }}</el-descriptions-item>
      </el-descriptions>
    </DrawerPanel>

    <el-dialog v-model="editVisible" title="编辑任务资料" width="600px" :close-on-click-modal="false">
      <el-form label-width="100px">
        <el-form-item label="任务名称" required>
          <el-input v-model="editForm.task_name" maxlength="255" />
        </el-form-item>
        <el-form-item label="备注">
          <el-input v-model="editForm.remark" type="textarea" :rows="3" maxlength="4000" />
        </el-form-item>
      </el-form>
      <p class="text-hint">冻结的创建时范围不会随重命名或备注修改变化；已完成任务需要更换输入时，请按方案当前范围重新创建任务。</p>
      <template #footer>
        <el-button @click="editVisible = false">取消</el-button>
        <el-button type="primary" :loading="editSaving" @click="submitEdit">保存</el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="copyVisible" title="复制为新任务" width="600px" :close-on-click-modal="false">
      <el-form label-width="100px">
        <el-form-item label="新任务名称" required>
          <el-input v-model="copyName" maxlength="255" />
        </el-form-item>
      </el-form>
      <p class="text-hint">按所属方案的**当前范围**重新创建一个新任务；材料与结果重新开始，原任务报告保留。</p>
      <template #footer>
        <el-button @click="copyVisible = false">取消</el-button>
        <el-button type="primary" :loading="copySaving" @click="submitCopy">复制</el-button>
      </template>
    </el-dialog>


  </div>
</template>

<style scoped>
.task-meta {
  display: flex;
  flex-wrap: wrap;
  gap: 6px 20px;
  margin-top: 8px;
  color: var(--el-text-color-regular);
  font-size: 13px;
}

.task-meta__remark {
  overflow-wrap: anywhere;
}

.primary-reason {
  line-height: 1.6;
}

.task-actions,
.task-overview {
  margin-top: 16px;
}

.blocker-title {
  margin-left: 8px;
}

.material-table-scroll {
  width: 100%;
  overflow-x: auto;
}

.material-table {
  width: 100%;
  min-width: 940px;
  border-collapse: collapse;
  text-align: left;
  font-size: 13px;
}

.material-table th,
.material-table :deep(td) {
  padding: 10px 12px;
  border-bottom: 1px solid var(--el-border-color-lighter);
  vertical-align: middle;
}

.material-table th {
  color: var(--el-text-color-secondary);
  font-weight: 600;
  background: var(--el-fill-color-light);
}

.material-table th:nth-child(1) { width: 16%; }
.material-table th:nth-child(2) { width: 20%; }
.material-table th:nth-child(3) { width: 19%; }
.material-table th:nth-child(4) { width: 21%; }
.material-table th:nth-child(5) { width: 24%; }

.execution-tech {
  max-height: 360px;
  margin: 0;
  overflow: auto;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}
</style>
