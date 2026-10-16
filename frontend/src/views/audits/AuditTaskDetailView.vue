<script setup lang="ts">
import { computed, onMounted, proxyRefs, ref, shallowRef, watch } from "vue";
import { useRoute, useRouter } from "vue-router";
import { ElMessage, ElMessageBox } from "element-plus";

import type {
  AuditDocument,
  AuditField,
  ExecuteNodeResponse,
  ExecutionRow,
  FieldCorrectionRequest,
  FieldCorrectionResponse,
  Report,
  RiskList,
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
  deleteAuditTask,
  executeAuditNode,
  getAuditTask,
  getTaskReport,
  getTaskRisks,
  listTaskExecutions,
  submitFieldCorrections,
  type AuditTaskView,
} from "@/api/audits";

import { getExecution } from "@/api/reviews";
import MaterialProcessingDialog from "@/components/audit/MaterialProcessingDialog.vue";
import AuditFieldResults from "@/components/audit/AuditFieldResults.vue";
import AuditReportPanel from "@/components/audit/AuditReportPanel.vue";
import AuditRiskResults from "@/components/audit/AuditRiskResults.vue";
import AuditRuleResults from "@/components/audit/AuditRuleResults.vue";
import FieldCorrectionDialog from "@/components/audit/FieldCorrectionDialog.vue";
import TaskMaterialRow from "@/components/audit/TaskMaterialRow.vue";
import EvidenceDrawer from "@/components/EvidenceDrawer.vue";
import BackButton from "@/components/BackButton.vue";
import FailureNotice from "@/components/FailureNotice.vue";
import OperationPanel from "@/components/OperationPanel.vue";
import StateBlock from "@/components/StateBlock.vue";
import StatusBadge from "@/components/StatusBadge.vue";
import { useEvidenceDrawer } from "@/composables/useEvidenceDrawer";
import { useMaterialProcessing } from "@/composables/useMaterialProcessing";
import { useMaterialActions } from "@/composables/useMaterialActions";
import { useMaterialTypeCatalog } from "@/composables/useMaterialTypeCatalog";
import { useOperationTracker } from "@/composables/useOperationTracker";
import { useRequestRunner } from "@/composables/useRequestRunner";
import { isDetailTab, type DetailTab } from "@/constants/auditWorkspaces";
import { COMPLETED_RESULT_STATUSES } from "@/constants/auditStatus";
import {
  formatDate,
  nodeLabel,
  statusTone,
} from "@/constants/labels";

/**
 * 审核任务详情。
 *
 * 页头保留任务名称与必要操作；横向摘要突出结果，完整内容由页签承载。
 *
 * 执行与报告都由服务端真实状态驱动；响应丢失先查询原命令，不重复提交。
 */

const route = useRoute();
const router = useRouter();
const { operation, watching, track, requery, reset: resetTracking } = useOperationTracker();
const { pending, failure, formMessage, run, setFailure } = useRequestRunner();

const taskNo = computed(() => String(route.params.taskNo ?? ""));

// 任务详情整体替换（applyTask），用 shallowRef 避免对含递归 JsonValue 的类型做深解包（TS2589）。
const task = shallowRef<AuditTaskView | null>(null);
const materialTypeCatalog = useMaterialTypeCatalog();
/* 材料位操作与原文证据抽屉：各自独立的状态与操作流程，见对应 composable。 */
const {
  busyDocumentNo: uploadingDocumentNo,
  uploadPercent: materialUploadPercent,
  replacementImpact,
  upload: uploadMaterial,
  remove: removeDocument,
  reset: resetMaterialActions,
} = useMaterialActions({ taskNo, run, refresh: refreshTask, onOperationAccepted: trackOperation });
const {
  field: evidenceField,
  evidence,
  error: evidenceError,
  loading: evidenceLoading,
  openForField: openEvidence,
  close: closeEvidence,
} = useEvidenceDrawer({ taskNo, run });

const risks = ref<RiskList | null>(null);
const risksError = ref<string | null>(null);
const risksLoading = ref(false);

const report = ref<Report | null>(null);
const reportError = ref<string | null>(null);
const reportLoading = ref(false);
const lastExecution = ref<ExecuteNodeResponse | null>(null);
const executingNode = ref<{ code: string; name: string } | null>(null);
const tab = ref<DetailTab>("materials");
/** 规则结果页的校验阶段筛选；由工作区入口设置，不由当前节点隐式改写。 */
const loadingTask = ref(false);
const correctionField = ref<AuditField | null>(null);
const correctionResult = ref<FieldCorrectionResponse | null>(null);
const correctionPending = ref(false);
const lastOperationType = ref<string | null>(null);
let executionAttemptKey: string | null = null;

/** 页面状态代次：切换任务时递增，过期响应不得覆盖当前页面。 */
let stateGeneration = 0;
const activeTaskNo = ref<string | null>(null);

function isStale(token: number, currentTaskNo: string): boolean {
  return token !== stateGeneration || activeTaskNo.value !== currentTaskNo;
}

const materialProcessing = useMaterialProcessing({
  task,
  watching,
  pending,
  generation: () => stateGeneration,
  isStale,
  refresh: refreshTask,
  onOperationAccepted: trackOperation,
});
const processingState = proxyRefs(materialProcessing);
const {
  processingDialogVisible,
  processingLoading,
  loadProcessingStatuses,
  openProcessingDialog,
  reset: resetMaterialProcessing,
} = materialProcessing;

/* 执行记录（页签） */
const executions = ref<ExecutionRow[]>([]);
const executionsError = ref<string | null>(null);
const executionsLoading = ref(false);
const executionDetail = ref<Awaited<ReturnType<typeof getExecution>> | null>(null);
const executionDetailVisible = ref(false);

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
function retryLoadExecutions(): void {
  void loadExecutions(stateGeneration);
}

/* 节点流程 */
const currentNodeCode = computed(() => task.value?.current_node.node_code ?? null);
const currentNodeName = computed(() => task.value?.current_node.name || nodeLabel(currentNodeCode.value));

const uploadedDocumentCount = computed(() => task.value?.documents.filter((document) => document.filename).length ?? 0);
const missingRequiredMaterials = computed(
  () => task.value?.documents.filter((document) => document.required && !document.filename) ?? [],
);
const extractionFinished = computed(
  () =>
    task.value?.nodes.some(
      (node) => node.node_type === "RULE_EXECUTION" && node.status === "SUCCEEDED",
    ) ?? false,
);
const riskNodeExecuted = computed(() => risks.value?.risk_executed ?? false);
const riskConfigured = computed(
  () => risks.value?.risk_configured ?? false,
);
const atWaitingReview = computed(() => task.value?.status === "WAITING_REVIEW");

const prerequisites = computed(() => {
  if (!task.value) {
    return [] as Array<{ label: string; satisfied: boolean }>;
  }
  const items: Array<{ label: string; satisfied: boolean }> = [
    { label: "任务不在后台执行中", satisfied: task.value.status !== "RUNNING" && !watching.value },
    { label: "没有仍在执行的后台操作", satisfied: !isOperationActive(task.value.latest_operation?.status) },
    {
      label: task.value.status === "COMPLETED" ? "任务执行已结束" : "任务已失败",
      satisfied: task.value.status !== "COMPLETED" && task.value.status !== "FAILED",
    },
  ];
  return items;
});

const unsatisfiedPrerequisites = computed(() => prerequisites.value.filter((item) => !item.satisfied));
const canExecuteNode = computed(
  () => !pending.value && !watching.value && unsatisfiedPrerequisites.value.length === 0 && task.value?.status !== "WAITING_REVIEW",
);

const executeDisabledReason = computed(() => {
  if (!task.value) return null;
  if (atWaitingReview.value) return "该任务已停在原人工复核等待点；人工审核暂不启用，请复制为新任务重新执行。";
  if (task.value.status === "COMPLETED") return "任务执行已结束，具体结论见规则结果。";
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
 *   执行中 → 查看进度；已完成 → 查看结果；
 *   失败 → 查看原因；阻断 → 重新执行当前节点；可执行 → 执行当前节点。
 * 任务未到可执行状态时不显示一个点不动的「执行当前节点」。
 */
const primaryAction = computed<PrimaryAction>(() => {
  if (!task.value) {
    return { label: "刷新任务", run: () => void refreshTask(), disabled: false, reason: null, loading: loadingTask.value };
  }
  if (atWaitingReview.value) {
    return { label: "查看执行记录", tab: "executions", disabled: false, reason: null, loading: false };
  }
  if (watching.value || task.value.status === "RUNNING") {
    return { label: "查看执行进度", tab: "executions", disabled: false, reason: null, loading: false };
  }
  if (task.value.status === "COMPLETED") {
    return { label: "查看审核结果", tab: "rules", disabled: false, reason: null, loading: false };
  }
  if (task.value.status === "FAILED") {
    return {
      label: "查看失败原因",
      tab: "executions",
      disabled: false,
      reason: null,
      loading: false,
    };
  }
  if (task.value.status === "BLOCKED") {
    return {
      label: "重新执行审核",
      run: () => void executeCurrentNode(),
      disabled: !canExecuteNode.value,
      reason: executeDisabledReason.value,
      loading: watching.value || pending.value,
    };
  }
  return {
    label: "执行生效规则",
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
 * 历史等待状态、读取失败。没有阻断时不显示这一节，不用「一切正常」占位。
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
  if (unsatisfiedPrerequisites.value.length
    && !watching.value && !isOperationActive(task.value.latest_operation?.status)
    && task.value.status !== "COMPLETED" && task.value.status !== "RUNNING") {
    items.push({
      tone: "warning",
      title: "暂时无法开始审核",
      description: unsatisfiedPrerequisites.value.map((item) => item.label).join("；"),
    });
  }
  if (atWaitingReview.value) {
    items.push({
      tone: "warning",
      title: "该任务已停在原人工复核等待点",
      description: "人工审核暂不启用，已有待办和结论保留；请复制为新任务重新执行。",
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
const unresolvedRuleCount = computed(
  () => task.value?.rules.filter((rule) => !COMPLETED_RESULT_STATUSES.has(rule.status)).length ?? 0,
);
const reportDownloadUrl = computed(() => {
  if (!task.value || !report.value?.content || !report.value.report_generated || !report.value.current) return null;
  return apiFileUrl(`/audit-tasks/${task.value.task_no}/reports/download?report_no=${encodeURIComponent(report.value.report_no ?? '')}`);
});

const retryableOperation = computed(() => {
  if (!task.value || task.value.status === "FAILED") return false;
  if (operation.value?.status !== "FAILED" && operation.value?.status !== "INTERRUPTED") return false;
  return lastOperationType.value === "NODE_EXECUTE" && canExecuteNode.value;
});

function applyTask(auditTask: AuditTaskView): void {
  task.value = auditTask;
}

/**
 * 切换任务时的完整清理。
 *
 * 旧任务的风险、报告、执行记录与所有抽屉状态都必须清掉：
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
  /* 独立业务区块各自重置：证据抽屉与材料操作。 */
  closeEvidence();
  resetMaterialActions();
  correctionField.value = null;
  correctionResult.value = null;
  correctionPending.value = false;
  lastOperationType.value = null;
  executionAttemptKey = null;
  executions.value = [];
  executionsError.value = null;
  executionsLoading.value = false;
  executionDetail.value = null;
  executionDetailVisible.value = false;
  /* 抽屉与弹窗 */
  resetMaterialProcessing();
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

async function loadReport(currentTaskNo: string, token: number, reportNo?: string): Promise<void> {
  reportLoading.value = true;
  reportError.value = null;
  try {
    const value = await getTaskReport(currentTaskNo, reportNo);
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
    loadExecutions(token),
  ]);
}

async function loadTask(currentTaskNo: string): Promise<void> {
  loadingTask.value = true;
  try {
    await run(async () => {
      await fetchTaskState(currentTaskNo);
      if (processingDialogVisible.value) await loadProcessingStatuses();
    });
  } finally {
    loadingTask.value = false;
  }
}

async function refreshTask(): Promise<void> {
  if (materialTypeCatalog.state.error) void materialTypeCatalog.reload();
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
    if (isRecoveredCheckpointFailure(current)) {
      await dropOperationQuery();
      return;
    }
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

function isRecoveredCheckpointFailure(value: OperationView): boolean {
  return task.value?.status === "READY"
    && value.operation_type === "NODE_EXECUTE"
    && value.failure_code === "AUDIT_CHECKPOINT_MISSING";
}

function resumeLatestOperation(token = stateGeneration): void {
  const latest = task.value?.latest_operation;
  if (latest && isRecoveredCheckpointFailure(latest)) return;
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
 *   URL 有合法 tab → 使用 URL 的 tab；
 *   未指定合法 tab → 执行结束的任务打开规则结果，其余打开材料。
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
    if (!routeTab && task.value.status === "COMPLETED") tab.value = "rules";
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

/* 任务删除是唯一保留的任务级管理操作。 */
async function removeTask(): Promise<void> {
  if (!task.value) return;
  try {
    await ElMessageBox.confirm(
      `确认删除任务「${task.value.task_name}」？删除后，该任务的材料文件和审核结果将一并永久删除，无法恢复。`,
      "删除审核任务",
      { type: "warning", confirmButtonText: "删除", cancelButtonText: "取消" },
    );
  } catch {
    return;
  }
  try {
    await deleteAuditTask(task.value.task_no);
    ElMessage.success("任务已永久删除");
    await router.push({ name: "audit-tasks" });
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : "删除失败");
  }
}

const pollIntervalSeconds = OPERATION_POLL_INTERVAL_MS / 1000;

watch(operation, () => {
  if (processingDialogVisible.value && !processingLoading.value) void loadProcessingStatuses();
});

onMounted(() => {
  void syncFromRoute();
  void materialTypeCatalog.reload();
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

    <div class="app-panel">
      <div class="app-detail-head">
        <BackButton label="返回任务列表" @click="router.push({ name: 'audit-tasks' })" />
        <span class="app-detail-head__name">{{ task?.task_name ?? "任务详情" }}</span>
        <StatusBadge v-if="task" :status="task.status" size="default"
          :label="task.status === 'COMPLETED' ? (unresolvedRuleCount ? '执行结束 · 有未解决项' : '执行已结束') : null"
          :tone="task.status === 'COMPLETED' && unresolvedRuleCount ? 'warning' : null" />
        <div class="card-actions task-head-actions">
          <el-button v-if="task && (primaryAction.run || primaryAction.tab !== tab)"
            type="primary" :disabled="primaryAction.disabled" :loading="primaryAction.loading"
            :title="primaryAction.reason ?? ''" @click="runPrimaryAction">{{ primaryAction.label }}</el-button>
          <el-button :loading="loadingTask" @click="refreshTask">刷新</el-button>
          <el-dropdown v-if="task" trigger="click">
            <el-button>更多</el-button>
            <template #dropdown>
              <el-dropdown-menu><el-dropdown-item @click="removeTask">删除任务</el-dropdown-item></el-dropdown-menu>
            </template>
          </el-dropdown>
        </div>
      </div>

      <StateBlock v-if="!task && loadingTask" kind="loading" />
      <el-alert v-else-if="!task" type="warning" show-icon :closable="false" title="任务不存在或读取失败">
        <el-button size="small" @click="refreshTask">重试</el-button>
      </el-alert>

      <template v-if="task">
        <div class="task-meta">
          <span>方案：{{ task.plan?.plan_name ?? "未关联方案" }}</span>
          <el-popover trigger="click" placement="bottom-start" :width="360">
            <template #reference><el-button link type="primary">任务信息</el-button></template>
            <el-descriptions :column="1" size="small">
              <el-descriptions-item label="场景">{{ task.scenario_name }} · 第 {{ task.version_no }} 版</el-descriptions-item>
              <el-descriptions-item v-if="task.plan" label="方案来源">冻结场景第 {{ task.plan.scenario_version_no }} 版</el-descriptions-item>
              <el-descriptions-item label="创建时间">{{ task.created_at ? formatDate(task.created_at) : "未记录" }}</el-descriptions-item>
              <el-descriptions-item v-if="task.remark" label="备注">{{ task.remark }}</el-descriptions-item>
            </el-descriptions>
          </el-popover>
        </div>

        <div class="task-summary" aria-label="任务结果概览">
          <button type="button" class="summary-item" @click="setTab('rules')">
            <span class="summary-label">审核事项</span><strong>{{ task.rules.length }}</strong>
          </button>
          <button type="button" class="summary-item" :class="{ 'summary-item--warning': unresolvedRuleCount > 0 }" @click="setTab('rules')">
            <span class="summary-label">未形成结论</span><strong>{{ unresolvedRuleCount }}</strong>
          </button>
          <button type="button" class="summary-item" :class="{ 'summary-item--danger': ruleAbnormalCount > 0 }" @click="setTab('rules')">
            <span class="summary-label">发现异常</span><strong>{{ ruleAbnormalCount }}</strong>
          </button>
          <button type="button" class="summary-item" @click="setTab('fields')">
            <span class="summary-label">字段已抽取</span><strong>{{ fieldExtractedCount }}<small> / {{ task.fields.length }}</small></strong>
          </button>
          <button type="button" class="summary-item" @click="setTab('materials')">
            <span class="summary-label">材料已上传</span><strong>{{ uploadedDocumentCount }}<small> / {{ task.documents.length }}</small></strong>
          </button>
        </div>

        <div v-if="blockers.length" class="task-notices">
          <div v-for="item in blockers" :key="item.title" class="task-notice" :class="`task-notice--${item.tone}`">
            <div><strong>{{ item.title }}</strong><div class="notice-description">{{ item.description }}</div></div>
            <el-button v-if="item.tab && tab !== item.tab" link type="primary" @click="setTab(item.tab)">{{ item.tabLabel || "查看" }}</el-button>
          </div>
        </div>
        <p v-if="primaryAction.reason && !blockers.length" class="text-danger primary-reason">{{ primaryAction.reason }}</p>

        <OperationPanel v-if="operation && (watching || operation.status !== 'SUCCEEDED' || operation.failure_code || operation.failure_message)"
          class="block-gap-sm" compact :operation="operation" :watching="watching" :label="operation.operation_type === 'DOCUMENT_CLASSIFY' ? '文件已保存，自动校验' : '执行进度'"
          :poll-seconds="pollIntervalSeconds" :can-retry="retryableOperation"
          @requery="requery()" @retry="retryOperation" />
      </template>
    </div>

    <template v-if="task">
      <!-- 业务结果与材料，完整执行记录按需查看。 -->
      <div class="app-panel audit-results-panel">
        <el-tabs :model-value="tab" @update:model-value="setTab">
          <el-tab-pane label="材料" name="materials">
            <el-alert v-if="materialTypeCatalog.state.error" class="block-gap-sm" type="error" show-icon :closable="false" :title="materialTypeCatalog.state.error">
              <el-button size="small" :loading="materialTypeCatalog.state.loading" @click="materialTypeCatalog.reload">重试读取目录</el-button>
            </el-alert>
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
                    :catalog="materialTypeCatalog"
                    :busy="uploadingDocumentNo === document.document_no"
                    :disabled="pending && uploadingDocumentNo !== document.document_no"
                    :replacement-impact="
                      replacementImpact && replacementImpact.materialName === document.material_name ? replacementImpact : null
                    "
                    @pick="(file, materialType) => uploadMaterial(file, document, materialType)"
                    @remove="removeDocument"
                    @inspect-processing="openProcessingDialog"
                  />
                </tbody>
              </table>
            </div>
            <p v-else class="text-muted">该场景没有定义材料位。</p>
          </el-tab-pane>

          <el-tab-pane label="字段结果" name="fields">
            <AuditFieldResults
              :fields="task.fields"
              :extraction-finished="extractionFinished"
              :correction-enabled="task.status !== 'COMPLETED' && task.status !== 'FAILED'"
              @open-evidence="openEvidence"
              @correct="openCorrection"
            />
          </el-tab-pane>

          <el-tab-pane label="规则结果" name="rules">
            <AuditRuleResults :rules="task.rules" />
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
              :running="watching"
              :loading="reportLoading"
              :error="reportError"
              @select-report="(reportNo: string) => loadReport(taskNo, stateGeneration, reportNo)"
            />
          </el-tab-pane>

          <el-tab-pane label="执行记录" name="executions">
            <OperationPanel v-if="operation?.status === 'SUCCEEDED' && !watching && !operation.failure_code && !operation.failure_message"
              class="block-gap-sm" compact :operation="operation" :watching="watching" label="最近一次执行"
              @requery="requery()" />
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
                <template #default="{ row }">{{ row.started_at ? formatDate(row.started_at) : "—" }}</template>
              </el-table-column>
              <el-table-column label="结束时间" width="170">
                <template #default="{ row }">{{ row.finished_at ? formatDate(row.finished_at) : "—" }}</template>
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
        <el-descriptions-item label="开始时间">{{ executionDetail.started_at ? formatDate(executionDetail.started_at) : "—" }}</el-descriptions-item>
        <el-descriptions-item label="结束时间">{{ executionDetail.finished_at ? formatDate(executionDetail.finished_at) : "—" }}</el-descriptions-item>
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
    </el-drawer>

    <MaterialProcessingDialog
      :state="processingState"
      @update:visible="processingState.processingDialogVisible = $event"
      @update:job-id="processingState.processingJobId = $event"
      @update:reason="processingState.processingReason = $event"
      @cancel-action="processingState.processingAction = null"
    />


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
  align-items: center;
}

.task-head-actions {
  margin-left: auto;
}

.task-summary {
  display: grid;
  grid-template-columns: repeat(5, minmax(0, 1fr));
  margin-top: 20px;
  border-top: 1px solid var(--el-border-color-lighter);
  padding-top: 16px;
}

.summary-item {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 8px;
  border: 0;
  border-right: 1px solid var(--el-border-color-lighter);
  background: transparent;
  color: var(--el-text-color-primary);
  padding: 0 20px;
  font: inherit;
  text-align: left;
  cursor: pointer;
}

.summary-item:first-child { padding-left: 0; }
.summary-item:last-child { border-right: 0; }
.summary-item:hover { background: var(--el-fill-color-light); }
.summary-item:focus-visible { outline: 2px solid var(--el-color-primary); outline-offset: 3px; }
.summary-label { color: var(--el-text-color-secondary); font-size: 13px; }
.summary-item strong { font-size: 26px; line-height: 1.2; font-weight: 600; }
.summary-item small { font-size: 14px; font-weight: 400; color: var(--el-text-color-secondary); }
.summary-item--warning strong { color: var(--el-color-warning-dark-2); }
.summary-item--danger strong { color: var(--el-color-danger); }

.task-notices {
  display: flex;
  flex-direction: column;
  gap: 8px;
  margin-top: 16px;
}

.task-notice {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 16px;
  padding: 10px 14px;
  border-left: 3px solid var(--el-color-info);
  background: var(--el-fill-color-light);
  font-size: 13px;
  line-height: 1.6;
  overflow-wrap: anywhere;
}

.task-notice--error { border-left-color: var(--el-color-danger); }
.task-notice--warning { border-left-color: var(--el-color-warning); }
.notice-description { color: var(--el-text-color-regular); }
.primary-reason { line-height: 1.6; }

@media (max-width: 760px) {
  .task-head-actions { width: 100%; margin-left: 0; }
  .task-summary { grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 16px; }
  .summary-item { padding: 0; border: 0; }
  .task-notice { flex-wrap: wrap; }
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

/* 审核结果是高频扫描区，使用紧凑密度；详情内容仍在抽屉中完整展开。 */
.audit-results-panel {
  padding: 10px 12px 12px;
}

.audit-results-panel :deep(.el-tabs__header) {
  margin: 0 0 8px;
}

.audit-results-panel :deep(.el-tabs__item) {
  height: 38px;
  padding: 0 16px;
  line-height: 38px;
}

.audit-results-panel :deep(.el-tabs__nav-wrap::after) {
  height: 1px;
}

.audit-results-panel :deep(.el-alert) {
  padding: 8px 12px;
}

.audit-results-panel :deep(.el-alert__title) {
  line-height: 1.45;
}
</style>
