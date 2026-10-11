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
  updateSheetBindings,
} from "@/api/imports";
import type {
  ImportJob,
  ImportJobSummary,
  ImportSheet,
  JsonValue,
  ProposalCalculation,
  ProposalRule,
  ProposalSheet,
  UploadPolicy,
} from "@/api/contracts";
import type { ConfigOrigin } from "@/api/contracts/scenes";
import CandidateTable, { type CandidateColumn, type CandidateRow } from "@/components/import/CandidateTable.vue";
import FileUploadCard from "@/components/import/FileUploadCard.vue";
import ImportJobPicker from "@/components/import/ImportJobPicker.vue";
import ImportStepNav from "@/components/import/ImportStepNav.vue";
import SheetBindingTable from "@/components/import/SheetBindingTable.vue";
import FailureNotice from "@/components/FailureNotice.vue";
import PageHeader from "@/components/PageHeader.vue";
import StateBlock from "@/components/StateBlock.vue";
import StatGrid, { type StatItem } from "@/components/StatGrid.vue";
import StatusBadge from "@/components/StatusBadge.vue";
import { buildImportSteps, importBlockingIssues } from "@/constants/importSteps";
import { useOperationTracker } from "@/composables/useOperationTracker";
import { useRequestRunner } from "@/composables/useRequestRunner";
import { getUploadPolicy } from "@/api/system";
import { getSceneDraft } from "@/api/scenes";
import {
  applicabilityLabel,
  failureNextStep,
  formatBusinessValue,
  riskSourceRuleColumnLabel,
  valueTypeLabel,
} from "@/constants/labels";
import { initialBindingOf, type SheetBindingDraft } from "@/constants/sheetBinding";

const route = useRoute();
const router = useRouter();
const { operation, watching, track, reset: resetTracking } = useOperationTracker();
const { pending, failure, formMessage, run, setFailure } = useRequestRunner();

type CandidateTab = "material" | "field" | "rule" | "calculation";

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
const bindings = ref<Record<string, SheetBindingDraft>>({});
const activeStep = ref(0);
const candidateTab = ref<CandidateTab>("material");
/** 首次进入时的加载状态：与「还没有数据」分开，避免把请求失败显示成空。 */
const loadingJob = ref(false);
const loadingJobs = ref(false);
/** 本次浏览器选择的文件大小；刷新后浏览器不再保留该信息。 */
const selectedFileSize = ref<number | null>(null);
/** 尚未确认受理的幂等标识：同一次业务动作再次提交时复用它，不会产生第二次执行。 */
let conversionAttemptKey: string | null = null;
const candidateTabModel = computed<CandidateTab>({
  get: () => candidateTab.value,
  set: (value) => {
    candidateTab.value = value === "field" || value === "rule" || value === "calculation" ? value : "material";
  },
});

let jobReception = 0;
let jobLoadRequest = 0;

const proposalSheets = computed<ProposalSheet[]>(() => job.value?.proposals?.sheets ?? []);
const proposalCalculations = computed<ProposalCalculation[]>(() => job.value?.proposals?.calculations ?? []);
const rejectedCandidates = computed(() => (job.value?.coverage?.rejected_candidates ?? []).map((item) => ({
  ...item,
  sourceSheet: job.value?.sheets.find((sheet) => sheet.sheet_key === item.source?.sheet_key)?.name,
})));
const hasProposals = computed(() => proposalSheets.value.length > 0 || proposalCalculations.value.length > 0);
const blockingIssueCount = computed(() => importBlockingIssues(job.value));

/**
 * 可选择参与解析的工作表；服务端仅将空表标记为 fixed_excluded。
 * 说明页是否参与由实际选择决定，表内说明行由模型声明处置。
 */
const bindingSheets = computed<ImportSheet[]>(() => (job.value?.sheets ?? []).filter((sheet) => !isSystemExcludedSheet(sheet)));
/** 统计本次解析接收的候选。 */
const proposalTotals = computed(() =>
  proposalSheets.value.reduce(
    (totals, sheet) => ({
      materials: totals.materials + sheet.materials.length,
      fields: totals.fields + sheet.fields.length,
      rules: totals.rules + sheet.rules.length,
      reportSections: totals.reportSections + sheet.report_sections.length,
    }),
    { materials: 0, fields: 0, rules: 0, reportSections: 0 },
  ),
);
const includedCalculations = computed(() => proposalCalculations.value);
const confirmedCandidateCount = computed(() => proposalTotals.value.materials + proposalTotals.value.fields
  + proposalTotals.value.rules + proposalCalculations.value.length);
const excludedSheetNames = computed(() =>
  (job.value?.sheets ?? [])
    .filter((sheet) => isSystemExcludedSheet(sheet) || bindingFor(sheet).excluded)
    .map((sheet) => sheet.name),
);
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

/** 已经形成转换结果或已发布的作业，服务端不再接受改动绑定与重复解析。 */
const importMutable = computed(() => Boolean(job.value?.proposals) === false && !sealed.value);


/** 解析失败或成功后都允许再次提交；提交期间和查询期间不允许重复提交。 */
const canSubmitConversion = computed(() => importMutable.value && !pending.value && !operationRunning.value);

const bindingBlocked = computed(() => Boolean(job.value) && bindingSheets.value.every((sheet) => bindingFor(sheet).excluded));
const bindingBlockedReason = computed(() => bindingBlocked.value ? "请至少选择一张参与解析的工作表。" : null);

/**
 * 当前绑定相对初值是否有未提交的改动。
 *
 * 比对的基线必须是绑定初值（`bindingDefaultsFor`），不是服务端原始字段：
 * 用途与表头行现在有默认值，拿原始字段当基线会让页面一打开就谎报「有未保存的改动」。
 */
const hasUnsavedBindings = computed(() =>
  bindingSheets.value.some((sheet) => {
    const draft = bindingFor(sheet);
    const saved = bindingDefaultsFor(sheet);
    return (
      draft.excluded !== saved.excluded
    );
  }),
);

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
    return "该作业已形成转换结果，服务端不再接受改动工作表绑定。请重新上传文件开始新的导入。";
  }
  if (bindingBlockedReason.value) {
    return bindingBlockedReason.value;
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
    blockingIssues: blockingIssueCount.value,
    published: sealed.value,
  }),
);

const parsingStats = computed<StatItem[]>(() => [
  { label: "未导入项目", value: rejectedCandidates.value.length, tone: rejectedCandidates.value.length ? "warning" : "success" },
  { label: "已接收候选", value: confirmedCandidateCount.value },
]);

// 候选清单
const fieldColumns: CandidateColumn[] = [
  { key: "name", label: "字段名称", primary: true, minWidth: 200 },
  { key: "type", label: "字段类型", width: 110 },
  { key: "material", label: "来源材料", minWidth: 140 },
  { key: "instruction", label: "抽取要求", minWidth: 180 },
];

const materialColumns: CandidateColumn[] = [
  { key: "name", label: "材料名称", primary: true, minWidth: 200 },
  { key: "required", label: "是否必传", width: 100 },
  { key: "description", label: "材料说明", minWidth: 220 },
];

/** 风险按规则能力识别，节点实例码由管理员配置。 */
function isRiskRule(rule: ProposalRule): boolean {
  return rule.operator === "RISK_TRIGGER";
}

/** 本次转换结果里是否存在风险疑点规则：只有存在时才展示来源节点与来源审核规则两列。 */
const hasRiskRules = computed(() => proposalSheets.value.some((sheet) => sheet.rules.some(isRiskRule)));

const ruleColumns = computed<CandidateColumn[]>(() => {
  const columns: CandidateColumn[] = [
    { key: "name", label: "规则名称", primary: true, minWidth: 200 },
  ];
  if (hasRiskRules.value) {
    columns.push({ key: "source_rule", label: riskSourceRuleColumnLabel, minWidth: 180 });
  }
  columns.push({ key: "method", label: "判断方式", minWidth: 220 });
  columns.push({ key: "requirement", label: "审核要求", minWidth: 200 });
  return columns;
});

const calculationColumns: CandidateColumn[] = [
  { key: "name", label: "计算规则", primary: true, minWidth: 190 },
  { key: "formula", label: "计算方法", minWidth: 220 },
  { key: "parameters", label: "参数来源", minWidth: 260 },
  { key: "requirement", label: "业务要求", minWidth: 220 },
  { key: "status", label: "配置状态", width: 110 },
];

function calculationParameterText(parameter: Record<string, JsonValue>): string {
  const code = String(parameter.parameter_code ?? "未记录编号");
  const sourceType = String(parameter.source_type ?? "");
  const sourceLabels: Record<string, string> = { FIELD: "字段", CALC_RESULT: "上游计算结果", CONSTANT: "常量" };
  const sourceLabel = sourceLabels[sourceType] ?? sourceType;
  const source = sourceType === "CONSTANT"
    ? formatBusinessValue(parameter.constant_value ?? null)
    : String(parameter.source_code ?? "未记录来源编号");
  return `${code} ← ${sourceLabel} ${source}`;
}

function calculationStatusLabel(status: string): string {
  const statusLabels: Record<string, string> = { CLEAR: "已明确", PENDING: "待确认", DISABLED: "停用" };
  return statusLabels[status] ?? status;
}

function sourceText(source: { source_sheet: string; source_row: number; source_cells: string[] }): string {
  const cells = source.source_cells?.length ? source.source_cells.join("、") : "未标注单元格";
  return `${source.source_sheet} 第 ${source.source_row} 行 · ${cells}`;
}

function calculationOriginText(origin: ConfigOrigin | null | undefined): string {
  if (!origin) return "未记录原表来源";
  const cells = origin.cells.map((address) => `${address}=${origin.texts[address] ?? "未记录原文"}`);
  return `${origin.sheet_name} 第 ${origin.row_no} 行 · ${cells.join("；") || "未记录单元格"}`;
}

/** 原始识别内容：让用户看到人工修改前的值，而不是只有改后的结果。 */
function originalSummary(original: Record<string, JsonValue> | null): string {
  if (!original) {
    return "没有记录原始识别内容";
  }
  const entries = Object.entries(original)
    .filter(([, value]) => value !== null && value !== undefined && value !== "")
    .map(([key, value]) => `${key}：${formatBusinessValue(value)}`);
  return entries.length ? entries.join("；") : "原始识别内容为空";
}

const fieldRows = computed<CandidateRow[]>(() =>
  proposalSheets.value.flatMap((sheet) =>
    sheet.fields.map((field) => ({
      ref: field.candidate_ref,
      name: field.name,
      values: [
        field.name,
        `${valueTypeLabel(field.value_type)}${field.unit ? `（${field.unit}）` : ""}`
          + `${field.currency ? ` / ${field.currency}` : ""}`,
        field.material_name ?? "未关联材料",
        field.instruction || "未填写抽取要求",
      ],
      source: sourceText(field),
      details: [
        { label: "候选标识", value: field.candidate_ref },
        { label: "字段类型编码", value: field.value_type ?? "未声明" },
        { label: "币种", value: field.currency ?? "未声明" },
        { label: "原始识别内容", value: originalSummary(field.original) },
      ],
    })),
  ),
);

const materialRows = computed<CandidateRow[]>(() =>
  proposalSheets.value.flatMap((sheet) =>
    sheet.materials.map((material) => ({
      ref: material.candidate_ref,
      name: material.name,
      values: [material.name, material.required ? "必传" : "按需上传", material.description || "未填写材料说明"],
      source: sourceText(material),
      details: [
        { label: "候选标识", value: material.candidate_ref },
        { label: "原始识别内容", value: originalSummary(material.original) },
      ],
    })),
  ),
);

const ruleRows = computed<CandidateRow[]>(() =>
  proposalSheets.value.flatMap((sheet) =>
    sheet.rules.map((rule) => {
      const risk = isRiskRule(rule);
      const values = [
        `${rule.name}${rule.config_status === "DISABLED" ? "（停用）" : ""}`,
      ];
      if (hasRiskRules.value) {
        // 风险规则展示其真实依赖的来源规则。
        values.push(risk ? riskSourceRuleText(rule) : "");
      }
      values.push(ruleComparisonText(rule), rule.comparison.requirement || "未填写审核要求");
      return {
        ref: rule.candidate_ref,
        name: rule.name,
        values,
        source: sourceText(rule),
        details: [
          { label: "候选标识", value: rule.candidate_ref },
          { label: "业务动作", value: rule.operator },
          { label: "配置状态", value: rule.config_status ? calculationStatusLabel(rule.config_status) : "未记录" },
          { label: "适用条件", value: ruleApplicability(rule) },
          { label: "原始识别内容", value: originalSummary(rule.original) },
        ],
      };
    }),
  ),
);

const calculationRows = computed<CandidateRow[]>(() =>
  proposalCalculations.value.map((item) => ({
    ref: item.candidate_ref,
    name: item.rule_name,
    values: [
      item.rule_name,
      item.formula,
      item.parameters.map(calculationParameterText).join("；") || "无参数声明",
      item.requirement,
      calculationStatusLabel(item.status),
    ],
    source: sourceText(item),
    details: [
      { label: "规则编号", value: item.rule_code },
      { label: "结果编号 / 类型", value: `${item.result_code} / ${item.result_type}` },
      { label: "业务补充", value: JSON.stringify(item.business_context) },
      { label: "计算定义原表", value: calculationOriginText(item.origin) },
      ...item.parameter_sources.map((parameter) => ({
        label: `参数 ${parameter.code || "未填写编号"} 原表`, value: calculationOriginText(parameter.origin),
      })),
    ],
  })),
);



/** 风险的全部来源审核规则，顺序与服务端候选引用一致。 */
function riskSourceRuleText(rule: ProposalRule): string {
  return [
    ...rule.comparison.source_rule_names,
    ...rule.comparison.calculation_sources.map((source) => `计算：${source.rule_name ?? "未找到来源计算规则"}`),
  ].join("、");
}

/** 规则的比较对象用业务语言描述。 */
function ruleComparisonText(rule: ProposalRule): string {
  const comparison = rule.comparison;
  if (!comparison.left && !comparison.right && !isRiskRule(rule)) {
    return comparison.operator_label || rule.operator;
  }
  if (isRiskRule(rule)) {
    return `${riskSourceRuleText(rule) || "未声明来源审核规则"}：${comparison.risk_condition_label ?? "未声明触发条件"}`;
  }
  const left = describeTarget(comparison.left) || "未指定";
  const right = comparison.right ? describeTarget(comparison.right) : "无（单字段条款审核）";
  const operator = comparison.operator_label ?? "比较";
  const label = comparison.comparison_label ? `（${comparison.comparison_label}）` : "";
  return `${left} ${operator} ${right}${label}`;
}

function ruleApplicability(rule: ProposalRule): string {
  return rule.comparison.applicability_label ?? applicabilityLabel(rule.comparison.applicability?.kind);
}

function describeTarget(target: { material_name: string | null; field_name: string | null } | null): string {
  if (!target) {
    return "";
  }
  return [target.material_name, target.field_name].filter(Boolean).join(" · ");
}


/**
 * 系统固定排除的工作表。
 *
 * fixed_excluded 表示服务端判定的空表，前端不按名称或隐藏状态排除工作表。
 */
function isSystemExcludedSheet(sheet: ImportSheet): boolean {
  return sheet.fixed_excluded;
}

/** 当前工作表选择值；显示和未提交改动比较共用初值。 */
function bindingFor(sheet: ImportSheet): SheetBindingDraft {
  return bindings.value[sheet.sheet_key] ?? bindingDefaultsFor(sheet);
}

/** 工作表选择初值由 `constants/sheetBinding.initialBindingOf` 统一提供。 */
function bindingDefaultsFor(sheet: ImportSheet): SheetBindingDraft {
  return initialBindingOf(
    sheet,
    isSystemExcludedSheet(sheet),
  );
}

function updateBinding(payload: { sheetKey: string; draft: SheetBindingDraft }): void {
  bindings.value = { ...bindings.value, [payload.sheetKey]: payload.draft };
}

/**
 * 恢复每张表是否参与解析；用途、节点和表头不设置表级绑定。
 */
function initialiseBindings(importJob: ImportJob): void {
  bindings.value = Object.fromEntries(
    importJob.sheets.map(
      (sheet): [string, SheetBindingDraft] => [
        sheet.sheet_key,
        initialBindingOf(sheet, isSystemExcludedSheet(sheet)),
      ],
    ),
  );
}

/**
 * 刷新或切换作业后落在真实可操作的步骤上。
 * 已有转换结果时不再落回工作表选择：作业完成转换后，服务端不再接受改动解析范围。
 */
function inferStep(importJob: ImportJob): number {
  if (importJob.status === "PUBLISHED" || importJob.status === "APPLIED") {
    return 3;
  }
  if (!importJob.proposals) {
    return importJob.latest_operation && isOperationActive(importJob.latest_operation.status) ? 1 : 0;
  }
  return 3;
}

/**
 * 接收一份作业视图并落到真实可操作的步骤上。
 *
 * 恢复当前工作表选择并展示本次解析结果。
 */
async function receiveJob(importJob: ImportJob, keepStep = false): Promise<void> {
  const reception = ++jobReception;
  job.value = importJob;
  if (reception !== jobReception || job.value?.job_no !== importJob.job_no) return;
  initialiseBindings(importJob);
  if (!keepStep) {
    activeStep.value = inferStep(importJob);
  }
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

/**
 * 重新加载已保存的绑定：只从服务端读取当前作业，不做第二次解析。
 * 有未保存改动时先确认，避免误丢本地编辑。
 */
async function reloadSavedBindings(): Promise<void> {
  if (!job.value) {
    return;
  }
  if (hasUnsavedBindings.value && !window.confirm("当前绑定有未保存的改动，重新加载会丢弃这些改动。是否继续？")) {
    return;
  }
  await loadJob(job.value.job_no);
}

async function chooseImportJob(importJob: ImportJobSummary): Promise<void> {
  await router.replace({ name: "scene-import-detail", params: { jobNo: importJob.job_no } });
}

/** 回到入口重新选择文件：清空当前作业的 URL 参数，由路由同步重置工作区。 */
async function startNewImport(): Promise<void> {
  const scenarioCode = job.value?.profile?.scenario_code;
  await router.replace({ name: "scene-imports", query: scenarioCode ? { scenario: scenarioCode } : {} });
}

function openSceneConfiguration(): void {
  const scenarioCode = job.value?.profile?.scenario_code;
  const jobNo = job.value?.job_no;
  if (!scenarioCode || !jobNo) return;
  void router.push({
    name: "scene-detail",
    params: { scenarioCode },
    query: { tab: "source", importJob: jobNo },
  });
}

function resetImportWorkspace(): void {
  ++jobLoadRequest;
  ++jobReception;
  loadingJob.value = false;
  // 完整重置操作查询：切作业后不再残留上一个作业的操作面板与轮询编号。
  resetTracking();
  job.value = null;
  importJobs.value = [];
  bindings.value = {};
  activeStep.value = 0;
  candidateTab.value = "material";
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
    activeStep.value = 3;
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
  // 作业号来自路由参数 /scene-imports/:jobNo；兼容旧的 ?job= 链接。
  const jobNo =
    typeof route.params.jobNo === "string" && route.params.jobNo
      ? route.params.jobNo
      : typeof route.query.job === "string"
        ? route.query.job
        : null;
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

/** 保存参与解析的工作表并提交后台解析。长处理只受理，随后查询真实状态。 */
async function submitConversion(saveBindings: boolean): Promise<void> {
  if (!job.value) {
    return;
  }
  if (conversionDisabledReason.value) {
    formMessage.value = conversionDisabledReason.value;
    return;
  }
  const jobNo = job.value.job_no;
  await run(async () => {
    if (saveBindings) {
      const updated = await updateSheetBindings(
        jobNo,
        job.value!.sheets.map((sheet) => {
          const binding = bindingFor(sheet);
          const systemExcluded = isSystemExcludedSheet(sheet);
          return {
            sheet_key: sheet.sheet_key,
            excluded: systemExcluded || binding.excluded,
          };
        }),
      );
      await receiveJob(updated, true);
    }
    const attemptKey = conversionAttemptKey ?? operationIdempotencyKey("IMPORT_CONVERT", jobNo);
    conversionAttemptKey = attemptKey;
    try {
      const accepted = await startImportConversion(jobNo, attemptKey);
      trackOperation(accepted);
      activeStep.value = 1;
      // 把操作编号写进 URL：刷新后仍能凭它继续查询同一个操作。
      await router.replace({ name: "scene-import-detail", params: { jobNo }, query: { operation: accepted.operation_no } });
    } catch (error) {
      setFailure(error);
      // 同一作业已有活跃操作时，明确切换到查询该操作的真实状态，不重复提交。
      if (error instanceof Error && "code" in error && (error as ApiError).code === "OPERATION_ALREADY_ACTIVE") {
        const reloaded = await getImportJob(jobNo);
        await receiveJob(reloaded, true);
        resumeLatestOperation();
      }
    }
  });
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
    const draft = await getSceneDraft(scenarioCode);
    if (job.value?.job_no !== jobNo || job.value.draft_revision !== jobRevision) {
      formMessage.value = "当前导入作业已变化，请重新核对后再写入。";
      return;
    }
    try {
      const hasExistingBusinessDraft = Boolean(
        draft.sheets.length
        || draft.materials.length
        || draft.fields.length
        || draft.rules.some((rule) => !rule.system)
        || draft.calculations.length
        || draft.sections.length,
      );
      await ElMessageBox.confirm(
        h("div", [
          h("p", { style: { marginBottom: "12px" } },
            `本次将导入 ${proposalTotals.value.materials} 个材料位、${proposalTotals.value.fields} 个字段、${proposalTotals.value.rules} 条业务规则、${includedCalculations.value.length} 条计算规则。`),
          ...(rejectedCandidates.value.length
            ? [h("p", { style: { marginBottom: "12px", color: "var(--el-color-warning)" } },
                `本次解析有 ${rejectedCandidates.value.length} 个项目未导入，草稿只包含上列候选。未导入项目及其原因保留在解析结果中。`)]
            : []),
          ...(hasExistingBusinessDraft
            ? [h("p", { style: { marginBottom: "12px" } },
                `当前草稿已有 ${draft.materials.length} 个材料位、${draft.fields.length} 个字段、${draft.rules.filter((rule) => !rule.system).length} 条业务规则、${draft.calculations.length} 条计算规则；其中的人工修改将被覆盖。`)]
            : []),
          h("p", { style: { marginBottom: "12px" } },
            "材料、字段、业务规则、计算配置、报告章节和来源工作表清单将被整份替换。执行按生效规则及其实际数据引用进行。"),
          h("p", "本次操作只生成草稿，不会发布场景，也不会影响当前已发布版本、已有方案和审核任务。"),
        ]),
        hasExistingBusinessDraft ? "覆盖现有草稿" : "生成场景草稿",
        {
          type: hasExistingBusinessDraft || rejectedCandidates.value.length ? "warning" : "info",
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
    await router.push({ name: "scene-detail", params: { scenarioCode }, query: { tab: "source", importJob: jobNo } });
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
      :busy="pending"
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
            <span class="text-hint" style="margin-left: 8px">
              导入作业会明确属于该场景；未启用的场景不可选。
            </span>
          </el-form-item>
        </el-form>
        <FileUploadCard
          :job-filename="null"
          :job-created-at="null"
          :job-status="null"
          :failure-message="null"
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
      </el-alert>

      <!-- 第一步：上传文件 -->
      <el-card v-if="activeStep === 0" shadow="never" class="block-gap">
        <template #header>第一步 · 上传规则 Excel</template>
        <FileUploadCard
          :job-filename="job.source_filename"
          :job-created-at="job.created_at"
          :job-status="job.status"
          :failure-message="job.failure?.message ?? null"
          :selected-file-size="selectedFileSize"
          :policy="uploadPolicy?.rulebook ?? null"
          :pending="pending"
          :disabled="!importMutable"
          @pick="uploadRulebook"
        />
        <div class="sticky-actions">
          <div class="sticky-actions__bar">
            <span class="text-muted">上传成功后选择需要解析的工作表。</span>
            <div class="card-actions">
              <el-button @click="startNewImport">重新上传规则库</el-button>
              <el-button type="primary" @click="activeStep = 1">下一步：选择工作表</el-button>
            </div>
          </div>
        </div>
      </el-card>

      <!-- 第二步：选择参与解析的工作表 -->
      <el-card v-else-if="activeStep === 1" shadow="never" class="block-gap">
        <template #header>第二步 · 选择参与解析的工作表</template>

        <SheetBindingTable
          class="block-gap"
          :sheets="bindingSheets"
          :drafts="bindings"
          @update:draft="updateBinding"
        />

        <el-alert
          v-if="bindingBlocked"
          class="block-gap"
          type="warning"
          show-icon
          :closable="false"
          :title="bindingBlockedReason ?? ''"
        />

        <div class="sticky-actions">
          <div class="sticky-actions__bar binding-actions">
            <div class="card-actions">
              <el-button @click="activeStep = 0">上一步</el-button>
              <el-button :disabled="!job.profile?.scenario_code" @click="openSceneConfiguration">查看场景配置</el-button>
              <el-button :loading="loadingJob" @click="reloadSavedBindings">重新加载已保存选择</el-button>
              <el-button
                type="primary"
                :loading="pending || parsing"
                :disabled="!canSubmitConversion"
                :title="conversionDisabledReason ?? ''"
                @click="submitConversion(true)"
              >
                {{ parsing ? "正在解析" : "保存并开始解析" }}
              </el-button>
            </div>
          </div>
        </div>
      </el-card>

      <!-- 第三步：字段规则确认 -->
      <el-card v-else-if="activeStep === 2" shadow="never" class="block-gap">
        <template #header>
          <div class="card-header">
            <span>第三步 · 解析结果</span>
            <StatusBadge status="READY" label="解析结果" />
          </div>
        </template>

        <p class="text-muted">
          按规则库原文生成配置，直接执行生效规则；停用规则仅保留原文。
          无法解析或执行的项目逐条记录原因，不影响其他规则。
        </p>

        <StatGrid class="block-gap" :items="parsingStats" />

        <el-card v-if="rejectedCandidates.length" shadow="never" class="block-gap">
          <template #header>未导入项目（{{ rejectedCandidates.length }} 项）</template>
          <p class="text-muted">以下项目未形成有效配置，原始模型内容和失败原因已保留；其他候选可继续写入草稿。</p>
          <el-table :data="rejectedCandidates" border>
            <el-table-column type="expand">
              <template #default="{ row }">
                <pre class="rejected-json">{{ JSON.stringify(row.raw, null, 2) }}</pre>
              </template>
            </el-table-column>
            <el-table-column label="项目" min-width="180">
              <template #default="{ row }">{{ row.name || "未提供名称" }}</template>
            </el-table-column>
            <el-table-column label="模型声明的来源" min-width="180">
              <template #default="{ row }">{{ row.sourceSheet || "未提供有效工作表" }}<template v-if="row.source?.row_no"> · 第 {{ row.source.row_no }} 行</template></template>
            </el-table-column>
            <el-table-column label="失败原因" prop="message" min-width="360" />
          </el-table>
        </el-card>

        <StateBlock
          v-if="loadingJob"
          kind="loading"
        />
        <StateBlock
          v-else-if="operationRunning && !hasProposals"
          kind="loading"
          title="正在解析规则库"
          description="完成后将显示解析得到的材料、字段和规则。"
        />
        <template v-else-if="hasProposals">
          <el-card shadow="never" class="block-gap">
            <template #header>有效配置</template>
            <el-tabs v-model="candidateTabModel">
              <el-tab-pane name="material">
                <template #label>材料（{{ materialRows.length }}）</template>
                <CandidateTable
                  :columns="materialColumns"
                  :rows="materialRows"
                  empty-title="转换结果中没有材料候选"
                  empty-description="当前工作表没有定义材料位。可查看原文与解析结果核对是否包含材料定义。"
                />
              </el-tab-pane>
              <el-tab-pane name="field">
                <template #label>字段（{{ fieldRows.length }}）</template>
                <CandidateTable
                  :columns="fieldColumns"
                  :rows="fieldRows"
                  empty-title="转换结果中没有字段候选"
                  empty-description="当前工作表的转换结果只有材料或规则，没有可纳入的字段。可查看原文核对是否包含字段抽取要求。"
                />
              </el-tab-pane>
              <el-tab-pane name="rule">
                <template #label>规则（{{ ruleRows.length }}）</template>
                <CandidateTable
                  :columns="ruleColumns"
                  :rows="ruleRows"
                  empty-title="转换结果中没有规则候选"
                  empty-description="当前工作表没有可执行的检查规则。只写入材料清单和字段也可以，但审核任务不会有自动判断结果。"
                />
              </el-tab-pane>
              <el-tab-pane v-if="proposalCalculations.length" name="calculation">
                <template #label>计算规则（{{ calculationRows.length }}）</template>
                <CandidateTable
                  :columns="calculationColumns"
                  :rows="calculationRows"
                  empty-title="转换结果中没有计算规则"
                  empty-description="当前规则库没有计算规则候选。"
                />
              </el-tab-pane>
            </el-tabs>
          </el-card>

          <p class="text-hint block-gap">
            已接收候选共 {{ confirmedCandidateCount }} 项。
            保留规则原文，不要求业务口径确认。
          </p>
        </template>
        <StateBlock
          v-else
          kind="empty"
          title="还没有解析结果"
          description="回到上一步选择工作表并提交解析，完成后这里会显示材料、字段和规则候选。"
        />

        <div class="sticky-actions">
          <div class="sticky-actions__bar">
            <span class="text-muted">
              可以直接写入场景草稿。
            </span>
            <div class="card-actions">
              <el-button @click="activeStep = 1">上一步</el-button>
              <el-button type="primary" :disabled="!hasProposals" @click="activeStep = 3">
                下一步：写入草稿
              </el-button>
            </div>
          </div>
        </div>
      </el-card>

      <!-- 第四步：确认写入草稿。详细核对与调整在场景配置页完成。 -->
      <el-card v-else-if="activeStep === 3" shadow="never" class="block-gap">
        <template #header>
          <div class="card-header">
            <span>第四步 · 写入场景草稿</span>
            <StatusBadge :status="job.status === 'APPLIED' ? 'APPLIED' : 'READY'" />
          </div>
        </template>

        <el-alert v-if="job.status === 'APPLIED'" type="success" show-icon :closable="false" title="已生成场景草稿">
          本次导入没有发布场景版本。请进入场景配置核对和调整，完成后由管理员手动发布。
        </el-alert>

        <template v-else>
          <el-descriptions :column="2" border>
            <el-descriptions-item label="目标场景" :span="2">
              {{ job.profile?.scenario_name || job.profile?.scenario_code || "未记录场景" }}
            </el-descriptions-item>
            <el-descriptions-item label="材料">{{ proposalTotals.materials }}</el-descriptions-item>
            <el-descriptions-item label="字段">{{ proposalTotals.fields }}</el-descriptions-item>
            <el-descriptions-item label="业务规则">{{ proposalTotals.rules }}</el-descriptions-item>
            <el-descriptions-item label="计算规则">{{ includedCalculations.length }}</el-descriptions-item>
            <el-descriptions-item label="报告章节">{{ proposalTotals.reportSections }}</el-descriptions-item>
            <el-descriptions-item label="不参与导入的工作表">{{ excludedSheetNames.length }}</el-descriptions-item>
          </el-descriptions>

          <div class="draft-write-scope block-gap">
            <section>
              <strong>本次替换</strong>
              <span>材料、字段、业务规则、计算配置、报告章节和来源工作表清单</span>
            </section>
            <section>
              <strong>继续保留</strong>
              <span>流程节点、节点顺序和节点依赖</span>
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
            <span class="text-muted">
              {{ job.status === 'APPLIED' ? '草稿已生成，请到场景配置页继续处理。' : '确认后写入或覆盖当前场景草稿。' }}
            </span>
            <div class="card-actions">
              <el-button @click="activeStep = 2">上一步</el-button>
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
                @click="router.push({ name: 'scene-detail', params: { scenarioCode: job.profile.scenario_code }, query: { tab: 'source' } })"
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
.rejected-json {
  margin: 12px;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
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
  grid-template-columns: repeat(3, minmax(0, 1fr));
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

.binding-actions {
  justify-content: flex-end;
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
