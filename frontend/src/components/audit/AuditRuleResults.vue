<script setup lang="ts">
import { computed, ref } from "vue";

import type { AuditRule, RuleStatusItem } from "@/api/contracts";
import { getRuleStatusCatalog } from "@/api/system";
import AuditRuleDetailDrawer from "@/components/audit/AuditRuleDetailDrawer.vue";
import StateBlock from "@/components/StateBlock.vue";
import StatusBadge from "@/components/StatusBadge.vue";
import { auditResultSummary, AUDIT_RESULT_CATEGORIES } from "@/constants/auditStatus";
import { statusTone } from "@/constants/labels";
import {
  formatInputValue,
  manualNote,
  manuallyReviewed,
  materialsOf,
  payloadOf,
  reasonText,
  resultStatusLabel,
  standardText,
  valueSourcesOf,
} from "@/utils/auditRulePresentation";

/** 规则结果列表：详情内容与纯展示投影分别维护。 */
const props = defineProps<{ rules: AuditRule[] }>();

/* 筛选 */

const search = ref("");
const statusFilter = ref("ALL");
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
      (!query || text.includes(query))
    );
  });
});

function clearFilters(): void {
  search.value = "";
  statusFilter.value = "ALL";
}

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
      <el-input v-model="search" clearable placeholder="搜索审核事项或原因" aria-label="搜索审核结果" class="rule-search" />
      <el-select v-model="statusFilter" aria-label="按审核结论筛选" class="rule-status-filter">
        <el-option v-for="filter in filters" :key="filter.value" :value="filter.value"
          :label="`${filter.label}（${rules.filter((rule) => matchesFilter(rule, filter)).length}）`" />
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

    <!-- 紧凑表格：几十条规则也能一眼扫完，需要细看的再打开抽屉 -->
    <el-table v-else :data="rows" class="result-table" @row-click="openDetail">
      <el-table-column label="审核事项" min-width="220">
        <template #default="{ row }">
          <span class="cell-strong">{{ row.name }}</span>
        </template>
      </el-table-column>

      <el-table-column label="关联材料" min-width="140">
        <template #default="{ row }">{{ materialsOf(row) || '—' }}</template>
      </el-table-column>

      <el-table-column label="结论" width="132">
        <template #default="{ row }">
          <StatusBadge :status="row.status" :label="resultStatusLabel(row)" :tone="statusTone(row.status)" />
        </template>
      </el-table-column>

      <el-table-column label="原因" min-width="400">
        <template #default="{ row }">
          <div class="rule-reason">
            <span v-if="manuallyReviewed(row)">{{ manualNote(row) || "本次人工复核没有记录说明" }}</span>
            <span v-else>{{ auditResultSummary(row.status, row.message, payloadOf(row)) }}</span>
          </div>
          <div v-if="reasonText(row)" class="table-note rule-reason">{{ reasonText(row) }}</div>
        </template>
      </el-table-column>

      <el-table-column label="操作" width="100" fixed="right" align="center">
        <template #default="{ row }">
          <el-button link type="primary" size="small" @click.stop="openDetail(row)">查看详情</el-button>
        </template>
      </el-table-column>
    </el-table>
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

.rule-count {
  margin-left: auto;
}

.result-table :deep(.el-table__cell) {
  padding: 6px 0;
  vertical-align: top;
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
