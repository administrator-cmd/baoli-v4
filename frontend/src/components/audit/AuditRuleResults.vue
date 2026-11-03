<script setup lang="ts">
import { computed, ref, watch } from "vue";

import type { AuditRule } from "@/api/contracts";
import AuditRuleDetailDrawer from "@/components/audit/AuditRuleDetailDrawer.vue";
import StateBlock from "@/components/StateBlock.vue";
import StatusBadge from "@/components/StatusBadge.vue";
import { isOutputRule, isOutputRuleGroup, ruleGroupOf } from "@/constants/auditRuleGroups";
import { auditResultSummary, AUDIT_RESULT_CATEGORIES, COMPLETED_RESULT_STATUSES } from "@/constants/auditStatus";
import { EXECUTION_MODE_OPTIONS, executionModeLabel } from "@/constants/executionModes";
import {
  categoryLabel,
  isStatusVocabularyLoaded,
  statusCategoryOf,
  statusVocabularyError,
} from "@/constants/statusVocabulary";
import { quoteInlineText } from "@/utils/quoteText";
import { compareNaturalLabels } from "@/utils/naturalSort";
import {
  formatInputValue,
  materialsOf,
  sourceFilesOf,
  payloadOf,
  ruleCodeOf,
  standardText,
  valueSourcesOf,
} from "@/utils/auditRulePresentation";

/** 规则结果列表：详情内容与纯展示投影分别维护。 */
const props = defineProps<{ rules: AuditRule[]; filter?: string }>();
const emit = defineEmits<{ "update:filter": [value: string] }>();

/* 筛选 */

const search = ref("");
const statusFilter = ref("ALL");
const modeFilter = ref("");
const sheetFilter = ref("");
/** 筛选项取自已有的来源工作表，不将展示用的缺省分组当成 Sheet。 */
const sheetOptions = computed(() => Array.from(new Set(
  props.rules.map((rule) => rule.source_sheet).filter((sheet): sheet is string => Boolean(sheet)),
)).sort(compareNaturalLabels));
/**
 * 审核状态词表由后端统一提供（main.ts 启动时加载）。分档以它为准，页面不再维护名单——
 * 本地名单与后端漂移过（`NOT_APPLICABLE`、`SKIPPED` 曾被漏出分档）。
 *
 * 词表尚未加载或读取失败时退回下面的只读兜底分档；词表已加载但状态码不在其中时，
 * 结果进入「未识别」并单独提示，不能被兜底表伪装成正常分档。
 */
const vocabularyError = computed(() => statusVocabularyError());

/** 与服务端 STATUS_CATEGORIES 同口径的兜底分档；仅用于词表不可用时。 */
const FALLBACK_CATEGORY: Readonly<Record<string, string>> = AUDIT_RESULT_CATEGORIES;

interface RuleFilter {
  value: string;
  label: string;
  categories: string[];
  /** 精确到状态码的筛选（例如只看 ABNORMAL）；与类别筛选二选一。 */
  statuses?: string[];
}

/**
 * 筛选分档。它必须覆盖服务端可能给出的全部状态码：
 *   「未执行」= 应当执行但没有跑完；「不适用」= 适用条件明确不成立，本次就不该查；
 *   「已跳过」= 当前场景没配置这类检查。三者都不是通过，也不能互相冒充。
 * 在词表中找不到的码（新增或历史遗留）不会被静默丢掉——它进「未识别」，并在下方单独提示。
 */
const filters = computed<RuleFilter[]>(() => [
  { value: "ALL", label: "全部", categories: [] },
  { value: "ABNORMAL", label: "异常", categories: [], statuses: ["ABNORMAL"] },
  { value: "EVIDENCE_INSUFFICIENT", label: "证据不足", categories: [], statuses: ["EVIDENCE_INSUFFICIENT"] },
  { value: "INCOMPLETE", label: "未完成 / 未确认", categories: [] },
  { value: "DECIDED", label: categoryLabel("DECIDED") ?? "已判定", categories: ["DECIDED"] },
  { value: "COMPUTED", label: categoryLabel("COMPUTED") ?? "已产出（无结论）", categories: ["COMPUTED"] },
  { value: "UNDECIDED", label: categoryLabel("UNDECIDED") ?? "尚未判定", categories: ["UNDECIDED"] },
  { value: "NOT_APPLICABLE", label: categoryLabel("NOT_APPLICABLE") ?? "不适用", categories: ["NOT_APPLICABLE"] },
]);

/** 词表可用时以词表为准；词表未加载时用兜底表，两者都没有才判为未识别。 */
function categoryOf(rule: AuditRule): string {
  const category = statusCategoryOf("rule", rule.status);
  if (category) {
    return category;
  }
  if (!isStatusVocabularyLoaded()) {
    return FALLBACK_CATEGORY[rule.status] ?? "UNKNOWN";
  }
  return "UNKNOWN";
}

function matchesFilter(rule: AuditRule, filter: RuleFilter): boolean {
  if (filter.value === "ALL") return true;
  if (filter.value === "INCOMPLETE") return !COMPLETED_RESULT_STATUSES.has(rule.status);
  if (filter.statuses?.length) return filter.statuses.includes(rule.status);
  return filter.categories.includes(categoryOf(rule));
}

watch(() => props.filter, (value) => {
  if (value && filters.value.some((item) => item.value === value)) {
    statusFilter.value = value;
  }
}, { immediate: true });
watch(statusFilter, (value) => emit("update:filter", value));

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
  const selected = filters.value.find((item) => item.value === statusFilter.value)!;
  const query = search.value.trim().toLocaleLowerCase();
  return props.rules.filter((rule) => {
    const text = [
      ruleCodeOf(rule),
      rule.name,
      standardText(rule),
      rule.message,
      materialsOf(rule),
      ...valueSourcesOf(rule).flatMap((item) => [
        item.material_name,
        item.field_name,
        item.filename,
        item.raw_value,
        item.message,
        formatInputValue(item),
      ]),
      ...rule.evidence.flatMap((item) => [item.material_name, item.document_name, item.quote_label, item.quote_header, item.quote_period, quoteInlineText(item.quote_text)]),
    ]
      .join(" ")
      .toLocaleLowerCase();
    return (
      matchesFilter(rule, selected) &&
      (!modeFilter.value || (rule.execution_mode || "UNKNOWN") === modeFilter.value) &&
      (!sheetFilter.value || rule.source_sheet === sheetFilter.value) &&
      (!query || text.includes(query))
    );
  }).sort((left, right) => compareNaturalLabels(ruleCodeOf(left), ruleCodeOf(right)));
});

function clearFilters(): void {
  search.value = "";
  statusFilter.value = "ALL";
  modeFilter.value = "";
  sheetFilter.value = "";
}

function applyFilter(value: string): void {
  clearFilters();
  statusFilter.value = value;
}
defineExpose({ applyFilter });

/* 分组：按任务冻结的来源工作表，不写死场景或规则编号 */

interface RuleGroupView {
  label: string;
  rows: AuditRule[];
  abnormal: number;
  undecided: number;
  riskOnly: boolean;
}

const groupOrder = computed(() => Array.from(new Set(props.rules.map(ruleGroupOf))).sort(compareNaturalLabels));

const groups = computed<RuleGroupView[]>(() =>
  groupOrder.value
    .map((label) => {
      const matched = rows.value.filter((rule) => ruleGroupOf(rule) === label);
      const judged = matched.filter((rule) => !isOutputRule(rule));
      return {
        label,
        rows: matched,
        abnormal: judged.filter((rule) => rule.status === "ABNORMAL").length,
        undecided: judged.filter((rule) => categoryOf(rule) === "UNDECIDED").length,
        riskOnly: judged.length > 0 && judged.every((rule) => rule.is_risk_rule),
      };
    })
    .filter((group) => group.rows.length),
);

/** 默认展开审核规则组；整组都是抽取/报告产出时收起，组头计数仍可见。 */
function defaultExpandedGroups(): string[] {
  const byGroup = new Map<string, AuditRule[]>();
  for (const rule of props.rules) {
    const label = ruleGroupOf(rule);
    byGroup.set(label, [...(byGroup.get(label) ?? []), rule]);
  }
  return Array.from(byGroup.entries())
    .filter(([, rules]) => !isOutputRuleGroup(rules))
    .map(([label]) => label);
}

const expandedGroups = ref<string[]>(defaultExpandedGroups());
const filtering = computed(() => Boolean(search.value.trim()) || statusFilter.value !== "ALL" || Boolean(modeFilter.value) || Boolean(sheetFilter.value));

watch([search, statusFilter, modeFilter, sheetFilter, () => props.rules], () => {
  expandedGroups.value = filtering.value
    ? groups.value.map((group) => group.label)
    : defaultExpandedGroups();
});

/* 详情抽屉 */

const detailRule = ref<AuditRule | null>(null);

watch(() => props.rules, (rules) => {
  if (detailRule.value) detailRule.value = rules.find((rule) => rule.rule_key === detailRule.value?.rule_key) ?? null;
});

function openDetail(rule: AuditRule): void {
  detailRule.value = rule;
}
</script>

<template>
  <StateBlock
    v-if="!rules.length"
    kind="empty"
    title="当前还没有规则结果"
    description="规则执行后，这里会显示每条生效规则的实际结果与结论。"
  />

  <div v-else class="rule-list">
    <div class="rule-toolbar">
      <el-input v-model="search" clearable placeholder="搜索编号、审核事项或原因" aria-label="搜索审核结果" class="rule-search" />
      <el-select v-model="statusFilter" aria-label="按审核结论筛选" class="rule-status-filter">
        <el-option v-for="filter in filters" :key="filter.value" :value="filter.value"
          :label="`${filter.label}（${rules.filter((rule) => matchesFilter(rule, filter)).length}）`" />
      </el-select>
      <el-select v-model="modeFilter" clearable aria-label="按执行方式筛选" class="rule-mode-filter" placeholder="全部执行方式">
        <el-option v-for="item in EXECUTION_MODE_OPTIONS" :key="item.value" :value="item.value" :label="item.label" />
      </el-select>
      <el-select v-model="sheetFilter" clearable filterable aria-label="按来源 Sheet 筛选" class="rule-sheet-filter" placeholder="全部 Sheet">
        <el-option v-for="sheet in sheetOptions" :key="sheet" :value="sheet" :label="sheet" />
      </el-select>
      <span class="text-hint rule-count">显示 {{ rows.length }} / {{ rules.length }} 项</span>
    </div>

    <el-alert
      v-if="vocabularyError"
      type="warning"
      show-icon
      :closable="false"
      :title="`规则状态口径读取失败：${vocabularyError}`"
    >
      分档暂按内置口径显示；未识别的状态码会在下方单独列出，不会被静默丢弃。
    </el-alert>

    <el-alert
      v-if="unknownStatusTotal"
      type="warning"
      show-icon
      :closable="false"
      :title="`有 ${unknownStatusTotal} 条结果的状态不在服务端状态词表中（${Object.entries(unknownStatusCounts).map(([status, count]) => `${status} ${count} 条`).join('、')}），它们只在「全部」里出现，既不计入已判定也不计入尚未判定。`"
    />

    <el-empty v-if="!rows.length" description="没有符合筛选条件的检查结果">
      <el-button @click="clearFilters">清除筛选</el-button>
    </el-empty>

    <el-collapse v-else v-model="expandedGroups" class="rule-groups">
      <el-collapse-item v-for="group in groups" :key="group.label" :name="group.label">
        <template #title>
          <div class="rule-group-head">
            <span class="rule-group-title">{{ group.label }}</span>
            <span class="text-hint">{{ group.rows.length }} 条</span>
            <span v-if="group.abnormal" class="rule-group-count rule-group-count--danger">{{ group.riskOnly ? '已确认风险' : '异常' }} {{ group.abnormal }}</span>
            <span v-if="group.undecided" class="rule-group-count rule-group-count--warning">未判定 {{ group.undecided }}</span>
          </div>
        </template>

        <el-table :data="group.rows" border class="audit-result-table result-table" @row-click="openDetail">
          <el-table-column label="审核事项" width="220" align="center">
            <template #default="{ row }">
              <span class="cell-strong">{{ row.name }}</span>
              <div class="table-note">{{ executionModeLabel(row.execution_mode) }}</div>
              <div v-if="ruleCodeOf(row)" class="table-note mono">{{ ruleCodeOf(row) }}</div>
            </template>
          </el-table-column>

          <el-table-column label="来源文件" min-width="200" show-overflow-tooltip>
            <template #default="{ row }">{{ sourceFilesOf(row).join('、') || '未记录来源文件' }}</template>
          </el-table-column>

          <!-- 结论列要容纳最长的状态文案「已产出（无结论）」，列宽不足时标签会向右溢出压到列边界。 -->
          <el-table-column label="结论" width="184" align="center">
            <template #default="{ row }">
              <StatusBadge domain="rule" :status="row.status" />
            </template>
          </el-table-column>

          <el-table-column label="判断依据" min-width="400" align="center">
            <template #default="{ row }">
              <div class="rule-reason">
                <span>{{ auditResultSummary(row.status, row.message, payloadOf(row)) }}</span>
              </div>
            </template>
          </el-table-column>

          <el-table-column label="操作" width="112" class-name="table-actions" fixed="right" align="center">
            <template #default="{ row }">
              <el-button link type="primary" @click.stop="openDetail(row)">查看详情</el-button>
            </template>
          </el-table-column>
        </el-table>
      </el-collapse-item>
    </el-collapse>
  </div>

  <AuditRuleDetailDrawer :rule="detailRule" @close="detailRule = null" />

</template>

<style scoped>
.rule-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.rule-toolbar {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
}

.rule-search {
  width: min(100%, 320px);
}

.rule-status-filter {
  width: 190px;
}

.rule-mode-filter {
  width: 150px;
}

.rule-sheet-filter {
  width: 220px;
}

.rule-count {
  margin-left: auto;
  font-size: var(--el-font-size-base);
}

.rule-groups :deep(.el-collapse-item__header) {
  height: auto;
  min-height: 40px;
  padding: 0 4px;
}

.rule-groups :deep(.el-collapse-item__content) {
  padding-bottom: 4px;
}

.rule-group-head {
  display: flex;
  flex: 1;
  flex-wrap: wrap;
  align-items: center;
  gap: 12px;
  padding-right: 8px;
}

.rule-group-title {
  font-size: var(--el-font-size-base);
  font-weight: 600;
  color: var(--el-text-color-primary);
}

.rule-group-count,
.rule-group-head .text-hint {
  font-size: var(--el-font-size-base);
}

.rule-group-count--danger {
  color: var(--el-color-danger);
}

.rule-group-count--warning {
  color: var(--el-color-warning);
}

.result-table :deep(.el-table__cell) {
  padding: 6px 0;
  vertical-align: middle;
}

.result-table :deep(.cell) {
  line-height: 1.35;
  overflow-wrap: anywhere;
}

.result-table :deep(.el-table__row) {
  cursor: pointer;
}

.rule-reason {
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
  white-space: pre-wrap;
}

.cell-strong {
  font-weight: 600;
}
</style>
