<script setup lang="ts">
import { computed, ref, shallowRef } from "vue";

import type { AuditFieldView } from "@/api/audits";
import AuditFieldDetailDrawer from "@/components/audit/AuditFieldDetailDrawer.vue";
import StateBlock from "@/components/StateBlock.vue";
import StatusBadge from "@/components/StatusBadge.vue";
import { fieldStatusTone, statusLabel } from "@/constants/labels";
import { evidenceIsValue, fieldMode, fieldReason, formatFieldValue } from "@/utils/fieldPresentation";
import { quoteInlineText } from "@/utils/quoteText";
import { compareNaturalLabels } from "@/utils/naturalSort";

/** 字段列表展示业务编号、名称、关联材料、抽取结果和状态；原文、说明和纠错入口在详情抽屉中。 */
const props = defineProps<{
  fields: AuditFieldView[];
  /** 字段抽取节点是否已经执行过；未执行时说明原因，而不是显示成空值。 */
  extractionFinished: boolean;
  /** 任务是否允许发起纠错（未结束的任务才可）。 */
  correctionEnabled?: boolean;
}>();

const emit = defineEmits<{
  correct: [field: AuditFieldView];
}>();

const detailField = shallowRef<AuditFieldView | null>(null);

function extractedText(field: AuditFieldView): string {
  const value = field.normalized_value;
  if (value !== null && value !== undefined && value !== "") return formatFieldValue(field);
  if (field.status === "NOT_PRESENT") return "材料未载明";
  if (field.status === "NOT_APPLICABLE") return "本次不适用";
  if (field.extraction_enabled === false) return "未启用抽取";
  if (field.material_uploaded === false) return "材料未上传";
  return "未取到有效值";
}

function searchText(field: AuditFieldView): string {
  const mode = fieldMode(field);
  return [
    field.source_ref,
    field.name,
    field.material_name,
    field.raw_value,
    String(field.normalized_value ?? ""),
    formatFieldValue(field),
    field.status,
    ...(evidenceIsValue(mode)
      ? field.evidence.map((item) => quoteInlineText(item.quote_text))
      : [fieldReason(field)]),
  ]
    .join(" ")
    .toLocaleLowerCase();
}

function forwardCorrect(field: AuditFieldView): void {
  emit("correct", field);
}

const search = ref("");
const statusFilter = ref("ALL");

/**
 * 筛选分档。分档必须覆盖服务端可能给出的全部字段状态码：
 * 落在分档之外的码会让数量对不上，也会让人以为所有结果都被看过了。
 */
const filters = [
  { value: "ALL", label: "全部", statuses: [] as string[] },
  { value: "EXTRACTED", label: "已抽取", statuses: ["PRESENT", "REVIEWED"] },
  {
    value: "UNRESOLVED",
    label: "未取到值",
    statuses: ["NOT_PRESENT", "EVIDENCE_UNCLEAR", "NORMALIZATION_FAILED", "EXTRACTION_FAILED", "EVIDENCE_INSUFFICIENT"],
  },
  { value: "NOT_APPLICABLE", label: "不适用", statuses: ["NOT_APPLICABLE"] },
  { value: "NOT_EXECUTED", label: "未执行", statuses: ["NOT_EXECUTED", "PENDING"] },
];

function matchesFilter(field: AuditFieldView, filter: (typeof filters)[number]): boolean {
  return filter.value === "ALL" || filter.statuses.includes(field.status);
}

/** 不被任何分档覆盖的状态码：新增状态出现时必须看得见，不能静默消失。 */
const uncoveredStatusCounts = computed(() => {
  const covered = new Set(filters.flatMap((filter) => filter.statuses));
  const counts: Record<string, number> = {};
  for (const field of props.fields) {
    if (!covered.has(field.status)) {
      counts[field.status] = (counts[field.status] ?? 0) + 1;
    }
  }
  return counts;
});

const uncoveredTotal = computed(() => Object.values(uncoveredStatusCounts.value).reduce((sum, item) => sum + item, 0));

const rows = computed(() => {
  const selected = filters.find((item) => item.value === statusFilter.value)!;
  const query = search.value.trim().toLocaleLowerCase();
  return props.fields.filter((field) => {
    return matchesFilter(field, selected) && (!query || searchText(field).includes(query));
  }).sort((left, right) => compareNaturalLabels(left.source_ref, right.source_ref));
});

/** 未执行时说明是「节点还没跑」还是「跑了但这一项没结果」，两者不能混。 */
const emptyDescription = computed(() => {
  if (props.extractionFinished) {
    return "字段抽取节点已执行，但没有形成字段结果；可在规则结果或证据区域核对具体原因。";
  }
  return "字段抽取节点尚未执行。执行后这里会显示服务端真实抽取到的原文与标准化结果。";
});
</script>

<template>
  <StateBlock v-if="!fields.length" kind="empty" title="当前还没有字段结果" :description="emptyDescription" />
  <div v-else class="stack">
    <div class="field-toolbar">
      <el-input v-model="search" clearable placeholder="搜索编号、字段、材料或原文" aria-label="搜索字段结果" class="field-search" />
      <el-select v-model="statusFilter" aria-label="按字段状态筛选" class="field-status-filter">
        <el-option v-for="filter in filters" :key="filter.value" :value="filter.value"
          :label="`${filter.label}（${fields.filter((field) => matchesFilter(field, filter)).length}）`" />
      </el-select>
      <span class="text-hint field-count">显示 {{ rows.length }} / {{ fields.length }} 项</span>
    </div>

    <el-alert
      v-if="uncoveredTotal"
      type="warning"
      show-icon
      :closable="false"
      :title="`有 ${uncoveredTotal} 条字段结果的状态不在上面的分档内（${Object.entries(uncoveredStatusCounts).map(([status, count]) => `${statusLabel(status)} ${count} 条`).join('、')}），它们只在「全部」里出现。`"
    />

    <el-empty v-if="!rows.length" description="没有符合筛选条件的字段结果">
      <el-button @click="search = ''; statusFilter = 'ALL'">清除筛选</el-button>
    </el-empty>

    <el-table v-else :data="rows" border stripe class="audit-result-table field-table" @row-click="(field: AuditFieldView) => detailField = field">
      <el-table-column label="字段编号" width="120">
        <template #default="{ row }">{{ row.source_ref || "—" }}</template>
      </el-table-column>
      <el-table-column label="字段" width="180">
        <template #default="{ row }">
          <span class="cell-strong">{{ row.name }}</span>
        </template>
      </el-table-column>
      <el-table-column prop="material_name" label="关联材料" width="220" show-overflow-tooltip />
      <el-table-column label="抽取结果" min-width="260">
        <template #default="{ row }">
          <div class="field-value" :class="{ 'field-value--empty': row.normalized_value === null || row.normalized_value === undefined || row.normalized_value === '' }">{{ extractedText(row) }}</div>
        </template>
      </el-table-column>
      <el-table-column label="状态" width="112">
        <template #default="{ row }">
          <StatusBadge :status="row.status" :tone="fieldStatusTone(row.status)" />
        </template>
      </el-table-column>
      <el-table-column label="操作" width="88" fixed="right" align="center">
        <template #default="{ row }">
          <el-button link type="primary" @click.stop="detailField = row">详情</el-button>
        </template>
      </el-table-column>
    </el-table>
  </div>

  <AuditFieldDetailDrawer
    :open="Boolean(detailField)"
    :field="detailField"
    :correction-enabled="correctionEnabled"
    @close="detailField = null"
    @correct="forwardCorrect"
  />
</template>

<style scoped>
.field-toolbar {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
}

.field-search {
  width: min(100%, 320px);
}

.field-status-filter {
  width: 180px;
}

.field-count {
  margin-left: auto;
  font-size: var(--el-font-size-base);
}

.field-table :deep(.el-table__cell) {
  padding: 8px 0;
  vertical-align: middle;
}

.field-table :deep(.cell) {
  line-height: 1.35;
  overflow-wrap: anywhere;
}

.field-table :deep(.el-table__row) {
  cursor: pointer;
}

.field-value {
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
  white-space: pre-wrap;
  color: var(--el-text-color-primary);
  font-weight: 500;
  font-variant-numeric: lining-nums tabular-nums;
}

.field-value--empty {
  color: var(--el-text-color-secondary);
  font-weight: 400;
}

.cell-strong {
  font-weight: 600;
}
</style>
