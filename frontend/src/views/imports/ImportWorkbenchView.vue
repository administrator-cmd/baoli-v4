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
  saveImportDecisions,
  type TemplateOption,
  startImportConversion,
  updateSheetBindings,
} from "@/api/imports";
import type {
  CandidateDecisionState,
  ImportJob,
  ImportJobSummary,
  ImportSheet,
  JsonValue,
  ProposalCalculation,
  ProposalRule,
  ProposalSheet,
  UploadPolicy,
} from "@/api/contracts";
import type { ConfigOrigin, DraftNodeItem } from "@/api/contracts/scenes";
import CandidateCorrectionDialog from "@/components/import/CandidateCorrectionDialog.vue";
import CandidateTable, { type CandidateColumn, type CandidateRow } from "@/components/import/CandidateTable.vue";
import FileUploadCard from "@/components/import/FileUploadCard.vue";
import ImportIssueCard from "@/components/import/ImportIssueCard.vue";
import ImportJobPicker from "@/components/import/ImportJobPicker.vue";
import { useDecisionAnswers } from "@/composables/useDecisionAnswers";
import ImportStepNav from "@/components/import/ImportStepNav.vue";
import TemplateDownloadButton from "@/components/scenes/TemplateDownloadButton.vue";
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
  nodeLabel,
  riskSourceNodeColumnLabel,
  riskSourceRuleColumnLabel,
  sheetRoleLabel,
  valueTypeLabel,
} from "@/constants/labels";
import { initialBindingOf, type NodeOption, type SheetBindingDraft } from "@/constants/sheetBinding";

const route = useRoute();
const router = useRouter();
const { operation, watching, track, reset: resetTracking } = useOperationTracker();
const { pending, failure, formMessage, run, setFailure } = useRequestRunner();

type CandidateTab = "material" | "field" | "rule" | "calculation";

const job = ref<ImportJob | null>(null);
const importJobs = ref<ImportJobSummary[]>([]);
const templates = ref<TemplateOption[]>([]);
/**
 * 本次上传所属的**场景编码**。
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
/** 候选原地修正弹窗状态。 */
const correctionTarget = ref<{ targetType: "MATERIAL" | "FIELD" | "RULE"; targetRef: string; candidate: Record<string, JsonValue> } | null>(
  null,
);
const correctionPending = ref(false);

/* 业务问题作答：状态与提交流程见 useDecisionAnswers。能力缺口在场景发布页逐条确认。 */
const {
  selectedOptions,
  update: updateIssueAnswer,
  readDecisionValue,
  initialise: initialiseDecisionAnswers,
  flush: flushDecisionDraft,
  submit: submitDecisionAnswers,
  reset: resetDecisionAnswers,
} = useDecisionAnswers({
  job,
  applyRevision: applyJobRevision,
  run,
  setFailure,
  formMessage,
});

const candidateTabModel = computed<CandidateTab>({
  get: () => candidateTab.value,
  set: (value) => {
    candidateTab.value = value === "field" || value === "rule" || value === "calculation" ? value : "material";
  },
});

/** 下载模板使用的场景：已打开作业时用作业所属场景，否则用上传前选中的场景。 */
const templateScenarioCode = computed(() => job.value?.profile?.scenario_code || uploadScenarioCode.value || null);

/** 统一模板协议声明的 Sheet 中文名（按 sheet_role）。 */
const profileSheetLabels = computed<Record<string, string>>(() => {
  const sheets = job.value?.profile?.sheets ?? [];
  return Object.fromEntries(sheets.map((item) => [item.sheet_role, item.display_name]));
});

/**
 * 作业所属场景里**用户配置的节点实例**：Sheet 绑定的唯一来源。
 *
 * 一张 Sheet 只能绑定一个节点；一个节点可以绑定多张 Sheet。系统不按表头自动绑定，
 * 建议只用于页面预选。作业没有记录场景（历史作业）时为空列表——
 * 此时页面明确提示「先在场景配置里建立节点」，不补一份默认节点。
 *
 * 节点**不在这里新建**：顺序与上游依赖是发布期 DAG 与执行顺序的唯一依据，
 * 这里只够填实例码与能力类型两项，建出来的节点依赖为空、会被排到最后执行，
 * 而发布期校验拦不住（依赖存在、无环、上游已启用三条全过）——是一个静默的错误顺序。
 * 新建与修改节点只在场景详情的「流程」页签里做。
 */
const scenarioNodes = ref<DraftNodeItem[]>([]);
const nodeLoadError = ref<string | null>(null);
let nodeRequest = 0;
let jobReception = 0;
let jobLoadRequest = 0;

const nodeOptions = computed<NodeOption[]>(() =>
  scenarioNodes.value.map((item) => ({
    nodeCode: item.node_code,
    nodeType: item.node_type,
    // 名称与能力类型同名时只显示一次：预置节点的名称就是能力类型名，
    // 无条件拼成「材料检查（材料检查）」是纯噪音。改了名的节点才需要标出能力类型。
    label:
      item.name === item.node_type_label ? item.name : `${item.name}（${item.node_type_label}）`,
  })),
);

async function loadScenarioNodes(scenarioCode: string | null | undefined): Promise<void> {
  const request = ++nodeRequest;
  if (!scenarioCode) {
    scenarioNodes.value = [];
    nodeLoadError.value = "当前作业没有所属场景，无法读取节点";
    return;
  }
  nodeLoadError.value = null;
  try {
    const draft = await getSceneDraft(scenarioCode);
    if (request !== nodeRequest) return;
    scenarioNodes.value = draft.nodes ?? [];
  } catch (error) {
    if (request !== nodeRequest) return;
    scenarioNodes.value = [];
    nodeLoadError.value = error instanceof Error ? error.message : "读取场景节点失败";
  }
}

/**
 * Sheet 用途的中文名。
 *
 * 优先用**统一模板协议声明的名称**，其次退回既有标签表；
 * 两者都没有时如实显示未识别，不猜。
 */
function roleLabel(role: string | null | undefined): string {
  if (!role) {
    return sheetRoleLabel(role);
  }
  return profileSheetLabels.value[role] ?? sheetRoleLabel(role);
}

const openIssues = computed(() => job.value?.issues.filter((item) => item.status === "OPEN") ?? []);
/** 已作结论的问题：提交后仍要能核对原答案与生效值。 */
const resolvedIssues = computed(() => job.value?.issues.filter((item) => item.status !== "OPEN") ?? []);
const proposalSheets = computed<ProposalSheet[]>(() => job.value?.proposals?.sheets ?? []);
const proposalCalculations = computed<ProposalCalculation[]>(() => job.value?.proposals?.calculations ?? []);
const hasProposals = computed(() => proposalSheets.value.length > 0 || proposalCalculations.value.length > 0);
const blockingIssueCount = computed(() => importBlockingIssues(job.value));

/**
 * 候选原地修正需要的引用选项与原始对象索引。
 * 全部来自当前作业的转换结果，不做额外推断。
 */
const materialRefOptions = computed(() =>
  proposalSheets.value.flatMap((sheet) => sheet.materials.map((material) => ({
    ref: material.candidate_ref,
    name: `${material.name}（${material.source_sheet} 第 ${material.source_row} 行）`,
  }))),
);
const ruleRefOptions = computed(() =>
  proposalSheets.value.filter((sheet) => !sheet.excluded).flatMap((sheet) => sheet.rules.filter(
    (rule) => rule.operator !== "RISK_TRIGGER" && rule.decision_state !== "EXCLUDED",
  ).map((rule) => {
    const materials = rule.comparison.scope_material_names.filter((name): name is string => Boolean(name)).join("、");
    return {
      ref: rule.candidate_ref,
      name: `${rule.name}（${materials ? `${materials} · ` : ""}${rule.source_sheet} 第 ${rule.source_row} 行）`,
    };
  })),
);
const candidateIndex = computed(() => {
  const index = new Map<string, { targetType: "MATERIAL" | "FIELD" | "RULE"; candidate: Record<string, JsonValue> }>();
  for (const sheet of proposalSheets.value) {
    for (const material of sheet.materials) {
      index.set(material.candidate_ref, { targetType: "MATERIAL", candidate: material as unknown as Record<string, JsonValue> });
    }
    for (const field of sheet.fields) {
      index.set(field.candidate_ref, {
        targetType: "FIELD",
        candidate: field as unknown as Record<string, JsonValue>,
      });
    }
    for (const rule of sheet.rules) {
      index.set(rule.candidate_ref, {
        targetType: "RULE",
        candidate: {
          ...rule,
          // parameters 在 comparison 里；修正弹窗需要按顶层的值做改动比对。
          parameters: rule.comparison.parameters ?? {},
          material_refs: rule.comparison.scope_material_refs,
          source_node: rule.comparison.source_node ?? null,
          source_rule_refs: rule.comparison.source_rule_refs,
        } as unknown as Record<string, JsonValue>,
      });
    }
  }
  return index;
});

/**
 * 需要业务确认用途的工作表：绑定表格里只出现这些。
 * 判定完全依据服务端返回的 fixed_excluded（模板说明页等元数据、没有业务数据的工作表），
 * 不按工作表名称在前端硬编码。
 */
const bindingSheets = computed<ImportSheet[]>(() => (job.value?.sheets ?? []).filter((sheet) => !isSystemExcludedSheet(sheet)));
/** 已排除的候选不参与有效统计：它们是明确不发布的项。 */
const proposalTotals = computed(() =>
  proposalSheets.value.reduce(
    (totals, sheet) => ({
      materials: totals.materials + sheet.materials.filter((item) => item.decision_state !== "EXCLUDED").length,
      fields: totals.fields + sheet.fields.filter((item) => item.decision_state !== "EXCLUDED").length,
      rules: totals.rules + sheet.rules.filter((item) => item.decision_state !== "EXCLUDED").length,
      reportSections: totals.reportSections + sheet.report_sections.length,
    }),
    { materials: 0, fields: 0, rules: 0, reportSections: 0 },
  ),
);
const includedCalculations = computed(() =>
  proposalCalculations.value.filter((item) => item.decision_state !== "EXCLUDED"),
);

const decisionCounts = computed(() => {
  const counts: Record<CandidateDecisionState, number> = { AUTO: 0, PENDING: 0, MANUAL: 0, EXCLUDED: 0 };
  for (const sheet of proposalSheets.value) {
    for (const state of ["AUTO", "PENDING", "MANUAL", "EXCLUDED"] as CandidateDecisionState[]) {
      counts[state] += countState(sheet, state);
    }
  }
  for (const item of proposalCalculations.value) {
    counts[item.decision_state] += 1;
  }
  return counts;
});

/** 有歧义候选：仍在等待业务补充。 */
const ambiguousCount = computed(() => decisionCounts.value.PENDING);
/** 明确候选：已自动识别或已人工处理，不需要逐项确认。 */
const confirmedCandidateCount = computed(() => decisionCounts.value.AUTO + decisionCounts.value.MANUAL);
/** 还没有选择用途、也没有显式排除的工作表：必须由业务选择用途或显式排除，没有默认值。 */
const unidentifiedSheets = computed(() => bindingSheets.value.filter((sheet) => !bindingFor(sheet).role && !bindingFor(sheet).excluded));
/** 已填写用途但没有表头行的工作表：同样不能被提交入口放过。 */
const missingHeaderSheets = computed(() =>
  bindingSheets.value.filter((sheet) => {
    const draft = bindingFor(sheet);
    return !draft.excluded && draft.role && draft.headerRow === null;
  }),
);
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

/** 已经形成转换结果或已发布的作业，服务端不再接受改动绑定与重复解析。 */
const importMutable = computed(() => Boolean(job.value?.proposals) === false && !sealed.value);

/** 尚未写入草稿、也未产生历史版本时，可以原地修正候选。 */
const candidateEditingEnabled = computed(() => Boolean(job.value) && hasProposals.value && !sealed.value);

/** 解析失败或成功后都允许再次提交；提交期间和查询期间不允许重复提交。 */
const canSubmitConversion = computed(() => importMutable.value && !pending.value && !operationRunning.value);

const duplicateRoleSheets = computed(() => {
  const byRole = new Map<string, string[]>();
  for (const sheet of bindingSheets.value) {
    const draft = bindingFor(sheet);
    if (draft.excluded || !draft.role) {
      continue;
    }
    const names = byRole.get(draft.role) ?? [];
    names.push(sheet.name);
    byRole.set(draft.role, names);
  }
  return Array.from(byRole.values()).filter((names) => names.length > 1);
});

const duplicateRoleNames = computed(() => new Set(duplicateRoleSheets.value.flat()));

function sheetProblems(sheet: ImportSheet): string[] {
  const problems: string[] = [];
  const draft = bindingFor(sheet);
  if (duplicateRoleNames.value.has(sheet.name)) {
    problems.push(`「${roleLabel(draft.role)}」被多个工作表同时使用`);
  }
  if (!draft.excluded && !draft.role) {
    problems.push("请选择本表用途，或设为不参与本次导入");
  }
  if (!draft.excluded && draft.role && draft.headerRow === null) {
    problems.push("请填写表头所在行");
  }
  if (!draft.excluded && draft.role && !draft.nodeCode) {
    // 未绑定节点的 Sheet 不进入执行范围：必须由用户显式绑定，系统不自动推断。
    problems.push("请选择这张表绑定的业务节点");
  }
  return problems;
}

const bindingBlocked = computed(
  () => Boolean(job.value) && (bindingSheets.value.some((sheet) => sheetProblems(sheet).length > 0) || duplicateRoleSheets.value.length > 0),
);

const bindingBlockedReason = computed(() => {
  if (unidentifiedSheets.value.length) {
    return `还有 ${unidentifiedSheets.value.length} 个工作表没有选择用途。`;
  }
  if (duplicateRoleSheets.value.length) {
    return "有工作表用途重复，请调整后再开始解析。";
  }
  if (missingHeaderSheets.value.length) {
    return `还有 ${missingHeaderSheets.value.length} 个工作表填写了用途但没有填写表头行。`;
  }
  return null;
});

/**
 * 当前绑定相对**初值**是否有未提交的改动。
 *
 * 比对的基线必须是绑定初值（`bindingDefaultsFor`），不是服务端原始字段：
 * 用途与表头行现在有默认值，拿原始字段当基线会让页面一打开就谎报「有未保存的改动」。
 */
const hasUnsavedBindings = computed(() =>
  bindingSheets.value.some((sheet) => {
    const draft = bindingFor(sheet);
    const saved = bindingDefaultsFor(sheet);
    return (
      draft.role !== saved.role ||
      draft.nodeCode !== saved.nodeCode ||
      draft.headerRow !== saved.headerRow ||
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

const decisionStats = computed<StatItem[]>(() => [
  { label: "待业务确认", value: ambiguousCount.value, tone: ambiguousCount.value ? "warning" : "success" },
  { label: "已自动识别", value: decisionCounts.value.AUTO },
  { label: "已人工处理", value: decisionCounts.value.MANUAL },
  { label: "已排除候选", value: decisionCounts.value.EXCLUDED },
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

/** 风险疑点（06_风险疑点）规则的节点，由服务端返回，不按工作表名称判断。 */
function isRiskRule(rule: ProposalRule): boolean {
  return rule.node === "RISK_RATING";
}

/** 本次转换结果里是否存在风险疑点规则：只有存在时才展示来源节点与来源审核规则两列。 */
const hasRiskRules = computed(() => proposalSheets.value.some((sheet) => sheet.rules.some(isRiskRule)));

const ruleColumns = computed<CandidateColumn[]>(() => {
  const columns: CandidateColumn[] = [
    { key: "name", label: "规则名称", primary: true, minWidth: 200 },
    { key: "node", label: "所属节点", width: 130 },
  ];
  if (hasRiskRules.value) {
    columns.push({ key: "source_node", label: riskSourceNodeColumnLabel, width: 140 });
    columns.push({ key: "source_rule", label: riskSourceRuleColumnLabel, minWidth: 180 });
  }
  columns.push({ key: "method", label: "判断方式", minWidth: 220 });
  columns.push({ key: "requirement", label: "审核要求", minWidth: 200 });
  return columns;
});

const calculationColumns: CandidateColumn[] = [
  { key: "name", label: "计算规则", primary: true, minWidth: 190 },
  { key: "formula", label: "公式", minWidth: 220 },
  { key: "parameters", label: "参数来源", minWidth: 260 },
  { key: "bands", label: "判定区间", width: 110 },
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
      state: field.decision_state,
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
      state: material.decision_state,
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
      const values = [rule.name, rule.node_name || nodeLabel(rule.node)];
      if (hasRiskRules.value) {
        // 风险疑点必须写明来源：只有风险规则有来源节点与来源审核规则，其它规则这两列为空。
        values.push(risk ? riskSourceNodeText(rule) : "", risk ? riskSourceRuleText(rule) : "");
      }
      values.push(ruleComparisonText(rule), rule.comparison.requirement || "未填写审核要求");
      return {
        ref: rule.candidate_ref,
        name: rule.name,
        values,
        state: rule.decision_state,
        source: sourceText(rule),
        details: [
          { label: "候选标识", value: rule.candidate_ref },
          { label: "规则编码", value: `${rule.node} · ${rule.operator}` },
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
      `${item.bands.length} 个`,
      calculationStatusLabel(item.status),
    ],
    state: item.decision_state,
    source: sourceText(item),
    details: [
      { label: "规则编号", value: item.rule_code },
      { label: "结果编号 / 类型", value: `${item.result_code} / ${item.result_type}` },
      { label: "节点实例", value: item.node_code },
      { label: "判定区间原文", value: item.bands_text || "未记录" },
      { label: "未声明参数", value: item.unresolved_identifiers.join("、") || "无" },
      { label: "计算定义原表", value: calculationOriginText(item.origin) },
      ...item.parameter_sources.map((parameter) => ({
        label: `参数 ${parameter.code || "未填写编号"} 原表`, value: calculationOriginText(parameter.origin),
      })),
      ...item.band_sources.map((band) => ({
        label: `判定 ${band.code || "未填写编号"} 原表`, value: calculationOriginText(band.origin),
      })),
    ],
  })),
);

/** 来源节点：只用服务端给出的中文文案；服务端没有给出时不显示，不在这里猜测。 */
function riskSourceNodeText(rule: ProposalRule): string {
  return rule.comparison.source_node_label ?? "";
}

/** 风险的全部来源审核规则，顺序与服务端候选引用一致。 */
function riskSourceRuleText(rule: ProposalRule): string {
  return rule.comparison.source_rule_names.join("、");
}

/** 规则的比较对象用业务语言描述。 */
function ruleComparisonText(rule: ProposalRule): string {
  const comparison = rule.comparison;
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

/** 写入草稿前已能确定的阻断项：未处理问题与未确认用途的工作表。 */
const writeBlockers = computed<string[]>(() => {
  const items: string[] = [];
  if (openIssues.value.length) {
    items.push(`还有 ${openIssues.value.length} 项业务问题没有处理；提交业务结论后才能导入。`);
  }
  if (unidentifiedSheets.value.length) {
    items.push(`还有 ${unidentifiedSheets.value.length} 个工作表没有选择用途。`);
  }
  if (duplicateRoleSheets.value.length) {
    items.push("有工作表用途重复，写入草稿前需要调整。");
  }
  return items;
});

function countState(sheet: ProposalSheet, state: CandidateDecisionState): number {
  return (
    sheet.materials.filter((item) => item.decision_state === state).length +
    sheet.fields.filter((item) => item.decision_state === state).length +
    sheet.rules.filter((item) => item.decision_state === state).length
  );
}

/**
 * 系统固定排除的工作表。
 *
 * 判定只依据服务端返回的字段：fixed_excluded 表示模板元数据（说明页等）或没有业务数据的工作表，
 * 这些工作表既不能绑定业务用途，也不会出现在 Sheet 绑定表格里。前端不按工作表名称做特判。
 */
function isSystemExcludedSheet(sheet: ImportSheet): boolean {
  return sheet.fixed_excluded;
}

/** 当前绑定值；没有本地草稿时退回初值口径（与渲染、与「是否有未提交改动」同源）。 */
function bindingFor(sheet: ImportSheet): SheetBindingDraft {
  return bindings.value[sheet.sheet_key] ?? bindingDefaultsFor(sheet);
}

/** 一张工作表的绑定初值。口径只有一份：`constants/sheetBinding.initialBindingOf`。 */
function bindingDefaultsFor(sheet: ImportSheet): SheetBindingDraft {
  return initialBindingOf(
    sheet,
    job.value?.profile?.sheets ?? [],
    nodeOptions.value,
    isSystemExcludedSheet(sheet),
  );
}

function updateBinding(payload: { sheetKey: string; draft: SheetBindingDraft }): void {
  bindings.value = { ...bindings.value, [payload.sheetKey]: payload.draft };
}

/**
 * 已保存的裁决形状是 {selected: ...}；刷新恢复时只取其中的选择值。
 * 不能把整个 decision 对象当成业务值用，否则回填形状与提交形状不一致。
 */
/** 已作结论的问题展示用：优先显示生效的选择值（取值口径由 workbench 提供）。 */
function decisionDisplay(decision: JsonValue): string {
  return formatBusinessValue(readDecisionValue(decision));
}

/**
 * 绑定初值。已确认的用途/节点/表头行从作业记录恢复；未确认的按下述默认值给出：
 *
 * - **用途**：系统按工作表名称识别出的结果直接选中（协议固定 Sheet 名，名称与角色一一对应）；
 *   识别不出的留空，必须由业务明确选择或排除；
 * - **表头行**：默认第 1 行——系统生成的 base Excel 构造上就把表头写在第 1 行；
 * - **节点**：只在「该用途下唯一可选」时预选，同类节点有多个实例时不猜。
 *
 * 三个默认值都只是**初值**，提交仍以用户看到并确认的值为准。
 * preserveAnswers：同一作业刷新时保留用户本地已填但未落库的答案，避免刷新清空输入。
 */
function initialiseBindings(importJob: ImportJob, preserveAnswers = false): void {
  const profileSheets = importJob.profile?.sheets ?? [];
  bindings.value = Object.fromEntries(
    importJob.sheets.map(
      (sheet): [string, SheetBindingDraft] => [
        sheet.sheet_key,
        initialBindingOf(sheet, profileSheets, nodeOptions.value, isSystemExcludedSheet(sheet)),
      ],
    ),
  );
  // 答案的初值口径（草稿值 → 展示值、以及刷新时保留本地输入）由 workbench 统一负责。
  initialiseDecisionAnswers(importJob, preserveAnswers);
}

/**
 * 刷新或切换作业后落在真实可操作的步骤上。
 * 已有转换结果时不再落回工作表绑定：作业完成转换后，服务端不再接受改动绑定。
 */
function inferStep(importJob: ImportJob): number {
  if (importJob.status === "PUBLISHED" || importJob.status === "APPLIED") {
    return 3;
  }
  if (!importJob.proposals) {
    return importJob.latest_operation && isOperationActive(importJob.latest_operation.status) ? 1 : 0;
  }
  return importJob.issues.some((issue) => issue.status === "OPEN") ? 2 : 3;
}

/**
 * 接收一份作业视图并落到真实可操作的步骤上。
 *
 * **先读回节点，再初始化绑定**：绑定的默认节点取决于「该用途下有几个可选实例」，
 * 节点还没读回来就会把本该预选的那一项算成 0 个。顺序不能颠倒。
 */
async function receiveJob(importJob: ImportJob, keepStep = false): Promise<void> {
  const reception = ++jobReception;
  const changed = job.value?.job_no !== importJob.job_no;
  job.value = importJob;
  // 节点实例来自作业所属场景的配置：绑定下拉只能选它们。
  await loadScenarioNodes(importJob.profile?.scenario_code);
  if (reception !== jobReception || job.value?.job_no !== importJob.job_no) return;
  initialiseBindings(importJob, !changed);
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

function openSceneFlow(): void {
  const scenarioCode = job.value?.profile?.scenario_code;
  const jobNo = job.value?.job_no;
  if (!scenarioCode || !jobNo) return;
  void router.push({
    name: "scene-detail",
    params: { scenarioCode },
    query: { tab: "flow", importJob: jobNo },
  });
}

function resetImportWorkspace(): void {
  ++jobLoadRequest;
  ++jobReception;
  ++nodeRequest;
  loadingJob.value = false;
  // 完整重置操作查询：切作业后不再残留上一个作业的操作面板与轮询编号。
  resetTracking();
  resetDecisionAnswers();
  job.value = null;
  importJobs.value = [];
  bindings.value = {};
  activeStep.value = 0;
  candidateTab.value = "material";
  nodeLoadError.value = null;
  selectedFileSize.value = null;
  correctionTarget.value = null;
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
    activeStep.value = reloaded.issues.some((issue) => issue.status === "OPEN") ? 2 : 3;
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
  // 上传必须从**已创建的场景**进入：场景由用户先建好，不由 Excel 表头推断。
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

/** 保存工作表用途并提交后台解析。长处理只受理，随后由页面查询真实状态。 */
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
            role: systemExcluded ? null : binding.role,
            node_code: systemExcluded ? null : binding.nodeCode,
            header_row: systemExcluded ? null : binding.headerRow,
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

/**
 * 候选原地修正 / 排除 / 恢复：走同一裁决接口，带上页面看到的草稿修订号。
 * 修订过期（IMPORT_DRAFT_REVISION_CONFLICT）由 FailureNotice 显示，提示刷新后重试。
 */
async function applyCandidateDecision(
  targetType: "MATERIAL" | "FIELD" | "RULE",
  targetRef: string,
  action: "RESOLVE" | "EXCLUDE" | "ACCEPT",
  value: Record<string, JsonValue> = {},
): Promise<void> {
  if (!job.value) {
    return;
  }
  const jobNo = job.value.job_no;
  await run(async () => {
    // 先落暂存，确保下面带上的草稿修订号与服务端当前修订一致。
    await flushDecisionDraft();
    const revision = job.value?.draft_revision ?? 0;
    const updated = await saveImportDecisions(jobNo, [{ target_type: targetType, target_ref: targetRef, action, value }], revision);
    await receiveJob(updated, true);
  });
}

function openCandidateEdit(targetType: "MATERIAL" | "FIELD" | "RULE", row: CandidateRow): void {
  const entry = candidateIndex.value.get(row.ref);
  if (!entry) {
    formMessage.value = "找不到该候选对象，请刷新页面后重试。";
    return;
  }
  correctionTarget.value = {
    targetType: entry.targetType,
    targetRef: row.ref,
    candidate: entry.candidate,
  };
}

async function submitCandidateEdit(value: Record<string, JsonValue>): Promise<void> {
  const target = correctionTarget.value;
  if (!target) {
    return;
  }
  if (!Object.keys(value).length) {
    ElMessage.info("没有检测到属性改动。");
    return;
  }
  correctionPending.value = true;
  try {
    await applyCandidateDecision(target.targetType, target.targetRef, "RESOLVE", value);
    correctionTarget.value = null;
  } finally {
    correctionPending.value = false;
  }
}

function excludeCandidate(targetType: "MATERIAL" | "FIELD" | "RULE", row: CandidateRow): void {
  if (!window.confirm(`确认排除「${row.name}」？排除后该候选不写入场景配置，可随时恢复。`)) {
    return;
  }
  void applyCandidateDecision(targetType, row.ref, "EXCLUDE");
}

function restoreCandidate(targetType: "MATERIAL" | "FIELD" | "RULE", row: CandidateRow): void {
  void applyCandidateDecision(targetType, row.ref, "ACCEPT");
}

/**
 * 暂存只回写修订号与问题状态，不重置绑定与步骤，避免打断用户当前编辑。
 */
function applyJobRevision(updated: ImportJob): void {
  if (!job.value) {
    return;
  }
  job.value = { ...job.value, draft_revision: updated.draft_revision, issues: updated.issues };
}

/** 提交业务结论：读回作业与场景节点后进入第三步。 */
async function saveDecisions(): Promise<void> {
  await submitDecisionAnswers(async (updated) => {
    await receiveJob(updated, true);
    activeStep.value = 3;
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
          ...(hasExistingBusinessDraft
            ? [h("p", { style: { marginBottom: "12px" } },
                `当前草稿已有 ${draft.materials.length} 个材料位、${draft.fields.length} 个字段、${draft.rules.filter((rule) => !rule.system).length} 条业务规则、${draft.calculations.length} 条计算规则；其中的人工修改将被覆盖。`)]
            : []),
          h("p", { style: { marginBottom: "12px" } },
            "材料、字段、业务规则、计算配置、报告章节和 Sheet 绑定将被整份替换；流程节点、节点顺序和依赖关系继续保留。"),
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

/** 离开歧义处理步骤前落一次答案暂存，避免未提交答案只存在内存里。 */
watch(activeStep, (next, previous) => {
  if (previous === 2 && next !== 2) {
    void flushDecisionDraft().catch((error) => setFailure(error));
  }
});
</script>

<template>
  <div class="import-workbench">
    <PageHeader
      :title="job ? '规则库导入工作台' : '规则库导入'"
      :object-name="job?.source_filename ?? null"
      :busy="pending"
      @refresh="job ? loadJob(job.job_no) : loadImportJobs()"
    >
      <template #actions>
        <TemplateDownloadButton :scenario-code="templateScenarioCode" label="下载当前场景模板" />
      </template>
    </PageHeader>

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
          :profile-name="null"
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
          :profile-name="job.profile?.display_name || job.profile_version"
          :selected-file-size="selectedFileSize"
          :policy="uploadPolicy?.rulebook ?? null"
          :pending="pending"
          :disabled="!importMutable"
          @pick="uploadRulebook"
        />
        <div class="sticky-actions">
          <div class="sticky-actions__bar">
            <span class="text-muted">上传成功后才能进入工作表绑定。</span>
            <div class="card-actions">
              <el-button @click="startNewImport">重新上传规则库</el-button>
              <el-button type="primary" @click="activeStep = 1">下一步：Sheet 绑定</el-button>
            </div>
          </div>
        </div>
      </el-card>

      <!-- 第二步：Sheet 绑定 -->
      <el-card v-else-if="activeStep === 1" shadow="never" class="block-gap">
        <template #header>第二步 · 确认工作表用途和表头</template>

        <el-alert v-if="nodeLoadError" class="block-gap-sm" type="error" show-icon :closable="false" :title="nodeLoadError" />

        <SheetBindingTable
          class="block-gap"
          :sheets="bindingSheets"
          :drafts="bindings"
          :profile-sheets="job?.profile?.sheets ?? []"
          :node-options="nodeOptions"
          :problems-of="sheetProblems"
          @update:draft="updateBinding"
        />

        <el-alert
          v-if="bindingBlocked"
          class="block-gap"
          type="warning"
          show-icon
          :closable="false"
          title="工作表绑定还有待处理项"
        >
          {{ bindingBlockedReason ?? "请检查每张工作表的用途与表头行。" }}
        </el-alert>

        <div class="sticky-actions">
          <div class="sticky-actions__bar binding-actions">
            <div class="card-actions">
              <el-button @click="activeStep = 0">上一步</el-button>
              <el-button :disabled="!job.profile?.scenario_code" @click="openSceneFlow">配置场景流程</el-button>
              <el-button :loading="loadingJob" @click="reloadSavedBindings">重新加载已保存绑定</el-button>
              <el-button
                type="primary"
                :disabled="!canSubmitConversion"
                :title="conversionDisabledReason ?? ''"
                @click="submitConversion(true)"
              >
                保存并开始解析
              </el-button>
            </div>
          </div>
        </div>
      </el-card>

      <!-- 第三步：字段规则确认 -->
      <el-card v-else-if="activeStep === 2" shadow="never" class="block-gap">
        <template #header>
          <div class="card-header">
            <span>第三步 · 规则转换、候选和歧义处理</span>
            <StatusBadge
              :status="openIssues.length ? 'PENDING_DECISION' : 'CONFIRMED'"
              :label="openIssues.length ? `${openIssues.length} 项待确认` : '无需补充'"
            />
          </div>
        </template>

        <p class="text-muted">
          这里只列出需要业务判断的内容（多个真实候选、缺少阈值或口径、无法唯一确定的检查方式）。
          其余内容系统已自动识别，不需要逐项确认。
        </p>

        <StatGrid class="block-gap" :items="decisionStats" />

        <StateBlock
          v-if="loadingJob"
          kind="loading"
        />
        <StateBlock
          v-else-if="operationRunning && !hasProposals"
          kind="loading"
          title="正在解析规则库"
          description="完成后将显示需要确认的材料、字段和规则。"
        />
        <template v-else-if="hasProposals">
          <el-alert
            v-if="!openIssues.length"
            type="success"
            show-icon
            :closable="false"
            title="当前没有需要业务确认的问题"
          >
            系统已把全部候选纳入有效配置，可以直接查看有效配置。
          </el-alert>

          <template v-else>
            <el-alert
              class="block-gap"
              type="warning"
              show-icon
              :closable="false"
              :title="`有 ${openIssues.length} 项需要业务判断`"
            >
              提交业务结论后才能写入场景草稿。
            </el-alert>
            <div class="stack block-gap">
              <ImportIssueCard
                v-for="issue in openIssues"
                :key="issue.issue_no"
                :issue="issue"
                :selected="selectedOptions[issue.issue_no]"
                @update:selected="updateIssueAnswer(issue.issue_no, $event)"
              />
            </div>
            <p v-if="formMessage" class="text-danger block-gap">{{ formMessage }}</p>
          </template>

          <el-card v-if="resolvedIssues.length" shadow="never" class="block-gap">
            <template #header>已处理的问题（{{ resolvedIssues.length }} 项）</template>
            <el-table :data="resolvedIssues" border>
              <el-table-column label="问题" prop="title" min-width="220" show-overflow-tooltip />
              <el-table-column label="结论状态" width="140">
                <template #default="{ row }"><StatusBadge :status="row.status" /></template>
              </el-table-column>
              <el-table-column label="已作结论（生效值）" min-width="200" show-overflow-tooltip>
                <template #default="{ row }">{{ decisionDisplay(row.decision) }}</template>
              </el-table-column>
              <el-table-column label="来源" min-width="180" show-overflow-tooltip>
                <template #default="{ row }">
                  {{ row.source.sheet_name || "未知工作表" }}
                  <template v-if="row.source.row_no"> · 第 {{ row.source.row_no }} 行</template>
                </template>
              </el-table-column>
            </el-table>
            <p class="text-hint">这里显示已作结论的问题及其生效值；未处理问题仍需在上面作答。</p>
          </el-card>

          <el-card shadow="never" class="block-gap">
            <template #header>有效配置</template>
            <el-tabs v-model="candidateTabModel">
              <el-tab-pane name="material">
                <template #label>材料（{{ materialRows.length }}）</template>
                <CandidateTable
                  :columns="materialColumns"
                  :rows="materialRows"
                  :can-edit="candidateEditingEnabled"
                  empty-title="转换结果中没有材料候选"
                  empty-description="当前工作表没有定义材料位。核查 01_材料清单工作表的用途与表头行后，请重新上传规则库再解析——已形成转换结果的作业不接受原地改动工作表绑定。"
                  @edit="openCandidateEdit('MATERIAL', $event)"
                  @exclude="excludeCandidate('MATERIAL', $event)"
                  @restore="restoreCandidate('MATERIAL', $event)"
                />
              </el-tab-pane>
              <el-tab-pane name="field">
                <template #label>字段（{{ fieldRows.length }}）</template>
                <CandidateTable
                  :columns="fieldColumns"
                  :rows="fieldRows"
                  :can-edit="candidateEditingEnabled"
                  empty-title="转换结果中没有字段候选"
                  empty-description="当前工作表的转换结果只有材料或规则，没有可纳入的字段。请检查工作表用途后重新上传规则库。"
                  @edit="openCandidateEdit('FIELD', $event)"
                  @exclude="excludeCandidate('FIELD', $event)"
                  @restore="restoreCandidate('FIELD', $event)"
                />
              </el-tab-pane>
              <el-tab-pane name="rule">
                <template #label>规则（{{ ruleRows.length }}）</template>
                <CandidateTable
                  :columns="ruleColumns"
                  :rows="ruleRows"
                  :can-edit="candidateEditingEnabled"
                  empty-title="转换结果中没有规则候选"
                  empty-description="当前工作表没有可执行的检查规则。只写入材料清单和字段也可以，但审核任务不会有自动判断结果。"
                  @edit="openCandidateEdit('RULE', $event)"
                  @exclude="excludeCandidate('RULE', $event)"
                  @restore="restoreCandidate('RULE', $event)"
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
            自动识别与已人工处理的候选共 {{ confirmedCandidateCount }} 项；仍有 {{ ambiguousCount }} 项处于待补充状态。
            系统不会代替业务做选择，也不会在缺少阈值时使用默认值。
          </p>
        </template>
        <StateBlock
          v-else
          kind="empty"
          title="还没有解析结果"
          description="回到上一步确认工作表用途并提交解析，完成后这里会显示材料、字段和规则候选。"
        />

        <div class="sticky-actions">
          <div class="sticky-actions__bar">
            <span class="text-muted">
              {{ openIssues.length ? "存在需要业务确认的内容，处理后才能写入草稿。" : "没有待确认问题，可以生成场景草稿。" }}
            </span>
            <div class="card-actions">
              <el-button @click="activeStep = 1">上一步</el-button>
              <el-button v-if="openIssues.length" type="primary" :disabled="pending" @click="saveDecisions">
                提交业务结论
              </el-button>
              <el-button v-else type="primary" :disabled="!hasProposals" @click="activeStep = 3">
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
            <StatusBadge :status="job.status === 'APPLIED' ? 'APPLIED' : writeBlockers.length ? 'BLOCKED' : 'READY'" />
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
              <span>材料、字段、业务规则、计算配置、报告章节和 Sheet 绑定</span>
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

        <el-alert
          v-if="writeBlockers.length"
          class="block-gap"
          type="error"
          show-icon
          :closable="false"
          title="存在阻断项，不能导入场景配置"
        >
          <div v-for="item in writeBlockers" :key="item">{{ item }}</div>
        </el-alert>
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
                :disabled="writeBlockers.length > 0 || !hasProposals"
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

    <CandidateCorrectionDialog
      :open="Boolean(correctionTarget)"
      :target-type="correctionTarget?.targetType ?? 'MATERIAL'"
      :candidate="correctionTarget?.candidate ?? null"
      :materials="materialRefOptions"
      :rules="ruleRefOptions"
      :pending="correctionPending"
      @close="correctionTarget = null"
      @submit="submitCandidateEdit"
    />
  </div>
</template>

<style scoped>
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
