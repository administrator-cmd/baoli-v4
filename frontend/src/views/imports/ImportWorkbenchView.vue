<script setup lang="ts">
import { computed, h, onMounted, ref, watch } from "vue";
import { useRoute, useRouter } from "vue-router";
import { ElMessage, ElMessageBox } from "element-plus";
import { getOperation, isOperationActive, isOperationFinished, operationIdempotencyKey, type ApiError, type OperationView } from "@/api/http";
import {
  applyImportToDraft,
  createImportJob,
  getImportJob,
  listImportJobs,
  listTemplates,
  type TemplateOption,
  startImportConversion,
} from "@/api/imports";
import type {
  ImportJob,
  ImportJobSummary,
  ProposalCalculation,
  UploadPolicy,
} from "@/api/contracts";
import ImportProposalEditor from "@/components/import/ImportProposalEditor.vue";
import FileUploadCard from "@/components/import/FileUploadCard.vue";
import ImportJobPicker from "@/components/import/ImportJobPicker.vue";
import ImportStepNav from "@/components/import/ImportStepNav.vue";
import FailureNotice from "@/components/FailureNotice.vue";
import PageHeader from "@/components/PageHeader.vue";
import StateBlock from "@/components/StateBlock.vue";
import StatusBadge from "@/components/StatusBadge.vue";
import { buildImportSteps } from "@/constants/importSteps";
import { useOperationTracker } from "@/composables/useOperationTracker";
import { useRequestRunner } from "@/composables/useRequestRunner";
import { getUploadPolicy } from "@/api/system";
import { getSceneConfiguration } from "@/api/scenes";
import { failureNextStep } from "@/constants/labels";

const route = useRoute();
const router = useRouter();
const { operation, watching, track, reset: resetTracking } = useOperationTracker();
const { pending, failure, formMessage, run, setFailure } = useRequestRunner();

const job = ref<ImportJob | null>(null);
const importJobs = ref<ImportJobSummary[]>([]);
const templates = ref<TemplateOption[]>([]);
/**
 * 本次上传所属的场景编码。
 *
 * 上传流程以场景为起点：用户先建场景、再在该场景下上传，作业明确记录它属于哪个场景。
 * 系统不再从 Excel 表头推断场景——猜错场景会拿另一套列协议解释这份文件，
 * 报出一堆与真实问题无关的缺列错误，而用户手里的文件根本没变。
 */
const uploadScenarioCode = ref("");
/** 后端上传策略；未取得时为 null，上传卡片据此不做前端预检。 */
const uploadPolicy = ref<UploadPolicy | null>(null);
const activeStep = ref(0);
const proposalSaving = ref(false);
/** 首次进入时的加载状态：与「还没有数据」分开，避免把请求失败显示成空。 */
const loadingJob = ref(false);
const loadingJobs = ref(false);
/** 本次浏览器选择的文件大小；刷新后浏览器不再保留该信息。 */
const selectedFileSize = ref<number | null>(null);
/** 尚未确认受理的幂等标识：同一次业务动作再次提交时复用它，不会产生第二次执行。 */
let conversionAttemptKey: string | null = null;

let jobReception = 0;
let jobLoadRequest = 0;

const proposalMaterials = computed(() => job.value?.proposals?.materials ?? []);
const proposalFields = computed(() => job.value?.proposals?.fields ?? []);
const proposalRules = computed(() => job.value?.proposals?.rules ?? []);
const proposalReports = computed(() => job.value?.proposals?.report_sections ?? []);
const proposalCalculations = computed<ProposalCalculation[]>(() => job.value?.proposals?.calculations ?? []);
const hasProposals = computed(() => Boolean(job.value?.proposals));

const proposalTotals = computed(() => ({
  materials: proposalMaterials.value.length,
  fields: proposalFields.value.length,
  rules: proposalRules.value.length,
  reportSections: proposalReports.value.length,
}));
const includedCalculations = computed(() => proposalCalculations.value);
/** 面向业务展示解析失败原因；操作编号、阶段等技术信息不进入页面。 */
const jobFailure = computed(() => {
  if (job.value?.failure) return job.value.failure;
  if (!operation.value?.failure_code) return null;
  return {
    code: operation.value.failure_code,
    message: operation.value.failure_message ?? "规则解析未完成",
  };
});
const published = computed(() => job.value?.status === "PUBLISHED");
/** 已写入场景配置，或历史作业已经产生过场景版本。这两种都不能再改这份导入作业。 */
const sealed = computed(() => published.value || job.value?.status === "APPLIED");
const operationRunning = computed(() => isOperationActive(operation.value?.status) || watching.value);
/** 仅在持续查询到活跃解析操作时显示动画；查询出错后由页面展示真实错误。 */
const parsing = computed(() => watching.value && isOperationActive(operation.value?.status));

/** 已有解析结果的作业可以调整配置，但不重复调用模型解析。 */
const importMutable = computed(() => Boolean(job.value?.proposals) === false && !sealed.value);


/** 解析失败或成功后都允许再次提交；提交期间和查询期间不允许重复提交。 */
const canSubmitConversion = computed(() => importMutable.value && !pending.value && !operationRunning.value);

/**
 * 第一轮目录已保存且尚未形成最终结果：可以只重跑第二轮。
 * 场景已发布、已写入草稿或已有解析结果时不提供该选项。
 */
const canResumeRefinement = computed(
  () => Boolean(job.value?.can_resume_refinement) && importMutable.value,
);

const conversionActionLabel = computed(() => {
  if (parsing.value) return "正在解析";
  return canResumeRefinement.value ? "重试第二轮" : "开始解析";
});

const conversionDisabledReason = computed(() => {
  if (!job.value) {
    return "请先上传规则 Excel 文件。";
  }
  if (sealed.value) {
    return job.value.status === "APPLIED"
      ? "该导入作业已写入场景草稿，不能再改。请到场景配置页核对，或重新上传。"
      : "该导入作业已产生场景版本，不能再改。如需调整请重新上传。";
  }
  if (job.value.proposals) {
    return "该作业已形成转换结果，服务端不再接受重复解析。请重新上传文件开始新的导入。";
  }
  if (operationRunning.value) {
    return "已有解析操作正在执行，请等待本次操作结束后再提交。";
  }
  return null;
});

const steps = computed(() =>
  buildImportSteps({
    job: job.value,
    operation: operation.value,
    hasProposals: hasProposals.value,
    published: sealed.value,
  }),
);

/**
 * 刷新或切换作业后落在真实可操作的步骤上。
 * 已有转换结果时直接落在解析结果：作业完成后服务端不再接受重复解析。
 */
function inferStep(importJob: ImportJob): number {
  if (importJob.status === "PUBLISHED" || importJob.status === "APPLIED") {
    return 2;
  }
  return importJob.proposals ? 1 : 0;
}

/**
 * 接收一份作业视图并落到真实可操作的步骤上。
 */
async function receiveJob(importJob: ImportJob, keepStep = false): Promise<void> {
  const reception = ++jobReception;
  job.value = importJob;
  if (reception !== jobReception || job.value?.job_no !== importJob.job_no) return;
  if (!keepStep) {
    activeStep.value = inferStep(importJob);
  }
}

async function receiveProposalUpdate(updated: ImportJob): Promise<void> {
  if (job.value?.job_no === updated.job_no) await receiveJob(updated, true);
}

async function loadJob(jobNo: string): Promise<void> {
  const request = ++jobLoadRequest;
  loadingJob.value = true;
  try {
    await run(async () => {
      const importJob = await getImportJob(jobNo);
      if (request !== jobLoadRequest) return;
      await receiveJob(importJob);
    });
  } finally {
    if (request === jobLoadRequest) loadingJob.value = false;
  }
}

async function loadImportJobs(): Promise<void> {
  loadingJobs.value = true;
  try {
    await run(async () => {
      importJobs.value = await listImportJobs();
    });
  } finally {
    loadingJobs.value = false;
  }
}

async function loadTemplates(): Promise<void> {
  await run(async () => {
    templates.value = await listTemplates();
  });
}

/**
 * 读取后端上传策略。上传界面的大小上限与允许扩展名一律来自这里，
 * 组件不复制常量——复制的那份会在配置变更后与后端实际校验不一致。
 * 读取失败不阻断页面：策略为 null 时上传卡片不做前端预检，仍由服务端返回真实错误。
 */
async function loadUploadPolicy(): Promise<void> {
  try {
    uploadPolicy.value = await getUploadPolicy();
  } catch {
    uploadPolicy.value = null;
  }
}

async function chooseImportJob(importJob: ImportJobSummary): Promise<void> {
  await router.replace({ name: "scene-import-detail", params: { jobNo: importJob.job_no } });
}

/** 回到入口重新选择文件：清空当前作业的 URL 参数，由路由同步重置工作区。 */
async function startNewImport(): Promise<void> {
  const scenarioCode = job.value?.profile?.scenario_code;
  await router.replace({ name: "scene-imports", query: scenarioCode ? { scenario: scenarioCode } : {} });
}

function resetImportWorkspace(): void {
  ++jobLoadRequest;
  ++jobReception;
  loadingJob.value = false;
  // 完整重置操作查询：切作业后不再残留上一个作业的操作面板与轮询编号。
  resetTracking();
  job.value = null;
  importJobs.value = [];
  activeStep.value = 0;
  selectedFileSize.value = null;
  conversionAttemptKey = null;
}

/**
 * 追踪一次后台解析操作。
 * 只查询状态；结束后重新读取作业详情，页面显示的是服务端真实状态。
 */
function trackOperation(value: OperationView): void {
  const jobNo = job.value?.job_no;
  conversionAttemptKey = null;
  track(value, {
    onFinished: async (finished) => {
      if (jobNo && job.value?.job_no === jobNo) await refreshAfterOperation(finished, jobNo);
    },
    onError: (error) => {
      if (jobNo && job.value?.job_no === jobNo) setFailure(error);
    },
  });
}

async function refreshAfterOperation(finished: OperationView, jobNo: string): Promise<void> {
  if (job.value?.job_no !== jobNo) return;
  const reloaded = await getImportJob(jobNo);
  if (job.value?.job_no !== jobNo) return;
  await receiveJob(reloaded, true);
  if (job.value?.job_no !== jobNo) return;
  if (isOperationFinished(finished.status)) {
    await dropOperationQuery();
  }
  if (finished.status === "SUCCEEDED" && reloaded.proposals) {
    activeStep.value = 1;
  }
}

/** 操作结束后把 URL 里的 operation 参数去掉，避免刷新时重复查询已结束的操作。 */
async function dropOperationQuery(): Promise<void> {
  if (!job.value || typeof route.query.operation !== "string") {
    return;
  }
  await router.replace({ name: "scene-import-detail", params: { jobNo: job.value.job_no } });
}

/** 刷新后按 URL 里的 operation 参数恢复查询。 */
async function resumeOperation(operationNo: string): Promise<void> {
  const jobNo = job.value?.job_no;
  try {
    const current = await getOperation(operationNo);
    if (!jobNo || job.value?.job_no !== jobNo) return;
    if (isOperationFinished(current.status)) {
      operation.value = current;
      await dropOperationQuery();
      return;
    }
    trackOperation(current);
  } catch (error) {
    if (jobNo && job.value?.job_no === jobNo) setFailure(error);
  }
}

/** 刷新后作业详情里有正在进行/刚结束的操作时继续查询。 */
function resumeLatestOperation(): void {
  const latest = job.value?.latest_operation;
  if (latest && isOperationActive(latest.status)) {
    trackOperation(latest);
    return;
  }
  if (latest) {
    operation.value = latest;
  }
}

async function syncImportWorkspaceFromRoute(): Promise<void> {
  // 作业号只来自当前详情路由 /scene-imports/:jobNo。
  const jobNo = typeof route.params.jobNo === "string" ? route.params.jobNo : null;
  const operationNo = typeof route.query.operation === "string" ? route.query.operation : null;
  if (!jobNo) {
    resetImportWorkspace();
    await loadImportJobs();
    return;
  }
  if (jobNo !== job.value?.job_no) {
    resetImportWorkspace();
    await loadJob(jobNo);
    if (!job.value) {
      return;
    }
    if (operationNo) {
      await resumeOperation(operationNo);
    } else {
      resumeLatestOperation();
    }
    return;
  }
  if (operationNo && operationNo !== operation.value?.operation_no) {
    await resumeOperation(operationNo);
  }
}

async function uploadRulebook(file: File): Promise<void> {
  // 上传必须从已创建的场景进入：场景由用户先建好，不由 Excel 表头推断。
  if (!uploadScenarioCode.value) {
    formMessage.value = "请先选择本次导入所属的场景；上传后系统不会再从 Excel 内容猜场景。";
    return;
  }
  selectedFileSize.value = file.size;
  await run(async () => {
    const created = await createImportJob(file, uploadScenarioCode.value);
    await receiveJob(created);
    await router.replace({ name: "scene-import-detail", params: { jobNo: created.job_no } });
  });
}

/**
 * 提交后台解析。长处理只受理，随后查询真实状态。
 * 默认复用已保存的第一轮目录，只重跑第二轮；显式选择 FULL 时从头解析。
 */
async function submitConversion(mode?: "FULL" | "RESUME_REFINEMENT"): Promise<void> {
  if (!job.value) {
    return;
  }
  if (conversionDisabledReason.value) {
    formMessage.value = conversionDisabledReason.value;
    return;
  }
  const selected = mode ?? (canResumeRefinement.value ? "RESUME_REFINEMENT" : "FULL");
  const jobNo = job.value.job_no;
  await run(async () => {
    const attemptKey = conversionAttemptKey ?? operationIdempotencyKey("IMPORT_CONVERT", jobNo);
    conversionAttemptKey = attemptKey;
    try {
      const accepted = await startImportConversion(jobNo, attemptKey, selected);
      trackOperation(accepted);
      activeStep.value = 1;
      // 把操作编号写进 URL：刷新后仍能凭它继续查询同一个操作。
      await router.replace({ name: "scene-import-detail", params: { jobNo }, query: { operation: accepted.operation_no } });
    } catch (error) {
      setFailure(error);
      const code = error instanceof Error && "code" in error ? (error as ApiError).code : null;
      // 服务端明确返回错误码说明本次请求未被受理，幂等键只在结果未知（网络中断）时才保留；
      // 否则用户换一个模式提交时会用同一键发出不同内容，持续命中幂等冲突。
      if (code) conversionAttemptKey = null;
      if (code === "OPERATION_ALREADY_ACTIVE") {
        // 同一作业已有活跃操作时，明确切换到查询该操作的真实状态，不重复提交。
        const reloaded = await getImportJob(jobNo);
        await receiveJob(reloaded, true);
        resumeLatestOperation();
      } else if (code === "IMPORT_INITIAL_STAGE_UNAVAILABLE") {
        // 第一轮检查点已失效：刷新作业详情，页面按钮随之回到「开始解析」。
        const reloaded = await getImportJob(jobNo);
        await receiveJob(reloaded, true);
      }
    }
  });
}

/** 从头重新解析：两轮都重跑，已保存的第一轮目录会被新的结果覆盖。 */
async function reparseFromStart(): Promise<void> {
  try {
    await ElMessageBox.confirm(
      "将从第一轮重新理解整份 Excel，耗时更长；已保存的第一轮结果会被新的结果覆盖。确认从头解析？",
      "从头重新解析",
      { type: "warning", confirmButtonText: "从头解析", cancelButtonText: "取消" },
    );
  } catch {
    return;
  }
  await submitConversion("FULL");
}

/** 用导入结果替换 Excel 管理的草稿内容；流程节点保留，场景不会自动发布。 */
async function applyToSceneDraft(): Promise<void> {
  if (!job.value || sealed.value) {
    return;
  }
  const jobNo = job.value.job_no;
  const jobRevision = job.value.draft_revision;
  const scenarioCode = job.value.profile?.scenario_code;
  if (!scenarioCode) {
    formMessage.value = "导入作业没有所属场景，不能写入草稿。";
    return;
  }
  await run(async () => {
    const draft = await getSceneConfiguration(scenarioCode);
    if (job.value?.job_no !== jobNo || job.value.draft_revision !== jobRevision) {
      formMessage.value = "当前导入作业已变化，请重新核对后再写入。";
      return;
    }
    try {
      const hasExistingBusinessDraft = draft.configuration_kind === "DRAFT" && Boolean(
        draft.materials.length
        || draft.fields.length
        || draft.rules.some((rule) => !rule.system)
        || draft.calculations.length
        || draft.sections.length,
      );
      await ElMessageBox.confirm(
        h("div", [
          h("p", { style: { marginBottom: "12px" } },
            `本次将导入 ${proposalTotals.value.materials} 个材料位、${proposalTotals.value.fields} 个字段、${proposalTotals.value.rules} 条业务规则、${includedCalculations.value.length} 条计算规则、${proposalTotals.value.reportSections} 个报告章节。`),
          ...(hasExistingBusinessDraft
            ? [h("p", { style: { marginBottom: "12px" } },
                `当前草稿已有 ${draft.materials.length} 个材料位、${draft.fields.length} 个字段、${draft.rules.filter((rule) => !rule.system).length} 条业务规则、${draft.calculations.length} 条计算规则；其中的人工修改将被覆盖。`)]
            : []),
          h("p", { style: { marginBottom: "12px" } },
            "材料、字段、业务规则、计算配置、报告章节将被整份替换。执行按生效规则及其实际数据引用进行。"),
          h("p", "本次操作只生成草稿，不会发布场景，也不会影响当前已发布版本、已有方案和审核任务。"),
        ]),
        hasExistingBusinessDraft ? "覆盖现有草稿" : "生成场景草稿",
        {
          type: hasExistingBusinessDraft ? "warning" : "info",
          confirmButtonText: hasExistingBusinessDraft ? "覆盖现有草稿" : "生成场景草稿",
          cancelButtonText: "取消",
        },
      );
    } catch {
      return;
    }
    if (job.value?.job_no !== jobNo || job.value.draft_revision !== jobRevision) {
      formMessage.value = "当前导入作业已变化，请重新核对后再写入。";
      return;
    }
    await receiveJob(await applyImportToDraft(jobNo, jobRevision, draft.revision_no), true);
    ElMessage.success("已生成场景草稿，请核对并调整后再发布");
    await router.push({ name: "scene-detail", params: { scenarioCode }, query: { tab: "rules", importJob: jobNo } });
  });
}

onMounted(async () => {
  await loadUploadPolicy();
  await loadTemplates();
  await syncImportWorkspaceFromRoute();
});

watch(
  () => [route.params.jobNo, route.query.job],
  () => {
    void syncImportWorkspaceFromRoute();
  },
);

watch(
  () => route.query.operation,
  () => {
    void syncImportWorkspaceFromRoute();
  },
);

</script>

<template>
  <div class="import-workbench">
    <PageHeader
      :title="job ? '规则库导入工作台' : '规则库导入'"
      :object-name="job?.source_filename ?? null"
      :busy="pending || proposalSaving"
      @refresh="job ? loadJob(job.job_no) : loadImportJobs()"
    />

    <FailureNotice :error="failure" @dismiss="failure = null" />

    <!-- 未选择作业：上传新文件 + 继续已有作业 -->
    <template v-if="!job">
      <el-card shadow="never">
        <template #header>上传规则库文件</template>
        <!-- 上传以场景为起点：先选场景，再上传。系统不从 Excel 内容猜场景。 -->
        <el-form label-width="120px" class="block-gap-sm">
          <el-form-item label="所属场景" required>
            <el-select
              v-model="uploadScenarioCode"
              filterable
              placeholder="请选择本次导入所属的场景"
              style="width: 320px"
            >
              <el-option
                v-for="item in templates"
                :key="item.scenario_code"
                :value="item.scenario_code"
                :label="item.scenario_name"
                :disabled="!item.enabled"
              />
            </el-select>
          </el-form-item>
        </el-form>
        <FileUploadCard
          :job-filename="null"
          :job-created-at="null"
          :job-status="null"
          :selected-file-size="null"
          :policy="uploadPolicy?.rulebook ?? null"
          :pending="pending"
          :disabled="!uploadScenarioCode"
          @pick="uploadRulebook"
        />
      </el-card>

      <ImportJobPicker
        class="block-gap"
        :jobs="importJobs"
        :pending="pending"
        :loading="loadingJobs"
        @select="chooseImportJob"
        @refresh="loadImportJobs"
      />
    </template>

    <template v-else>
      <el-card shadow="never">
        <ImportStepNav :steps="steps" :active="activeStep" clickable @select="activeStep = $event" />
      </el-card>

      <div v-if="parsing" class="parsing-status block-gap" role="status" aria-live="polite">
        <span class="parsing-status__spinner" aria-hidden="true"></span>
        <span>正在解析规则库，完成后将显示解析结果。</span>
      </div>

      <!-- 只展示业务失败原因与下一步，不暴露内部操作状态。 -->
      <el-alert
        v-if="jobFailure"
        class="block-gap"
        type="error"
        show-icon
        :closable="false"
        :title="jobFailure.message"
      >
        <div>{{ failureNextStep(jobFailure.code) }}</div>
        <div v-if="canResumeRefinement">第一轮解析结果已保存，可只重试第二轮；如需重新理解整份 Excel，选择「从头重新解析」。</div>
      </el-alert>

      <!-- 第一步：上传文件 -->
      <el-card v-if="activeStep === 0" shadow="never" class="block-gap">
        <template #header>第一步 · 上传规则 Excel</template>
        <FileUploadCard
          :job-filename="job.source_filename"
          :job-created-at="job.created_at"
          :job-status="job.status"
          :selected-file-size="selectedFileSize"
          :policy="uploadPolicy?.rulebook ?? null"
          :pending="pending"
          :disabled="!importMutable"
          @pick="uploadRulebook"
        />
        <div class="sticky-actions">
          <div class="sticky-actions__bar">
            <div class="card-actions">
              <el-button @click="startNewImport">重新上传规则库</el-button>
              <el-button v-if="canResumeRefinement" :disabled="!canSubmitConversion" @click="reparseFromStart">从头重新解析</el-button>
              <el-button
                type="primary"
                :loading="pending || parsing"
                :disabled="!canSubmitConversion"
                :title="conversionDisabledReason ?? ''"
                @click="submitConversion()"
              >
                {{ conversionActionLabel }}
              </el-button>
            </div>
          </div>
        </div>
      </el-card>

      <!-- 第二步：核对与调整解析结果 -->
      <el-card v-else-if="activeStep === 1" shadow="never" class="block-gap parse-results-card">
        <template #header>
          <div class="card-header">
            <span>第二步 · 解析结果</span>
            <span class="parse-results-source">{{ job.source_filename }}</span>
          </div>
        </template>

        <StateBlock v-if="loadingJob" kind="loading" />
        <StateBlock v-else-if="operationRunning && !hasProposals" kind="loading" title="正在解析规则库" description="完成后将显示材料、字段、规则和报告配置。" />
        <ImportProposalEditor
          v-else-if="hasProposals"
          :job="job"
          :readonly="sealed || operationRunning"
          @saved="receiveProposalUpdate"
          @busy="proposalSaving = $event"
        />
        <StateBlock v-else kind="empty" title="还没有解析结果" description="回到上一步开始解析。" />

        <div class="sticky-actions">
          <div class="sticky-actions__bar">
            <div class="card-actions">
              <el-button :disabled="proposalSaving" @click="activeStep = 0">上一步</el-button>
              <el-button type="primary" :disabled="!hasProposals || proposalSaving" @click="activeStep = 2">下一步：写入草稿</el-button>
            </div>
          </div>
        </div>
      </el-card>

      <!-- 第三步：确认写入草稿。详细核对与调整在场景配置页完成。 -->
      <el-card v-else-if="activeStep === 2" shadow="never" class="block-gap">
        <template #header>
          <div class="card-header">
            <span>第三步 · 写入场景草稿</span>
            <StatusBadge :status="job.status === 'APPLIED' ? 'APPLIED' : 'READY'" />
          </div>
        </template>

        <el-alert v-if="job.status === 'APPLIED'" type="success" show-icon :closable="false" title="已生成场景草稿">
          本次导入没有发布场景版本。请进入场景配置核对和调整，完成后由管理员手动发布。
        </el-alert>

        <template v-else>
          <el-descriptions :column="2" border>
            <el-descriptions-item label="目标场景" :span="2">
              {{ job.profile?.scenario_name || "未记录场景名称" }}
            </el-descriptions-item>
            <el-descriptions-item label="材料">{{ proposalTotals.materials }}</el-descriptions-item>
            <el-descriptions-item label="字段">{{ proposalTotals.fields }}</el-descriptions-item>
            <el-descriptions-item label="业务规则">{{ proposalTotals.rules }}</el-descriptions-item>
            <el-descriptions-item label="计算规则">{{ includedCalculations.length }}</el-descriptions-item>
            <el-descriptions-item label="报告章节">{{ proposalTotals.reportSections }}</el-descriptions-item>
          </el-descriptions>

          <div class="draft-write-scope block-gap">
            <section>
              <strong>本次替换</strong>
              <span>材料、字段、业务规则、计算配置和报告章节</span>
            </section>
            <section>
              <strong>不会影响</strong>
              <span>当前已发布版本、已有方案和审核任务</span>
            </section>
          </div>

          <el-alert class="block-gap" type="info" show-icon :closable="false" title="本次操作只生成草稿，不会自动发布">
            写入后将在场景配置页继续核对和调整；只有管理员手动发布后，新配置才会生效。
          </el-alert>
        </template>

        <div class="sticky-actions">
          <div class="sticky-actions__bar">
            <div class="card-actions">
              <el-button @click="activeStep = 1">上一步</el-button>
              <el-button
                v-if="job.status !== 'APPLIED'"
                type="primary"
                :loading="pending"
                :disabled="!hasProposals"
                @click="applyToSceneDraft"
              >
                写入场景草稿
              </el-button>
              <el-button
                v-else-if="job.profile?.scenario_code"
                type="primary"
                @click="router.push({ name: 'scene-detail', params: { scenarioCode: job.profile.scenario_code }, query: { tab: 'materials' } })"
              >
                打开场景配置
              </el-button>
            </div>
          </div>
        </div>
      </el-card>
    </template>

  </div>
</template>

<style scoped>
.sticky-actions__bar {
  justify-content: flex-end;
}

.parse-results-source {
  max-width: 50%;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 13px;
  font-weight: 400;
  color: var(--el-text-color-secondary);
}


.parsing-status {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 14px 16px;
  border: 1px solid var(--el-color-primary-light-7);
  border-radius: 6px;
  background: var(--el-color-primary-light-9);
  color: var(--el-text-color-primary);
  font-size: 14px;
}

.parsing-status__spinner {
  flex: 0 0 auto;
  width: 18px;
  height: 18px;
  border: 2px solid var(--el-color-primary-light-7);
  border-top-color: var(--el-color-primary);
  border-radius: 50%;
  animation: parsing-spin 0.9s linear infinite;
}

@keyframes parsing-spin {
  to {
    transform: rotate(360deg);
  }
}

@media (prefers-reduced-motion: reduce) {
  .parsing-status__spinner {
    animation: none;
  }
}

.draft-write-scope {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  border: 1px solid var(--el-border-color-light);
  border-radius: 4px;
  background: #fff;
}

.draft-write-scope section {
  padding: 14px 16px;
  border-right: 1px solid var(--el-border-color);
}

.draft-write-scope section:last-child {
  border-right: 0;
}

.draft-write-scope strong,
.draft-write-scope span {
  display: block;
}

.draft-write-scope strong {
  margin-bottom: 5px;
  font-size: 13px;
}

.draft-write-scope span {
  color: var(--el-text-color-secondary);
  font-size: 13px;
  line-height: 1.6;
}

@media (max-width: 820px) {
  .draft-write-scope {
    grid-template-columns: 1fr;
  }

  .draft-write-scope section {
    border-right: 0;
    border-bottom: 1px solid var(--el-border-color);
  }

  .draft-write-scope section:last-child {
    border-bottom: 0;
  }
}
</style>
