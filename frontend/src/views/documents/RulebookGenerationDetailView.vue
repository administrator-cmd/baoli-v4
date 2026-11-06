<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from "vue";
import { onBeforeRouteLeave, onBeforeRouteUpdate, useRoute, useRouter } from "vue-router";
import { ElMessage, ElMessageBox } from "element-plus";

import type {
  GenerationDocument,
  GenerationExport,
  GenerationJob,
  GenerationRevision,
  GenerationStatus,
} from "@/api/contracts";
import {
  createGenerationExport,
  downloadGenerationExportFile,
  getGenerationJob,
  importGenerationExport,
  listGenerationExports,
  listGenerationMessages,
  listGenerationRevisions,
  retryGeneration,
  saveGenerationDocument,
  startGeneration,
  sendGenerationMessage,
  restoreGenerationRevision,
} from "@/api/rulebookGenerations";
import { listTemplates, type TemplateOption } from "@/api/imports";
import { ApiError, isOperationActive, operationIdempotencyKey } from "@/api/http";
import { formatDate } from "@/constants/labels";
import { useOperationTracker } from "@/composables/useOperationTracker";
import { useRequestRunner } from "@/composables/useRequestRunner";
import { useMediaQuery } from "@/composables/useMediaQuery";
import BackButton from "@/components/BackButton.vue";
import FailureNotice from "@/components/FailureNotice.vue";
import OperationPanel from "@/components/OperationPanel.vue";
import PageHeader from "@/components/PageHeader.vue";
import StateBlock from "@/components/StateBlock.vue";
import StatusBadge from "@/components/StatusBadge.vue";
import GenerationWorkbookPanel from "@/components/generation/GenerationWorkbookPanel.vue";
import GenerationIssuesPanel from "@/components/generation/GenerationIssuesPanel.vue";
import GenerationAssistantPanel from "@/components/generation/GenerationAssistantPanel.vue";
import {
  cloneValue,
  generationCounts,
  generationAssistantFailureMessage,
  generationStatusLabel,
  generationStatusTone,
} from "@/utils/generationDocument";

/**
 * 供业务人员核对与修改的规则库编制工作台。
 *
 * 页面只显示服务端真实状态：生成操作以 operation 为准，文档以已保存修订为准；
 * 未保存的页面修改不会被导出或导入，也不会被旧轮询覆盖。
 */
const route = useRoute();
const pinActions = useMediaQuery("(min-width: 993px)");
const router = useRouter();
const jobNo = computed(() => String(route.params.jobNo ?? ""));

const job = ref<GenerationJob | null>(null);
const draft = ref<GenerationDocument | null>(null);
// 编辑基准独立于刷新得到的作业信息，旧草稿不能借用其他人保存后的修订。
const draftRevision = ref<number | null>(null);
const draftVersion = ref(0);
const dirty = ref(false);
const loading = ref(true);
const loadError = ref<string | null>(null);
const exports = ref<GenerationExport[]>([]);
const revisionHistory = ref<GenerationRevision[]>([]);
type VersionRow = Pick<GenerationExport, "revision" | "filename" | "created_at" | "imports">;
const versionRows = computed<VersionRow[]>(() => {
  const rows = new Map<number, VersionRow>(exports.value.map(item => [item.revision, item]));
  const stem = job.value?.source_filename.replace(/\.[^.]+$/, "") || "规则库";
  for (const revision of revisionHistory.value) {
    const file = rows.get(revision.revision);
    if (!file && !job.value?.document_supported) continue;
    rows.set(revision.revision, {
      revision: revision.revision,
      filename: file?.filename ?? `${stem}-规则库-第${revision.revision}版.xlsx`,
      created_at: revision.created_at,
      imports: file?.imports ?? [],
    });
  }
  return [...rows.values()].sort((a, b) => b.revision - a.revision);
});
const activeTab = ref("content");
const generating = ref(false);
const saving = ref(false);
const exporting = ref(false);
const importing = ref(false);
const preparingImport = ref(false);
const assistantSubmitting = ref(false);
const assistantDialog = ref(false);
const assistantInstruction = ref("");
const assistantRefresh = ref(0);
const resultSummary = ref("");
const restoring = ref(false);
const pendingReload = ref(false);
const documentBusy = computed(() => saving.value || exporting.value || preparingImport.value || importing.value
  || restoring.value || assistantSubmitting.value || generating.value);
const versionOutputBusy = computed(() => loading.value || saving.value || exporting.value || preparingImport.value || importing.value || restoring.value);
const readOnly = computed(() => loading.value || saving.value || exporting.value || preparingImport.value || importing.value || assistantSubmitting.value || restoring.value || isGenerating.value || !job.value?.can_edit);
const hasContent = computed(() => Boolean(draft.value && (draft.value.sheets.length || draft.value.issues.length)));

const { operation, watching, track, requery, reset: resetTracking } = useOperationTracker();
const { failure, clear, setFailure } = useRequestRunner();

const displayOperation = computed(() => operation.value ?? job.value?.latest_operation ?? null);
const isGenerating = computed(() => isOperationActive(displayOperation.value?.status));
const isRevising = computed(() => displayOperation.value?.operation_type === "RULEBOOK_REVISE");
const displayStatus = computed<GenerationStatus>(() => {
  const status = displayOperation.value?.status;
  if (status === "PENDING" || status === "RUNNING") return status;
  if (!job.value?.document && (status === "FAILED" || status === "INTERRUPTED")) return status;
  return job.value?.status ?? "UNKNOWN";
});
const displayStatusMessage = computed(() => {
  if (displayStatus.value === "PENDING") return isRevising.value ? "修改要求已受理，等待 AI 处理" : "AI 编制已受理，等待后台执行";
  if (displayStatus.value === "RUNNING") return "AI 正在整理业务规则库，离开页面后仍会继续";
  return job.value?.status_message ?? "";
});
const counts = computed(() => generationCounts(draft.value));
const generateLabel = computed(() => (
  job.value?.status === "FAILED" || job.value?.status === "INTERRUPTED" ? "重试 AI 编制"
    : job.value?.can_retry ? "使用原文件重新编制" : "开始 AI 编制"
));

let loadRequest = 0;
let revisionRequest = 0;
let contextVersion = 0;
let editVersion = 0;
/** 尚未确认受理的幂等标识：响应丢失后的重发复用它，已确认或明确拒绝后清空。 */
let generationAttemptKey: string | null = null;
let assistantAttempt: { key: string; message: string; revision: number; issueId?: string } | null = null;

function errorText(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

function trackOperation(value: NonNullable<GenerationJob["latest_operation"]>): void {
  const context = contextVersion;
  track(value, {
    onFinished: async (finished) => {
      if (context !== contextVersion) return;
      if (finished.status === "SUCCEEDED") {
        ElMessage.success(finished.operation_type === "RULEBOOK_REVISE" ? "AI 已处理完成" : "AI 编制已完成，请核对业务内容");
      }
      await loadJob({ replaceDraft: !dirty.value });
    },
    onUpdate: (next) => {
      if (context !== contextVersion) return;
      if (job.value) job.value.latest_operation = next;
    },
    onError: (error) => { if (context === contextVersion) setFailure(error); },
  });
}

function applyJob(payload: GenerationJob, replaceDraft: boolean): void {
  if (job.value && payload.revision < job.value.revision) return;
  if (payload.revision !== job.value?.revision) resultSummary.value = "";
  const firstDocument = !draft.value && payload.document;
  job.value = payload;
  assistantRefresh.value += 1;
  if (replaceDraft || !dirty.value) {
    draft.value = payload.document ? cloneValue(payload.document) : null;
    draftVersion.value += 1;
    draftRevision.value = payload.document ? payload.revision : null;
    dirty.value = false;
  }
  if (firstDocument) activeTab.value = "content";
  if (activeTab.value === "issues" && !payload.document?.issues.length) activeTab.value = "content";
  if (!payload.document) activeTab.value = "files";
  const latest = payload.latest_operation;
  if (latest && isOperationActive(latest.status)) {
    trackOperation(latest);
  } else {
    resetTracking();
  }
}

async function refreshRevisionHistory(targetJobNo: string): Promise<GenerationRevision[]> {
  const context = contextVersion;
  const request = ++revisionRequest;
  const items = await listGenerationRevisions(targetJobNo);
  if (context === contextVersion && request === revisionRequest) revisionHistory.value = items;
  return items;
}

async function loadJob(options: { replaceDraft?: boolean } = {}): Promise<void> {
  if (documentBusy.value) {
    pendingReload.value = true;
    return;
  }
  pendingReload.value = false;
  const context = contextVersion;
  const targetJobNo = jobNo.value;
  const edits = editVersion;
  const request = ++loadRequest;
  loading.value = true;
  loadError.value = null;
  try {
    const payload = await getGenerationJob(targetJobNo);
    if (context !== contextVersion || request !== loadRequest) return;
    applyJob(payload, options.replaceDraft !== false && edits === editVersion);
    const [files, revisions, chat] = await Promise.allSettled([
      listGenerationExports(targetJobNo), refreshRevisionHistory(targetJobNo), listGenerationMessages(targetJobNo),
    ]);
    if (context === contextVersion && request === loadRequest) {
      if (files.status === "rejected") throw files.reason;
      const items = files.value;
      exports.value = items;
      if (revisions.status === "rejected") throw revisions.reason;
      const latestReply = chat.status === "fulfilled" ? chat.value.items[chat.value.items.length - 1] : null;
      resultSummary.value = latestReply?.role === "ASSISTANT" && latestReply.revision === payload.revision && latestReply.operation.result?.reply_kind === "CLARIFY"
        ? latestReply.content
        : revisions.status === "fulfilled" ? revisions.value.find(item => item.revision === payload.revision)?.summary ?? "" : "";
      const requestedExport = typeof route.query.import_export === "string"
        ? route.query.import_export : "";
      if (requestedExport) {
        const target = items.find((item) => item.export_no === requestedExport);
        if (target) {
          void router.replace({ name: "rulebook-generation-detail", params: { jobNo: targetJobNo } });
          void openImport(target);
        } else {
          activeTab.value = "files";
          ElMessage.error("所选文件版本不存在，请在版本与使用记录中重新选择");
        }
      }
    }
  } catch (error) {
    if (context === contextVersion && request === loadRequest) loadError.value = errorText(error);
  } finally {
    if (context === contextVersion && request === loadRequest) loading.value = false;
  }
}

async function refresh(): Promise<void> {
  if (assistantSubmitting.value || restoring.value || generating.value || saving.value || loading.value || exporting.value || preparingImport.value || importing.value) return;
  const context = contextVersion;
  if (dirty.value) {
    try {
      await ElMessageBox.confirm(
        "刷新会丢弃尚未保存的页面修改。已保存的修订仍会保留。确认刷新？",
        "刷新编制结果",
        { type: "warning", confirmButtonText: "刷新", cancelButtonText: "取消" },
      );
    } catch {
      return;
    }
  }
  if (context !== contextVersion) return;
  await loadJob({ replaceDraft: true });
}

async function generate(): Promise<void> {
  if (!job.value || loading.value || generating.value || isGenerating.value
    || (!job.value.can_generate && !job.value.can_retry)) return;
  const context = contextVersion;
  const targetJobNo = jobNo.value;
  const retry = job.value.can_retry;
  generating.value = true;
  clear();
  const key = generationAttemptKey ?? operationIdempotencyKey(retry ? "generation-retry" : "generation", targetJobNo);
  generationAttemptKey = key;
  try {
    if (retry) {
      let result = await retryGeneration(targetJobNo, key);
      if (context !== contextVersion) return;
      if (result.reused && ["FAILED", "INTERRUPTED"].includes(result.operation.status)) {
        generationAttemptKey = operationIdempotencyKey("generation-retry", targetJobNo);
        result = await retryGeneration(targetJobNo, generationAttemptKey);
      }
      if (context !== contextVersion) return;
      // 新记录交给路由加载并追踪；留在当前记录时用服务端最新状态恢复工作台。
      if (result.job.job_no !== targetJobNo) {
        await router.push({ name: "rulebook-generation-detail", params: { jobNo: result.job.job_no } });
        return;
      }
      generationAttemptKey = null;
      applyJob(result.job, true);
      return;
    }
    let accepted = await startGeneration(targetJobNo, key);
    if (context !== contextVersion) return;
    if (accepted.reused && ["FAILED", "INTERRUPTED"].includes(accepted.status)) {
      generationAttemptKey = operationIdempotencyKey("generation-retry", targetJobNo);
      accepted = await startGeneration(targetJobNo, generationAttemptKey);
    }
    if (context !== contextVersion) return;
    generationAttemptKey = null;
    if (job.value) job.value.latest_operation = accepted;
    trackOperation(accepted);
  } catch (error) {
    if (context !== contextVersion) return;
    // 4xx 是服务端明确拒绝，重试需要新的显式操作；网络超时或 5xx 保留同一标识，
    // 避免响应丢失后重复受理或重复调用模型。
    if (error instanceof ApiError && error.statusCode !== null
      && error.statusCode >= 400 && error.statusCode < 500) {
      generationAttemptKey = null;
    }
    setFailure(error);
    if (error instanceof ApiError && error.code === "OPERATION_ALREADY_ACTIVE") {
      await loadJob({ replaceDraft: !dirty.value });
    }
  } finally {
    if (context === contextVersion) generating.value = false;
  }
}

async function sendInstruction(message: string, issueId?: string): Promise<boolean> {
  if (!job.value?.document || readOnly.value) return false;
  const context = contextVersion;
  const targetJobNo = jobNo.value;
  assistantSubmitting.value = true;
  clear();
  try {
    if (dirty.value && !await save()) return false;
    if (context !== contextVersion || !job.value) return false;
    const revision = job.value.revision;
    const attempt = assistantAttempt?.message === message && assistantAttempt.revision === revision && assistantAttempt.issueId === issueId
      ? assistantAttempt : { key: operationIdempotencyKey("generation-edit", targetJobNo), message, revision, issueId };
    assistantAttempt = attempt;
    let accepted = await sendGenerationMessage(targetJobNo, {
      message: attempt.message, expectedRevision: attempt.revision, idempotencyKey: attempt.key, issueId: attempt.issueId,
    });
    if (context !== contextVersion) return false;
    if (accepted.reused && ["FAILED", "INTERRUPTED"].includes(accepted.status)) {
      const next = { ...attempt, key: operationIdempotencyKey("generation-edit", targetJobNo) };
      assistantAttempt = next;
      accepted = await sendGenerationMessage(targetJobNo, {
        message: next.message, expectedRevision: next.revision, idempotencyKey: next.key, issueId: next.issueId,
      });
    }
    if (context !== contextVersion) return false;
    assistantAttempt = null;
    if (!issueId && assistantInstruction.value.trim() === message) assistantInstruction.value = "";
    job.value.latest_operation = accepted;
    assistantRefresh.value += 1;
    trackOperation(accepted);
    return true;
  } catch (error) {
    if (context !== contextVersion) return false;
    if (error instanceof ApiError && error.statusCode !== null && error.statusCode >= 400 && error.statusCode < 500) assistantAttempt = null;
    setFailure(error);
    if (error instanceof ApiError && ["OPERATION_ALREADY_ACTIVE", "GENERATION_REVISION_CONFLICT", "GENERATION_ISSUE_NOT_FOUND"].includes(error.code)) {
      await loadJob({ replaceDraft: !dirty.value });
    }
    return false;
  } finally {
    if (context === contextVersion) assistantSubmitting.value = false;
  }
}

async function downloadRevision(revision: number): Promise<void> {
  if (versionOutputBusy.value) return;
  const context = contextVersion;
  const target = jobNo.value;
  exporting.value = true;
  clear();
  try {
    const item = await createGenerationExport(target, revision);
    if (context !== contextVersion) return;
    await downloadGenerationExportFile(target, item);
    if (context === contextVersion) exports.value = [item, ...exports.value.filter(entry => entry.export_no !== item.export_no)].sort((a, b) => b.revision - a.revision);
  } catch (error) {
    if (context === contextVersion) setFailure(error);
  } finally {
    if (context === contextVersion) exporting.value = false;
  }
}

async function restoreRevision(revision: number): Promise<void> {
  if (!job.value || readOnly.value) return;
  const context = contextVersion;
  try {
    await ElMessageBox.confirm(`将第 ${revision} 版恢复为新的当前版本，已有历史仍保留。${dirty.value ? '当前未保存的修改会被替换。' : ''}`, "恢复历史版本", {
      confirmButtonText: "恢复", cancelButtonText: "取消", type: "warning",
    });
  } catch { return; }
  if (context !== contextVersion || !job.value || readOnly.value) return;
  restoring.value = true;
  clear();
  try {
    const payload = await restoreGenerationRevision(jobNo.value, job.value.revision, revision);
    if (context === contextVersion) {
      applyJob(payload, true);
      ElMessage.success("已恢复为新的当前版本");
      await refreshRevisionHistory(jobNo.value).catch(error => { if (context === contextVersion) setFailure(error); });
    }
  } catch (error) {
    if (context === contextVersion) setFailure(error);
  } finally {
    if (context === contextVersion) restoring.value = false;
  }
}

function markDirty(): void {
  editVersion += 1;
  dirty.value = true;
}

function updateDocument(document: GenerationDocument): void {
  if (!draft.value || readOnly.value) return;
  draft.value = document;
  markDirty();
}

async function save(): Promise<boolean> {
  if (!job.value || !draft.value || draftRevision.value === null || saving.value) return false;
  if (!dirty.value) return true;
  const context = contextVersion;
  const targetJobNo = jobNo.value;
  const edits = editVersion;
  const revision = draftRevision.value;
  const document = cloneValue(draft.value);
  loadRequest += 1;
  revisionRequest += 1;
  loading.value = false;
  saving.value = true;
  clear();
  try {
    const updated = await saveGenerationDocument(targetJobNo, revision, document);
    if (context !== contextVersion) return false;
    applyJob(updated, edits === editVersion);
    draftRevision.value = updated.revision;
    ElMessage.success(edits === editVersion ? "已保存业务修改" : "已保存提交的内容，页面仍有新的未保存修改");
    await refreshRevisionHistory(targetJobNo).catch(error => { if (context === contextVersion) setFailure(error); });
    if (context !== contextVersion) return false;
    return edits === editVersion;
  } catch (error) {
    if (context === contextVersion) setFailure(error);
    return false;
  } finally {
    if (context === contextVersion) saving.value = false;
  }
}

const titleDialog = ref(false);
const titleForm = ref({ title: "", summary: "" });
function openTitle(): void {
  if (!draft.value || readOnly.value) return;
  titleForm.value = { title: draft.value.title, summary: draft.value.summary };
  titleDialog.value = true;
}
function applyTitle(): void {
  if (!draft.value || readOnly.value) return;
  if (!titleForm.value.title.trim()) { ElMessage.error("请填写规则库名称"); return; }
  updateDocument({ ...draft.value, ...titleForm.value });
  titleDialog.value = false;
}

function ensureSavedForOutput(): boolean {
  if (!job.value?.document) {
    ElMessage.warning("当前没有已保存的编制结果");
    return false;
  }
  if (dirty.value) {
    ElMessage.warning("有未保存修改，请先保存后再导出或导入");
    return false;
  }
  return true;
}

async function freezeCurrentExport(): Promise<GenerationExport | null> {
  const context = contextVersion;
  if (dirty.value && !await save()) return null;
  if (context !== contextVersion || !job.value || !ensureSavedForOutput()) return null;
  const item = await createGenerationExport(jobNo.value, job.value.revision);
  if (context !== contextVersion) return null;
  exports.value = [item, ...exports.value.filter((entry) => entry.export_no !== item.export_no)]
    .sort((first, second) => second.revision - first.revision);
  return item;
}
async function downloadCurrent(): Promise<void> {
  if (!job.value || exporting.value || preparingImport.value || saving.value) return;
  const context = contextVersion;
  const targetJobNo = jobNo.value;
  exporting.value = true;
  clear();
  try {
    const item = await freezeCurrentExport();
    if (!item || context !== contextVersion) return;
    await downloadGenerationExportFile(targetJobNo, item);
    if (context === contextVersion) ElMessage.success(`已发起第 ${item.revision} 版 Excel 下载`);
  } catch (error) {
    if (context === contextVersion) setFailure(error);
  } finally {
    if (context === contextVersion) exporting.value = false;
  }
}
async function useCurrentInScenario(): Promise<void> {
  if (!job.value || preparingImport.value || exporting.value || saving.value) return;
  const context = contextVersion;
  preparingImport.value = true;
  clear();
  try {
    const item = await freezeCurrentExport();
    if (item && context === contextVersion) await openImport(item);
  } catch (error) {
    if (context === contextVersion) setFailure(error);
  } finally {
    if (context === contextVersion) preparingImport.value = false;
  }
}

async function useRevisionInScenario(revision: number): Promise<void> {
  if (versionOutputBusy.value || dirty.value) return;
  const context = contextVersion;
  const targetJobNo = jobNo.value;
  preparingImport.value = true;
  clear();
  try {
    const item = await createGenerationExport(targetJobNo, revision);
    if (context !== contextVersion) return;
    exports.value = [item, ...exports.value.filter(entry => entry.export_no !== item.export_no)]
      .sort((a, b) => b.revision - a.revision);
    await openImport(item);
  } catch (error) {
    if (context === contextVersion) setFailure(error);
  } finally {
    if (context === contextVersion) preparingImport.value = false;
  }
}

const importDialog = ref(false);
const importTarget = ref<GenerationExport | null>(null);
const importScenario = ref("");
const importKey = ref("");
const scenarios = ref<TemplateOption[]>([]);
const loadingScenarios = ref(false);
const scenarioError = ref<string | null>(null);
const importError = ref<string | null>(null);

async function openImport(item: GenerationExport): Promise<void> {
  if (importing.value || dirty.value) return;
  importTarget.value = item;
  importScenario.value = "";
  importKey.value = operationIdempotencyKey("generation-import", item.export_no);
  importError.value = null;
  importDialog.value = true;
  if (scenarios.value.length || loadingScenarios.value) return;
  await loadScenarios();
}

async function loadScenarios(): Promise<void> {
  if (loadingScenarios.value) return;
  const context = contextVersion;
  loadingScenarios.value = true;
  scenarioError.value = null;
  try {
    // 只提供可用场景；停用场景即使选到也会被服务端拒绝。
    const items = await listTemplates();
    if (context !== contextVersion) return;
    scenarios.value = items.filter((item) => item.enabled);
  } catch (error) {
    if (context === contextVersion) scenarioError.value = errorText(error);
  } finally {
    if (context === contextVersion) loadingScenarios.value = false;
  }
}

function onImportScenarioChange(): void {
  if (!importTarget.value) return;
  // 同一场景的重发复用同一幂等键；换了场景就是新的显式导入操作。
  importKey.value = operationIdempotencyKey(
    "generation-import", importTarget.value.export_no, importScenario.value,
  );
}

async function confirmImport(): Promise<void> {
  if (!importTarget.value || importing.value) return;
  if (!importScenario.value) {
    ElMessage.warning("请选择要导入的场景");
    return;
  }
  const context = contextVersion;
  const targetJobNo = jobNo.value;
  importing.value = true;
  importError.value = null;
  clear();
  try {
    const result = await importGenerationExport(targetJobNo, {
      exportNo: importTarget.value.export_no,
      scenarioCode: importScenario.value,
      idempotencyKey: importKey.value,
    });
    if (context !== contextVersion) return;
    importDialog.value = false;
    await router.push({ name: "scene-import-detail", params: { jobNo: result.import_job_no } });
  } catch (error) {
    if (context === contextVersion) importError.value = errorText(error);
  } finally {
    if (context === contextVersion) importing.value = false;
  }
}

function openImportJob(link: GenerationExport["imports"][number]): void {
  if (link.import_job_no) {
    void router.push({ name: "scene-import-detail", params: { jobNo: link.import_job_no } });
  }
}

function beforeUnload(event: BeforeUnloadEvent): void {
  if (!dirty.value) return;
  event.preventDefault();
  event.returnValue = "";
}

onMounted(() => {
  window.addEventListener("beforeunload", beforeUnload);
  void loadJob({ replaceDraft: true });
});

watch(() => pendingReload.value && !documentBusy.value && !loading.value, ready => {
  if (ready) void loadJob({ replaceDraft: !dirty.value });
});

watch(jobNo, () => {
  contextVersion += 1;
  loadRequest += 1;
  editVersion = 0;
  generationAttemptKey = null;
  assistantAttempt = null;
  assistantDialog.value = false;
  assistantInstruction.value = "";
  resultSummary.value = "";
  assistantSubmitting.value = false;
  restoring.value = false;
  resetTracking();
  clear();
  job.value = null;
  draft.value = null;
  draftRevision.value = null;
  dirty.value = false;
  exports.value = [];
  revisionHistory.value = [];
  pendingReload.value = false;
  activeTab.value = "content";
  generating.value = false;
  saving.value = false;
  exporting.value = false;
  importing.value = false;
  preparingImport.value = false;
  titleDialog.value = false;
  importDialog.value = false;
  importTarget.value = null;
  loadingScenarios.value = false;
  scenarioError.value = null;
  importError.value = null;
  void loadJob({ replaceDraft: true });
});

onUnmounted(() => {
  contextVersion += 1;
  loadRequest += 1;
  window.removeEventListener("beforeunload", beforeUnload);
});

async function confirmDiscard(): Promise<boolean> {
  if (!dirty.value) return true;
  try {
    await ElMessageBox.confirm(
      "页面有未保存的编制修改，离开后会丢失。确认离开？",
      "离开页面",
      { type: "warning", confirmButtonText: "离开", cancelButtonText: "留下" },
    );
    return true;
  } catch {
    return false;
  }
}

onBeforeRouteLeave(confirmDiscard);
onBeforeRouteUpdate(confirmDiscard);
</script>

<template>
  <div class="app-panel authoring-page">
    <PageHeader title="AI 辅助编制规则库" :notes="job ? [job.source_filename, job.revision ? `已保存第 ${job.revision} 版` : '原始业务资料已上传'] : []" :busy="loading" @refresh="refresh">
      <template #leading><BackButton label="返回规则库" @click="router.push({ name: 'rulebooks' })" /></template>
      <template #actions>
        <el-button v-if="job && !draft && job.can_generate && !job.can_retry" type="primary" :loading="generating || isGenerating" :disabled="loading || isGenerating" @click="generate">{{ isGenerating ? 'AI 编制中' : generateLabel }}</el-button>
        <template v-if="draft">
          <el-button v-if="dirty" :loading="saving" :disabled="readOnly" @click="save">保存修改</el-button>
          <el-button v-if="job?.document_supported" type="primary" plain :disabled="loading" @click="assistantDialog = true">让 AI 修改</el-button>
          <el-button type="primary" :loading="exporting" :disabled="readOnly || !hasContent" @click="downloadCurrent">{{ dirty ? '保存并下载 Excel' : '下载规则库 Excel' }}</el-button>
          <el-button :disabled="readOnly || !hasContent" @click="useCurrentInScenario">用于场景</el-button>
          <el-button :disabled="readOnly" @click="openTitle">修改名称与说明</el-button>
        </template>
      </template>
    </PageHeader>

    <FailureNotice v-if="!assistantDialog" :error="failure" @dismiss="clear" />
    <StateBlock v-if="loadError" kind="error" title="编制记录读取失败" :description="loadError"><el-button @click="loadJob({ replaceDraft: !dirty })">重试读取</el-button></StateBlock>
    <StateBlock v-else-if="loading && !job" kind="loading" />
    <template v-else-if="job">
      <el-steps v-if="!draft" :active="1" simple class="authoring-page__steps">
        <el-step title="上传业务资料" /><el-step title="AI 整理编制" /><el-step title="核对与导出" />
      </el-steps>
      <div class="authoring-page__status">
        <StatusBadge :status="displayStatus" :label="generationStatusLabel(displayStatus)" :tone="generationStatusTone(displayStatus)" />
        <span v-if="displayStatus !== 'EDITABLE'">{{ displayStatusMessage }}</span>
        <el-tag v-if="dirty" type="warning">有未保存修改</el-tag>
      </div>
      <div v-if="job.can_retry && !draft && !isGenerating" class="authoring-page__recovery">
        <div class="authoring-page__recovery-copy">
          <strong>可使用已上传的资料重新编制</strong>
          <p>无需重新上传，沿用原 Excel、业务背景和编制要求，重新执行完整编制流程。</p>
          <p v-if="job.retry_requires_new_job">本次使用更新后的编制流程，自动打开新记录，原记录保留。</p>
        </div>
        <div class="authoring-page__recovery-actions">
          <el-button :disabled="generating || loading" @click="router.push({ name: 'rulebook-generate' })">更换 Excel</el-button>
          <el-button type="primary" :loading="generating" :disabled="loading" @click="generate">{{ generateLabel }}</el-button>
        </div>
      </div>
      <OperationPanel v-if="displayOperation && (!draft || isGenerating || (isRevising && displayOperation.status !== 'SUCCEEDED'))" compact :operation="displayOperation" :watching="watching" :failure-message="generationAssistantFailureMessage(displayOperation)" :label="isRevising ? 'AI 修改规则库' : 'AI 编制规则库'" eyebrow="处理进度" @requery="requery" />

      <el-alert v-if="!job.document_supported" class="block-gap" type="info" :closable="false" show-icon title="此记录来自之前的编制格式">
        这份历史结果按系统配置组织，原始文件和已有下载文件仍保留。新的 AI 编制会生成供业务人员阅读和修改的规则库。
        <el-button v-if="!job.can_retry" class="block-gap-sm" @click="router.push({ name: 'rulebook-generate' })">新建业务规则库编制</el-button>
      </el-alert>
      <el-alert v-else-if="job.status === 'NO_DEFINITIONS'" class="block-gap" type="warning" :closable="false" show-icon title="未识别到可编制的业务内容">
        {{ draft?.summary || '请核对原始 Excel 中是否包含业务规则、判断条件或计算要求。' }}
      </el-alert>

      <template v-if="draft">
        <div class="authoring-page__heading">
          <h2>{{ draft.title }}</h2>
        </div>
        <div class="authoring-page__summary">
          <button type="button" @click="activeTab = 'content'"><strong>{{ counts.sheets }}</strong> 张业务表 · <strong>{{ counts.entries }}</strong> 条内容</button>
          <button v-if="counts.openIssues" type="button" class="authoring-page__attention" @click="activeTab = 'issues'"><strong>{{ counts.openIssues }}</strong> 项需要补充</button>
        </div>
        <p v-if="resultSummary" class="authoring-page__result">{{ resultSummary }}</p>
      </template>

      <div class="authoring-page__workspace">
      <el-tabs v-model="activeTab" class="authoring-page__tabs">
        <el-tab-pane v-if="draft" label="规则库内容" name="content">
          <GenerationWorkbookPanel :key="jobNo" :document="draft" :refresh-token="draftVersion" :disabled="readOnly" @change="updateDocument" />
        </el-tab-pane>
        <el-tab-pane v-if="draft?.issues.length" :label="counts.openIssues ? `需要你补充（${counts.openIssues}）` : '补充记录'" name="issues">
          <GenerationIssuesPanel :key="jobNo" :document="draft" :disabled="readOnly" :error-message="failure?.message" :submit-answer="sendInstruction" />
        </el-tab-pane>
        <el-tab-pane label="版本与使用记录" name="files">
          <el-table :data="versionRows" border stripe class="management-table" empty-text="暂无已保存版本">
            <el-table-column label="版本" width="110"><template #default="{ row }">第 {{ row.revision }} 版 <el-tag v-if="row.revision === job.revision && !dirty" size="small">当前</el-tag></template></el-table-column>
            <el-table-column prop="filename" label="规则库文件" min-width="240" show-overflow-tooltip />
            <el-table-column label="保存时间" width="180"><template #default="{ row }">{{ formatDate(row.created_at) }}</template></el-table-column>
            <el-table-column label="使用记录" min-width="190">
              <template #default="{ row }">
                <div v-for="link in row.imports" :key="link.created_at">
                  <span v-if="link.deleted" class="text-hint">此前的场景导入已删除</span>
                  <el-button v-else link type="primary" @click="openImportJob(link)">查看场景导入进度</el-button>
                </div>
                <span v-if="!row.imports.length" class="text-hint">尚未用于场景</span>
              </template>
            </el-table-column>
            <el-table-column label="操作" width="230" :fixed="pinActions ? 'right' : false" class-name="table-actions"><template #default="{ row }">
              <el-button link type="primary" :disabled="versionOutputBusy" @click="downloadRevision(row.revision)">下载此版本</el-button>
              <el-button link type="primary" :disabled="dirty || versionOutputBusy" @click="useRevisionInScenario(row.revision)">用于场景</el-button>
            </template></el-table-column>
          </el-table>
        </el-tab-pane>
      </el-tabs>
      </div>
    </template>

    <el-dialog v-model="assistantDialog" title="AI 编制助手" width="min(1040px, calc(100vw - 32px))" top="5vh" destroy-on-close :close-on-click-modal="false">
      <div class="authoring-page__assistant-content">
        <FailureNotice :error="assistantDialog ? failure : null" @dismiss="clear" />
        <GenerationAssistantPanel v-if="draft && job?.document_supported" v-model="assistantInstruction" :job-no="jobNo" :revision="job.revision" :refresh-token="assistantRefresh" :disabled="readOnly" :busy="assistantSubmitting || (isRevising && isGenerating)" @send="sendInstruction" @download="downloadRevision" @restore="restoreRevision" />
      </div>
    </el-dialog>
    <el-dialog v-model="titleDialog" title="规则库名称与说明" width="min(760px, 94vw)" destroy-on-close :close-on-click-modal="false">
      <el-form label-position="top" :disabled="readOnly">
        <el-form-item label="规则库名称" required><el-input v-model="titleForm.title" /></el-form-item>
        <el-form-item label="业务范围与编制说明"><el-input v-model="titleForm.summary" type="textarea" :rows="4" /></el-form-item>
      </el-form>
      <template #footer><el-button @click="titleDialog = false">取消</el-button><el-button type="primary" :disabled="readOnly" @click="applyTitle">应用修改</el-button></template>
    </el-dialog>
    <el-dialog v-model="importDialog" title="将规则库用于场景" width="min(620px, 94vw)" destroy-on-close :close-on-click-modal="!importing" :close-on-press-escape="!importing" :show-close="!importing">
      <p v-if="importTarget">使用第 <strong>{{ importTarget.revision }}</strong> 版规则库 Excel<span v-if="importTarget.revision !== job?.revision">（历史版本）</span>。</p>
      <el-alert v-if="scenarioError" type="error" :closable="false" show-icon :title="`场景读取失败：${scenarioError}`">
        <el-button :loading="loadingScenarios" @click="loadScenarios">重新读取场景</el-button>
      </el-alert>
      <el-alert v-else-if="!loadingScenarios && !scenarios.length" type="info" :closable="false" title="尚无可用场景，请先在场景管理中创建或启用场景。" />
      <el-alert v-if="importError" class="block-gap-sm" type="error" :closable="false" show-icon :title="importError" />
      <el-form label-position="top" class="block-gap">
        <el-form-item label="目标场景" required>
          <el-select v-model="importScenario" :disabled="importing" filterable style="width: 100%" placeholder="选择已有场景" :loading="loadingScenarios" @change="onImportScenarioChange">
            <el-option v-for="item in scenarios" :key="item.scenario_code" :value="item.scenario_code" :label="item.scenario_name" />
          </el-select>
        </el-form-item>
      </el-form>
      <template #footer><el-button :disabled="importing" @click="importDialog = false">取消</el-button><el-button type="primary" :loading="importing" :disabled="loadingScenarios || !importScenario" @click="confirmImport">进入 Excel 解析</el-button></template>
    </el-dialog>
  </div>
</template>

<style scoped>
.authoring-page__steps { margin-bottom: 20px; }
.authoring-page__status { display: flex; align-items: center; flex-wrap: wrap; gap: 12px; margin-bottom: 20px; color: var(--el-text-color-secondary); }
.authoring-page__recovery { display: flex; align-items: center; flex-wrap: wrap; gap: 16px 24px; margin-bottom: 20px; padding: 16px 20px; border: 1px solid var(--el-border-color-light); border-radius: var(--el-border-radius-base); background: var(--el-fill-color-light); }
.authoring-page__recovery-copy { flex: 1 1 340px; min-width: 0; }
.authoring-page__recovery-copy p { margin: 6px 0 0; color: var(--el-text-color-secondary); line-height: 1.6; }
.authoring-page__recovery-actions { display: flex; align-items: center; flex-wrap: wrap; gap: 8px; }
.authoring-page__recovery-actions :deep(.el-button + .el-button) { margin-left: 0; }
.authoring-page__heading { display: flex; align-items: flex-start; justify-content: space-between; gap: 24px; margin: 24px 0 16px; }
.authoring-page__heading h2 { margin: 0; font-size: 20px; }
.authoring-page__summary { display: flex; flex-wrap: wrap; align-items: center; gap: 16px 24px; padding: 16px 0; border-top: 1px solid var(--el-border-color-lighter); border-bottom: 1px solid var(--el-border-color-lighter); }
.authoring-page__summary button { border: 0; background: none; color: var(--el-text-color-primary); font: inherit; text-align: left; padding: 4px; cursor: pointer; }
.authoring-page__summary button:hover { color: var(--el-color-primary); }
.authoring-page__summary button:focus-visible { outline: 2px solid var(--el-color-primary); outline-offset: 3px; }
.authoring-page__summary strong { font-size: 20px; }
.authoring-page__summary .authoring-page__attention { color: var(--el-color-warning-dark-2); }
.authoring-page__result { margin: 16px 0 0; line-height: 1.7; white-space: pre-wrap; overflow-wrap: anywhere; }
.authoring-page__tabs { margin-top: 18px; }
.authoring-page__workspace { min-width: 0; }
.authoring-page__assistant-content { display: flex; flex-direction: column; height: min(700px, calc(100dvh - 160px)); min-height: 0; }
.authoring-page__assistant-content :deep(.failure-notice) { flex-shrink: 0; max-height: 160px; overflow-y: auto; }
@media (max-width: 700px) { .authoring-page__heading { flex-direction: column; gap: 8px; } .authoring-page__steps { padding: 12px; } .authoring-page__steps :deep(.el-step__title) { font-size: 12px; } }
</style>

