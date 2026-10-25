<script setup lang="ts">
import { computed, ref, watch } from "vue";

import type { AuditRule, RuleStatusItem } from "@/api/contracts";
import { getRuleStatusCatalog } from "@/api/system";
import AuditRuleDetailDrawer from "@/components/audit/AuditRuleDetailDrawer.vue";
import StateBlock from "@/components/StateBlock.vue";
import StatusBadge from "@/components/StatusBadge.vue";
import { isOutputRule, isOutputRuleGroup, ruleGroupOf } from "@/constants/auditRuleGroups";
import { auditResultSummary, AUDIT_RESULT_CATEGORIES, UNDECIDED_RESULT_STATUSES } from "@/constants/auditStatus";
import { EXECUTION_MODE_OPTIONS, executionModeLabel } from "@/constants/executionModes";
import { statusTone } from "@/constants/labels";
import { quoteInlineText } from "@/utils/quoteText";
import { compareNaturalLabels } from "@/utils/naturalSort";
import {
  formatInputValue,
  manualNote,
  manuallyReviewed,
  materialsOf,
  payloadOf,
  reasonText,
  resultStatusLabel,
  ruleCodeOf,
  standardText,
  valueSourcesOf,
} from "@/utils/auditRulePresentation";

/** 规则结果列表：详情内容与纯展示投影分别维护。 */
const props = defineProps<{ rules: AuditRule[] }>();

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
 * 服务端规则状态目录。分档以它为准，页面不再本地维护一份名单——
 * 本地名单与后端漂移过（`NOT_APPLICABLE`、`SKIPPED` 曾被漏出分档）。
 *
 * 目录读取中或读取失败时退回下面的只读兜底分档：兜底只在拿不到目录时生效，
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
const FALLBACK_CATEGORY: Readonly<Record<string, string>> = AUDIT_RESULT_CATEGORIES;

/**
 * 筛选分档。它必须覆盖服务端可能给出的全部状态码：
 *   「未执行」= 应当执行但没有跑完；「不适用」= 适用条件明确不成立，本次就不该查；
 *   「已跳过」= 当前场景没配置这类检查。三者都不是通过，也不能互相冒充。
 * 在目录中找不到的码（新增或历史遗留）不会被静默丢掉——它进「未识别」，并在下方单独提示。
 */
const filters = [
  { value: "ALL", label: "全部", categories: [] as string[] },
  { value: "DECIDED", label: "已判定", categories: ["DECIDED"] },
  { value: "COMPUTED", label: "已完成产出", categories: ["COMPUTED"] },
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
  return props.rules.filter((rule) => {
    const text = [
      ruleCodeOf(rule),
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
      ...rule.evidence.flatMap((item) => [item.material_name, item.document_name, quoteInlineText(item.quote_text)]),
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
        undecided: judged.filter((rule) => UNDECIDED_RESULT_STATUSES.has(rule.status)).length,
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

watch([search, statusFilter, modeFilter, sheetFilter], () => {
  expandedGroups.value = filtering.value
    ? groups.value.map((group) => group.label)
    : defaultExpandedGroups();
});

/* 详情抽屉 */

const detailRule = ref<AuditRule | null>(null);

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

          <el-table-column label="关联材料" width="128" align="center">
            <template #default="{ row }">{{ materialsOf(row) || '—' }}</template>
          </el-table-column>

          <el-table-column label="结论" width="120" align="center">
            <template #default="{ row }">
              <StatusBadge :status="row.status" :label="resultStatusLabel(row)" :tone="statusTone(row.status)" />
            </template>
          </el-table-column>

          <el-table-column label="判断依据" min-width="400" align="center">
            <template #default="{ row }">
              <div class="rule-reason">
                <span v-if="manuallyReviewed(row)">{{ manualNote(row) || "本次人工复核没有记录说明" }}</span>
                <span v-else>{{ auditResultSummary(row.status, row.message, payloadOf(row)) }}</span>
              </div>
              <div v-if="reasonText(row)" class="table-note rule-reason">{{ reasonText(row) }}</div>
            </template>
          </el-table-column>

          <el-table-column label="操作" width="96" class-name="table-actions" fixed="right" align="center">
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
