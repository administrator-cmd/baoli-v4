<script setup lang="ts">
import { computed, ref, watch } from "vue";

import type {
  AuditRule,
  JsonValue,
  RuleCheckComparison,
  RuleInputSnapshot,
  RuleStandard,
  RuleStatusItem,
} from "@/api/contracts";
import CrossComparisonPanel from "@/components/audit/CrossComparisonPanel.vue";
import DrawerPanel from "@/components/DrawerPanel.vue";
import StateBlock from "@/components/StateBlock.vue";
import StatusBadge from "@/components/StatusBadge.vue";
import { getRuleStatusCatalog } from "@/api/system";
import {
  evidenceRouteLabel,
  formatBusinessValue,
  judgeRouteLabel,
  nodeLabel,
  statusTone,
} from "@/constants/labels";

/**
 * 规则结果。
 *
 * 每条结果按审核人员的阅读顺序呈现：
 *   审核事项 → 最终状态 → 判断结论 → 判断原因 → 原文内容 → 标准化结果 →
 *   审核标准 → 实际比较 → 人工复核 → 技术执行详情。
 *
 * 列表默认是**紧凑表格**，不是把每条规则都摊成长卡片；点开某一条才打开详情抽屉。
 * 这样几十条规则也能一眼扫完，需要细看的再展开。
 *
 * 「位置」不是审核的前置条件：页码、坐标、索引号收在技术执行详情里，
 * 缺失或对不上都不影响上面这些内容的可读性。未执行与被跳过都不会显示成通过。
 */
const props = defineProps<{
  rules: AuditRule[];
  /** 本模板未配置检查规则的业务节点，用于解释为什么某些规则没有结果。 */
  unconfiguredNodes: string[];
  /** 打开时预置的节点筛选（例如从「跨文件校验」工作区进入）。 */
  initialNodeFilter?: string | null;
}>();

/* ---- 结果负载读取：全部来自服务端 result_json，不做前端推断 ---- */

function payloadOf(rule: AuditRule): Record<string, JsonValue> {
  return rule.result && typeof rule.result === "object" ? rule.result : {};
}

function standardOf(rule: AuditRule): RuleStandard | null {
  const value = payloadOf(rule).standard;
  return value && typeof value === "object" && !Array.isArray(value) ? (value as RuleStandard) : null;
}

function comparisonOf(rule: AuditRule): RuleCheckComparison | null {
  const value = payloadOf(rule).comparison;
  return value && typeof value === "object" && !Array.isArray(value) ? (value as RuleCheckComparison) : null;
}

function inputsOf(rule: AuditRule): RuleInputSnapshot[] {
  const value = payloadOf(rule).inputs;
  return Array.isArray(value) ? (value as RuleInputSnapshot[]) : [];
}

/** 抽取结果：本规则实际从材料里取到的字段原文与标准化值。 */
function extractedOf(rule: AuditRule): RuleInputSnapshot[] {
  const value = payloadOf(rule).extracted_values;
  return Array.isArray(value) ? (value as RuleInputSnapshot[]) : [];
}

/** 比较规则用 inputs；抽取规则用 extracted_values（它没有参与比较的输入，只有产出）。 */
function valueSourcesOf(rule: AuditRule): RuleInputSnapshot[] {
  const inputs = inputsOf(rule);
  return inputs.length ? inputs : extractedOf(rule);
}

/** 这条规则是否按材料逐份判定（文件质量检查、材料检查）：此时输入是材料而非字段。 */
function materialRowsOf(rule: AuditRule): RuleInputSnapshot[] {
  return inputsOf(rule).filter((item) => item.kind === "MATERIAL");
}

function materialsOf(rule: AuditRule): string {
  const names = new Set<string>();
  for (const item of inputsOf(rule)) {
    if (item.material_name) {
      names.add(item.material_name);
    }
  }
  const comparison = comparisonOf(rule);
  for (const label of [comparison?.left_label, comparison?.right_label, ...(comparison?.addend_labels ?? [])]) {
    const name = typeof label === "string" ? label.split("·")[0] : "";
    if (name) {
      names.add(name);
    }
  }
  return Array.from(names).join("、");
}

/**
 * 规则所属节点。
 *
 * 结果负载里目前**没有**节点字段，因此这里只读不猜：读不到就返回 null，
 * 由页面把节点筛选显式标为「服务端未返回」，而不是按材料数量或规则名去推断
 * 一条检查属于文件内部校验还是跨文件校验——猜错会让筛选数量与真实结果对不上。
 * 服务端补上 node 之后，这里无需改动即可生效。
 */
function nodeOf(rule: AuditRule): string | null {
  const value = payloadOf(rule).node;
  return typeof value === "string" && value ? value : null;
}

/** 规则结果里是否带节点信息：为 false 时页面必须说明筛选不可用。 */
const nodeMetaAvailable = computed(() => props.rules.some((rule) => nodeOf(rule) !== null));

/** 审核标准：只在标准不明确时才提示缺什么，不代填默认条件。 */
function standardText(rule: AuditRule): string {
  const standard = standardOf(rule);
  if (!standard) {
    return rule.configured_requirement
      ? `任务配置要求：${rule.configured_requirement}`
      : "本条结果没有记录审核标准";
  }
  if (standard.requirement) {
    return standard.requirement;
  }
  if (standard.missing) {
    return `标准未明确：${standard.missing}`;
  }
  if (standard.operator_label) {
    return `按「${standard.operator_label}」判定`;
  }
  return "本条结果没有记录审核标准";
}

/** 审核标准之下的一行判据明细：检查项目与合格要求、或比较方式与允许差值。 */
function standardDetail(rule: AuditRule): string {
  const standard = standardOf(rule);
  if (!standard) {
    return "";
  }
  const parts: string[] = [];
  if (standard.check_item_label) {
    parts.push(
      standard.check_requirement_label
        ? `检查项目：${standard.check_item_label}（合格要求：${standard.check_requirement_label}）`
        : `检查项目：${standard.check_item_label}`,
    );
    if (standard.threshold !== null && standard.threshold !== undefined) {
      parts.push(`标准值：${standard.threshold}${standard.threshold_unit ?? ""}`);
    }
    return parts.join(" · ");
  }
  if (standard.comparison_label) {
    parts.push(`比较方式：${standard.comparison_label}`);
  }
  if (standard.threshold !== null && standard.threshold !== undefined) {
    parts.push(`允许差值 ${standard.threshold}${standard.threshold_unit ?? ""}`);
  }
  return parts.join(" · ");
}

/**
 * 判断原因：无法判断要说清究竟缺什么，不能统一写成「证据不足」。
 * 「能力未实现」与「证据不足」是两种不同的缺什么：前者要改配置或等能力接入，
 * 后者要补原文或补字段，混成一句话会让人去补根本没有缺的东西。
 */
function reasonText(rule: AuditRule): string | null {
  const payload = payloadOf(rule);
  const blocked = payload.blocked_reason;
  const missing = payload.missing_inputs;
  if (blocked === "CAPABILITY_UNIMPLEMENTED") {
    const capability = payload.unimplemented_capability;
    const label =
      capability && typeof capability === "object" && !Array.isArray(capability)
        ? ((capability as Record<string, JsonValue>).label as string | null)
        : null;
    return `能力未实现：本项引用的能力${label ? `“${label}”` : ""}当前没有判定实现，系统不能给出结论`;
  }
  if (blocked === "STANDARD_UNDEFINED") {
    return "标准不明确：缺少已确认的换算口径，系统不代填默认条件";
  }
  if (blocked === "EVIDENCE_REJECTED") {
    return "依据不可用：依赖的材料已被人工确认与材料位不符";
  }
  if (blocked === "MATERIAL_NOT_PREPARED") {
    return "缺少内容：依赖材料的文件处理尚未完成";
  }
  if (blocked === "NO_FIELD_FACT" || blocked === "NO_SUBJECT_VALUE") {
    return "缺少内容：规则引用的字段尚未形成有效取值";
  }
  if (blocked === "SUBJECT_CANDIDATE_MISSING") {
    return "依据不可用：本次没有可用的印章主体候选";
  }
  if (Array.isArray(missing) && missing.length) {
    const inputs = inputsOf(rule);
    const names = missing.map((key) => {
      const input = inputs.find((item) => item.field_key === key);
      return input?.field_name
        ? `${input.material_name || "材料"}·${input.field_name}`
        : "未取得有效值的信息项";
    });
    return `缺少内容：${names.join("、")} 没有可用的取值`;
  }
  return null;
}

/** 本条结果引用的能力：标识、版本与是否已实现。 */
function capabilityOf(rule: AuditRule): Record<string, JsonValue> | null {
  const value = payloadOf(rule).capability;
  return value && typeof value === "object" && !Array.isArray(value) ? (value as Record<string, JsonValue>) : null;
}

function materialTypeNotice(rule: AuditRule): string | null {
  const notice = payloadOf(rule).material_type_notice;
  return typeof notice === "string" && notice ? notice : null;
}

function routeText(rule: AuditRule): string {
  const route = payloadOf(rule).route;
  if (!route || typeof route !== "object" || Array.isArray(route)) {
    return "取证方式未记录";
  }
  const record = route as Record<string, JsonValue>;
  return `${evidenceRouteLabel(record.evidence as string | null)} · ${judgeRouteLabel(record.judge as string | null)}`;
}

function evidenceNumbers(rule: AuditRule): string[] {
  const value = payloadOf(rule).evidence_nos;
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === "string") : [];
}

function applicabilityText(rule: AuditRule): string {
  const applicability = payloadOf(rule).applicability;
  if (!applicability || typeof applicability !== "object" || Array.isArray(applicability)) {
    return "适用条件未记录";
  }
  const record = applicability as Record<string, JsonValue>;
  return typeof record.label === "string" && record.label ? record.label : "适用条件未记录";
}

function manualNote(rule: AuditRule): string | null {
  const note = payloadOf(rule).manual_business_note;
  return typeof note === "string" && note ? note : null;
}

function manuallyReviewed(rule: AuditRule): boolean {
  return typeof payloadOf(rule).manual_review_no === "string";
}

/** 人工改判后的有效状态；服务端写入 manual_effective_status。 */
function manualEffectiveStatus(rule: AuditRule): string | null {
  const value = payloadOf(rule).manual_effective_status;
  return typeof value === "string" && value ? value : null;
}

/*
 * 紧凑列表里的三列取自服务端本次执行的真实事实：
 *   原文内容   ← 输入摘要的 raw_value（材料里真实写着的内容）
 *   标准化结果 ← 输入摘要的 normalized_value（为比较确定性算出的值）
 * 一条规则可能引用多个字段，这里按行合并展示，并在详情抽屉里逐项展开。
 */

interface CompactValue {
  /** 业务名（材料·字段，或材料级输入的材料名）。 */
  label: string;
  raw: string | null;
  normalized: string | null;
  status: string | null;
  message: string | null;
}

function compactValues(rule: AuditRule): CompactValue[] {
  const materialRows = materialRowsOf(rule);
  if (materialRows.length) {
    return materialRows.map((item) => ({
      label: item.material_name || "未知材料",
      raw: item.filename ?? null,
      normalized: null,
      status: item.status ?? null,
      message: item.message ?? null,
    }));
  }
  return valueSourcesOf(rule).map((item) => ({
    label: `${item.material_name || "未知材料"} · ${item.field_name || item.field_key || "未知字段"}`,
    raw: item.raw_value ?? null,
    normalized:
      item.normalized_value !== null && item.normalized_value !== undefined
        ? formatBusinessValue(item.normalized_value)
        : null,
    status: item.status ?? null,
    message: null,
  }));
}

/** 一列的合并文本；没有内容时说明是哪一种没有，不留空白。 */
function columnText(values: CompactValue[], key: "raw" | "normalized", rule: AuditRule): string {
  if (!values.length) {
    return "本条结果未记录取值内容";
  }
  const parts = values
    .map((item) => {
      const value = key === "raw" ? item.raw : item.normalized;
      if (value === null || value === undefined || value === "") {
        return null;
      }
      return values.length > 1 ? `${item.label}：${value}` : value;
    })
    .filter((item): item is string => item !== null);
  if (parts.length) {
    return parts.join("；");
  }
  if (key === "raw") {
    if (rule.status === "NOT_EXECUTED") {
      return "本次未执行，没有原文";
    }
    if (rule.status === "SKIPPED") {
      return "本模板未配置此类检查，未执行";
    }
    return "服务端没有返回原文内容";
  }
  if (rule.status === "NOT_EXECUTED" || rule.status === "SKIPPED") {
    return "未执行，没有标准化结果";
  }
  return "服务端没有返回标准化结果";
}

function formatInputValue(item: RuleInputSnapshot): string {
  const raw = item.raw_value;
  const normalized = item.normalized_value ?? item.value;
  if (raw && normalized !== null && normalized !== undefined && String(raw) !== String(normalized)) {
    return `${raw}（标准化 ${formatBusinessValue(normalized)}）`;
  }
  const shown = raw ?? normalized;
  if (shown === null || shown === undefined || shown === "") {
    // 没有取值时说明是哪种情况，不统一写成「未提供」：
    // 材料级输入本来就没有字段取值，与字段确实没抽到是两件事。
    return item.kind === "MATERIAL" ? "按材料判定，无字段取值" : "未取得取值";
  }
  return formatBusinessValue(shown);
}

/* ---- 筛选 ---- */

const search = ref("");
const statusFilter = ref("ALL");
const nodeFilter = ref<string | null>(props.initialNodeFilter ?? null);

/**
 * 父页面从工作区进入时带来的阶段筛选。
 *
 * 只在父值真正变化时同步；父值清空（null）时也同步清空，这样从「跨文件校验」
 * 进入后再点「通用规则结果」不会残留上一次的阶段筛选。
 */
watch(
  () => props.initialNodeFilter,
  (value) => {
    nodeFilter.value = value ?? null;
  },
);

/**
 * 服务端规则状态目录。分档以它为准，页面不再本地维护一份名单——
 * 本地名单与后端漂移过（`NOT_APPLICABLE`、`SKIPPED` 曾被漏出分档）。
 *
 * 目录读取中或读取失败时退回下面的**只读兜底**分档：兜底只在拿不到目录时生效，
 * 且未覆盖的状态码仍会被显式提示出来。没有兜底的话，目录还没到时所有结果都会落进
 * 「未识别」，筛选数量显示为 0，用户会以为结果丢了。
 */
const catalog = ref<RuleStatusItem[] | null>(null);
const catalogError = ref<string | null>(null);

void (async () => {
  try {
    catalog.value = (await getRuleStatusCatalog()).statuses;
  } catch (error) {
    catalogError.value = error instanceof Error ? error.message : "未知原因";
  }
})();

/** 与服务端 STATUS_CATEGORIES 同口径的兜底分档；仅用于目录不可用时。 */
const FALLBACK_CATEGORY: Record<string, string> = {
  PASSED: "DECIDED",
  ABNORMAL: "DECIDED",
  REVIEWED: "DECIDED",
  EVIDENCE_INSUFFICIENT: "UNDECIDED",
  PENDING_REVIEW: "UNDECIDED",
  NOT_EXECUTED: "UNDECIDED",
  RUNNING: "UNDECIDED",
  SKIPPED: "UNDECIDED",
  NOT_APPLICABLE: "NOT_APPLICABLE",
};

/**
 * 筛选分档。它必须覆盖服务端可能给出的**全部**状态码：
 *   「未执行」= 应当执行但没有跑完；「不适用」= 适用条件明确不成立，本次就不该查；
 *   「已跳过」= 本模板没配置这类检查。三者都不是通过，也不能互相冒充。
 * 在目录中找不到的码（新增或历史遗留）不会被静默丢掉——它进「未识别」，并在下方单独提示。
 */
const filters = [
  { value: "ALL", label: "全部", categories: [] as string[] },
  { value: "DECIDED", label: "已判定", categories: ["DECIDED"] },
  { value: "UNDECIDED", label: "尚未判定", categories: ["UNDECIDED"] },
  { value: "NOT_APPLICABLE", label: "不适用", categories: ["NOT_APPLICABLE"] },
];

/** 目录可用时以目录为准；否则用兜底表，两者都没有才判为未识别。 */
function categoryOf(rule: AuditRule): string {
  const known = catalog.value?.find((item) => item.status === rule.status);
  if (known) {
    return known.category;
  }
  if (!catalog.value) {
    return FALLBACK_CATEGORY[rule.status] ?? "UNKNOWN";
  }
  return "UNKNOWN";
}

function matchesFilter(rule: AuditRule, filter: (typeof filters)[number]): boolean {
  return filter.value === "ALL" || filter.categories.includes(categoryOf(rule));
}

/** 目录中没有记录的规则状态码计数：新增状态出现时必须看得见，不能静默消失。 */
const unknownStatusCounts = computed(() => {
  const counts: Record<string, number> = {};
  for (const rule of props.rules) {
    if (categoryOf(rule) === "UNKNOWN") {
      counts[rule.status] = (counts[rule.status] ?? 0) + 1;
    }
  }
  return counts;
});

const unknownStatusTotal = computed(() => Object.values(unknownStatusCounts.value).reduce((sum, item) => sum + item, 0));

const rows = computed(() => {
  const selected = filters.find((item) => item.value === statusFilter.value)!;
  const query = search.value.trim().toLocaleLowerCase();
  // 只有服务端确实返回了节点信息时才按阶段筛选：否则每一条规则的 node 都是 null，
  // 筛选会把全部结果清空——用户从「跨文件校验」工作区进来会看到一个空列表，
  // 而真实结果明明存在。缺少节点信息由上面的提示条说明，不在这里静默丢结果。
  const selectedNode = nodeMetaAvailable.value ? nodeFilter.value : null;
  return props.rules.filter((rule) => {
    const text = [
      rule.name,
      standardText(rule),
      rule.message,
      manualNote(rule),
      materialsOf(rule),
      ...valueSourcesOf(rule).flatMap((item) => [
        item.material_name,
        item.field_name,
        item.filename,
        item.raw_value,
        item.message,
        formatInputValue(item),
      ]),
      ...(rule.evidence ?? []).flatMap((item) => [item.material_name, item.filename, item.quote_text]),
    ]
      .join(" ")
      .toLocaleLowerCase();
    return (
      matchesFilter(rule, selected) &&
      (!selectedNode || nodeOf(rule) === selectedNode) &&
      (!query || text.includes(query))
    );
  });
});

/** 已选节点筛选时，服务端没有返回节点的结果数量：不能静默少显示。 */
const nodeFilterUnavailableCount = computed(() => {
  if (!nodeFilter.value) {
    return 0;
  }
  return props.rules.filter((rule) => nodeOf(rule) === null).length;
});

function clearFilters(): void {
  search.value = "";
  statusFilter.value = "ALL";
  nodeFilter.value = null;
}

/* ---- 详情抽屉 ---- */

const detailRule = ref<AuditRule | null>(null);

function openDetail(rule: AuditRule): void {
  detailRule.value = rule;
}

function closeDetail(): void {
  detailRule.value = null;
}

const detailPayload = computed(() => (detailRule.value ? payloadOf(detailRule.value) : {}));

/** 详情抽屉的取值来源：与列表同一份事实，不重新查库也不重新计算。 */
const detailValues = computed(() => (detailRule.value ? valueSourcesOf(detailRule.value) : []));
const detailMaterialRows = computed(() => (detailRule.value ? materialRowsOf(detailRule.value) : []));

/** 抽屉里的一次性读取：避免在模板里反复调用 payloadOf。 */
const detailCapability = computed(() => (detailRule.value ? capabilityOf(detailRule.value) : null));
const detailNode = computed(() => (detailRule.value ? nodeOf(detailRule.value) : null));
const detailManualNote = computed(() => (detailRule.value ? manualNote(detailRule.value) : null));
const detailManualStatus = computed(() => (detailRule.value ? manualEffectiveStatus(detailRule.value) : null));
const detailReason = computed(() => (detailRule.value ? reasonText(detailRule.value) : null));
const detailStandardText = computed(() => (detailRule.value ? standardText(detailRule.value) : ""));
const detailStandardDetail = computed(() => (detailRule.value ? standardDetail(detailRule.value) : ""));
const detailComparison = computed(() => (detailRule.value ? comparisonOf(detailRule.value) : null));
const detailEvidenceNumbers = computed(() => (detailRule.value ? evidenceNumbers(detailRule.value) : []));
const detailIsReviewed = computed(() => (detailRule.value ? manuallyReviewed(detailRule.value) : false));
const detailTypeNotice = computed(() => (detailRule.value ? materialTypeNotice(detailRule.value) : null));
const detailRetrievalCandidates = computed<JsonValue[]>(() => {
  const retrieval = detailPayload.value.retrieval;
  if (!retrieval || typeof retrieval !== "object" || Array.isArray(retrieval)) {
    return [];
  }
  const candidates = (retrieval as Record<string, JsonValue>).candidates;
  return Array.isArray(candidates) ? candidates : [];
});
</script>

<template>
  <StateBlock
    v-if="!rules.length"
    kind="empty"
    title="当前还没有规则结果"
    :description="
      unconfiguredNodes.length
        ? `本模板未配置以下检查类别：${unconfiguredNodes.map((node) => nodeLabel(node)).join('、')}。未配置的检查不会产生结果，也不代表检查通过。`
        : '规则节点执行后，这里会显示每条检查的标准、实际比较与结论；未执行的检查不会显示成通过。'
    "
  />

  <div v-else class="rule-list">
    <div class="rule-toolbar">
      <el-input v-model="search" clearable placeholder="搜索审核事项、材料或原文" aria-label="搜索审核结果" class="rule-search" />
      <el-radio-group v-model="statusFilter" aria-label="按审核结论筛选">
        <el-radio-button v-for="filter in filters" :key="filter.value" :value="filter.value">
          {{ filter.label }}（{{ rules.filter((rule) => matchesFilter(rule, filter)).length }}）
        </el-radio-button>
      </el-radio-group>
      <el-select
        v-if="nodeMetaAvailable"
        v-model="nodeFilter"
        clearable
        placeholder="全部校验阶段"
        aria-label="按校验阶段筛选"
        class="rule-node-filter"
      >
        <el-option value="INTERNAL_VALIDATE" :label="nodeLabel('INTERNAL_VALIDATE')" />
        <el-option value="CROSS_VALIDATE" :label="nodeLabel('CROSS_VALIDATE')" />
        <el-option value="MATERIAL_CHECK" :label="nodeLabel('MATERIAL_CHECK')" />
        <el-option value="DOCUMENT_PREPARE" :label="nodeLabel('DOCUMENT_PREPARE')" />
        <el-option value="FIELD_EXTRACTION" :label="nodeLabel('FIELD_EXTRACTION')" />
        <el-option value="RISK_RATING" :label="nodeLabel('RISK_RATING')" />
      </el-select>
    </div>

    <el-alert
      v-if="nodeFilter && !nodeMetaAvailable"
      type="warning"
      show-icon
      :closable="false"
      title="按校验阶段筛选当前不可用"
    >
      服务端在规则结果里没有返回该条检查所属的节点，页面无法按阶段筛选，也不会用规则名或材料推断它属于哪个阶段。
      需要后端在规则结果里补 `node` 字段；补齐前请用「审核事项 / 材料 / 原文」搜索或直接用任务详情的节点流程条定位。
    </el-alert>
    <el-alert
      v-else-if="nodeFilter && nodeMetaAvailable && nodeFilterUnavailableCount"
      type="warning"
      show-icon
      :closable="false"
      :title="`有 ${nodeFilterUnavailableCount} 条结果没有返回所属节点，未计入本次筛选`"
    />

    <el-alert
      v-if="catalogError"
      type="warning"
      show-icon
      :closable="false"
      :title="`规则状态口径读取失败：${catalogError}`"
    >
      分档暂按内置口径显示；未识别的状态码会在下方单独列出，不会被静默丢弃。
    </el-alert>

    <el-alert
      v-if="unknownStatusTotal"
      type="warning"
      show-icon
      :closable="false"
      :title="`有 ${unknownStatusTotal} 条结果的状态不在服务端状态目录中（${Object.entries(unknownStatusCounts).map(([status, count]) => `${status} ${count} 条`).join('、')}），它们只在「全部」里出现，既不计入已判定也不计入尚未判定。`"
    />

    <p class="text-hint">
      共 {{ rules.length }} 条检查结果；已判定条数不代表整个任务已审核通过。点击某一条查看标准、双方取值与执行详情。
    </p>

    <el-empty v-if="!rows.length" description="没有符合筛选条件的检查结果">
      <el-button @click="clearFilters">清除筛选</el-button>
    </el-empty>

    <!-- 紧凑表格：几十条规则也能一眼扫完，需要细看的再打开抽屉 -->
    <el-table v-else :data="rows" border stripe size="small" @row-click="openDetail">
      <el-table-column label="审核事项" min-width="180" show-overflow-tooltip>
        <template #default="{ row }">
          <span class="cell-strong">{{ row.name }}</span>
          <div v-if="nodeOf(row)" class="table-note">{{ nodeLabel(nodeOf(row)) }}</div>
        </template>
      </el-table-column>

      <el-table-column label="原文内容" min-width="200" show-overflow-tooltip>
        <template #default="{ row }">{{ columnText(compactValues(row), "raw", row) }}</template>
      </el-table-column>

      <el-table-column label="标准化结果" min-width="160" show-overflow-tooltip>
        <template #default="{ row }">{{ columnText(compactValues(row), "normalized", row) }}</template>
      </el-table-column>

      <el-table-column label="审核标准" min-width="200" show-overflow-tooltip>
        <template #default="{ row }">
          <span>{{ standardText(row) }}</span>
          <div v-if="standardDetail(row)" class="table-note">{{ standardDetail(row) }}</div>
        </template>
      </el-table-column>

      <el-table-column label="判断" width="120">
        <template #default="{ row }">
          <StatusBadge :status="row.status" :tone="statusTone(row.status)" />
        </template>
      </el-table-column>

      <el-table-column label="原因" min-width="220" show-overflow-tooltip>
        <template #default="{ row }">
          <span v-if="manuallyReviewed(row)">{{ manualNote(row) || "本次人工复核没有记录说明" }}</span>
          <span v-else>{{ row.message || "服务端没有返回判定说明" }}</span>
          <div v-if="reasonText(row)" class="table-note">无法判断的具体原因：{{ reasonText(row) }}</div>
        </template>
      </el-table-column>

      <el-table-column label="操作" width="100" fixed="right" align="center">
        <template #default="{ row }">
          <el-button link type="primary" size="small" @click.stop="openDetail(row)">查看详情</el-button>
        </template>
      </el-table-column>
    </el-table>
  </div>

  <!-- 详情抽屉：标准 → 取值 → 实际比较 → 结论原因 → 人工复核 → 技术执行详情 -->
  <DrawerPanel
    :open="Boolean(detailRule)"
    :title="detailRule?.name ?? '检查详情'"
    description="本条检查的审核标准、实际取值、比较过程与结论；技术信息收在最下方。"
    size="60%"
    @close="closeDetail"
  >
    <template v-if="detailRule">
      <div class="detail-head">
        <StatusBadge :status="detailRule.status" :tone="statusTone(detailRule.status)" size="default" />
        <span v-if="detailNode" class="text-hint">所属阶段：{{ nodeLabel(detailNode) }}</span>
        <span v-if="detailTypeNotice" class="text-hint">{{ detailTypeNotice }}</span>
      </div>

      <!-- 1. 审核标准 -->
      <h4 class="app-section-title">审核标准</h4>
      <div class="detail-block">{{ detailStandardText }}</div>
      <div v-if="detailStandardDetail" class="text-muted detail-block">{{ detailStandardDetail }}</div>

      <!-- 2. 原文内容与标准化结果 -->
      <h4 class="app-section-title">原文内容与标准化结果</h4>
      <div v-if="!detailValues.length && !detailMaterialRows.length" class="text-muted detail-block">
        本条结果未记录取值内容，请结合判定说明与原文依据核对。
      </div>
      <el-table v-if="detailMaterialRows.length" :data="detailMaterialRows" size="small" border empty-text="没有材料级输入">
        <el-table-column label="材料" min-width="150">
          <template #default="{ row }">{{ row.material_name || "未知材料" }}</template>
        </el-table-column>
        <el-table-column label="文件名" min-width="180" show-overflow-tooltip>
          <template #default="{ row }">{{ row.filename || "—" }}</template>
        </el-table-column>
        <el-table-column label="本份判定" width="120">
          <template #default="{ row }">
            <StatusBadge v-if="row.status" :status="row.status" />
            <span v-else>—</span>
          </template>
        </el-table-column>
        <el-table-column label="说明" min-width="220" show-overflow-tooltip>
          <template #default="{ row }">{{ row.message || "—" }}</template>
        </el-table-column>
      </el-table>
      <el-table v-else-if="detailValues.length" :data="detailValues" size="small" border>
        <el-table-column label="材料 · 字段" min-width="200">
          <template #default="{ row }">{{ row.material_name || "未知材料" }} · {{ row.field_name || row.field_key }}</template>
        </el-table-column>
        <el-table-column label="原文内容" min-width="200">
          <template #default="{ row }">
            <span v-if="row.raw_value" class="detail-quote">{{ row.raw_value }}</span>
            <span v-else class="text-muted">未取得原文</span>
          </template>
        </el-table-column>
        <el-table-column label="标准化结果" min-width="150">
          <template #default="{ row }">
            <span v-if="row.normalized_value !== null && row.normalized_value !== undefined">
              {{ formatBusinessValue(row.normalized_value) }}
            </span>
            <span v-else class="text-muted">未形成标准化结果</span>
          </template>
        </el-table-column>
        <el-table-column label="取值状态" width="120">
          <template #default="{ row }">
            <StatusBadge v-if="row.status" :status="row.status" />
            <span v-else>—</span>
          </template>
        </el-table-column>
      </el-table>
      <div v-if="!detailValues.length && detailMaterialRows.length === 0" class="text-hint">
        本条结果没有可展示的取值内容。
      </div>

      <!-- 3. 实际比较：跨材料并排展示双方内容；金额与日期给出真实算式 -->
      <h4 class="app-section-title">实际比较</h4>
      <CrossComparisonPanel
        v-if="detailComparison"
        :comparison="detailComparison"
        :rule-name="detailRule.name"
      />
      <div v-else class="text-muted detail-block">
        本条结果没有记录比较过程（抽取类与材料类检查按材料逐份判定，见上方取值表）。
      </div>

      <!-- 4. 判断结论与原因 -->
      <h4 class="app-section-title">{{ detailIsReviewed ? "人工复核后的结论" : "判断结论" }}</h4>
      <div class="detail-block">
        <StatusBadge
          :status="detailManualStatus ?? detailRule.status"
          :tone="statusTone(detailManualStatus ?? detailRule.status)"
        />
        <span class="detail-block__text">
          {{ detailIsReviewed ? detailManualNote || "本次人工复核没有记录说明" : detailRule.message || "服务端没有返回判定说明" }}
        </span>
      </div>
      <div v-if="detailIsReviewed" class="text-muted detail-block">
        系统原说明：{{ detailRule.message || "未记录" }}
      </div>
      <div v-if="detailReason" class="text-muted detail-block">无法判断的具体原因：{{ detailReason }}</div>

      <!-- 5. 原文依据 -->
      <h4 class="app-section-title">原文依据</h4>
      <div v-if="!(detailRule.evidence ?? []).length" class="text-muted detail-block">
        本条结果未保存原文片段，请结合上方取值内容与判定说明核对。
      </div>
      <div v-for="source in detailRule.evidence ?? []" :key="source.evidence_no" class="rule-source">
        <div class="text-muted">
          {{ source.material_name || "来源材料未记录" }}
          <template v-if="source.filename"> · {{ source.filename }}</template>
        </div>
        <blockquote class="rule-quote">{{ source.quote_text || "本条依据没有记录原文文字" }}</blockquote>
      </div>

      <!-- 6. 人工复核 -->
      <h4 class="app-section-title">人工复核</h4>
      <div class="text-muted detail-block">
        复核状态：{{ detailIsReviewed || detailRule.status === "REVIEWED" ? "已经人工复核" : detailRule.status === "PENDING_REVIEW" ? "等待人工复核" : "未经人工复核" }}
      </div>
      <div v-if="detailManualNote" class="detail-block">{{ detailManualNote }}</div>
      <div v-if="detailManualStatus" class="detail-block">
        复核后有效状态：<StatusBadge :status="detailManualStatus" />
      </div>

      <!-- 7. 技术执行详情：页码、编号、能力与检索范围等辅助信息 -->
      <el-collapse class="block-gap">
        <el-collapse-item title="技术执行详情（辅助信息）" name="tech">
          <div class="text-muted">适用条件：{{ applicabilityText(detailRule) }}</div>
          <div class="text-muted">判定方式：{{ routeText(detailRule) }}</div>
          <div class="text-muted">
            本条结果的取值是否保存了原文片段：{{ (detailRule.evidence ?? []).length ? `已保存 ${(detailRule.evidence ?? []).length} 段原文` : "未保存原文片段" }}
          </div>
          <div class="text-muted">
            判定能力：
            <template v-if="detailCapability">
              {{ detailCapability.label || detailCapability.capability_id || "未记录" }}
              <span class="mono">（{{ detailCapability.capability_id }} · v{{ detailCapability.version }}）</span>
              <template v-if="detailCapability.known === false">｜该能力不在系统能力目录中</template>
              <template v-else-if="detailCapability.implemented === false">
                ｜当前未实现：{{ detailCapability.unimplemented_reason || "未说明原因" }}
              </template>
            </template>
            <template v-else>本条结果未记录判定能力</template>
          </div>
          <div v-if="detailRule.rule_key" class="mono">规则键：{{ detailRule.rule_key }}</div>
          <div v-if="detailEvidenceNumbers.length">
            <div class="text-muted">关联证据编号：</div>
            <div v-for="number in detailEvidenceNumbers" :key="number" class="mono">{{ number }}</div>
          </div>
          <div v-if="detailRetrievalCandidates.length">
            <div class="text-muted">检索范围与候选（相似度仅用于排序，不构成结论）：</div>
            <el-table :data="detailRetrievalCandidates" size="small" border>
              <el-table-column prop="subject_name" label="识别到的印章主体" min-width="160" show-overflow-tooltip />
              <el-table-column prop="material_name" label="归属材料" min-width="120" show-overflow-tooltip />
              <el-table-column prop="similarity" label="相似度" width="90" />
              <el-table-column label="候选身份" width="170">
                <template #default="{ row }">
                  <span class="mono">{{ row.index_no ?? "—" }}#{{ row.chunk_no ?? "—" }}#{{ row.source_entry_no ?? "—" }}</span>
                </template>
              </el-table-column>
            </el-table>
          </div>
        </el-collapse-item>
      </el-collapse>
    </template>
  </DrawerPanel>
</template>

<style scoped>
.rule-list {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.rule-toolbar {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 12px;
}

.rule-search {
  width: min(100%, 320px);
}

.rule-node-filter {
  width: 180px;
}

.rule-toolbar :deep(.el-radio-group) {
  flex-wrap: wrap;
  gap: 4px 0;
}

.rule-source + .rule-source {
  margin-top: 12px;
}

.rule-quote {
  margin: 6px 0 0;
  padding: 8px 12px;
  border-left: 3px solid var(--el-border-color);
  background: var(--el-fill-color-light);
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  line-height: 1.7;
}

.detail-head {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
  margin-bottom: 8px;
}

.detail-block {
  margin-bottom: 8px;
  line-height: 1.7;
}

.detail-block__text {
  margin-left: 8px;
}

.detail-quote {
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}

.cell-strong {
  font-weight: 600;
}
</style>
