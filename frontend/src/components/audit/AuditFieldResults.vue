<script setup lang="ts">
import { computed, ref, shallowRef, watch } from "vue";

import type { AuditField } from "@/api/contracts";
import AuditFieldDetailDrawer from "@/components/audit/AuditFieldDetailDrawer.vue";
import StateBlock from "@/components/StateBlock.vue";
import StatusBadge from "@/components/StatusBadge.vue";
import { evidenceIsValue, extractionOutcome, extractionOutcomeCode, fieldMode, fieldReason, fieldSourceFiles, formatFieldValue, hasEffectiveFieldValue } from "@/utils/fieldPresentation";
import { quoteInlineText } from "@/utils/quoteText";
import { compareNaturalLabels } from "@/utils/naturalSort";

/** 字段列表展示业务编号、名称、关联材料、抽取结果和状态；原文、说明和纠错入口在详情抽屉中。 */
const props = defineProps<{
  fields: AuditField[];
  /** 字段抽取是否已经执行过；未执行时说明原因，而不是显示成空值。 */
  extractionFinished: boolean;
  /** 任务是否允许发起纠错（未结束的任务才可）。 */
  correctionEnabled?: boolean;
  /** 页面汇总入口设置的初始筛选；由父组件传入，改选后同步回父组件。 */
  filter?: string;
}>();

const emit = defineEmits<{
  correct: [field: AuditField];
  "update:filter": [value: string];
}>();

const detailField = shallowRef<AuditField | null>(null);

function extractedText(field: AuditField): string {
  if (extractionOutcomeCode(field) === "UNKNOWN_STATUS") return `未识别状态（${field.status}）`;
  if (hasEffectiveFieldValue(field)) return formatFieldValue(field);
  if (field.status === "NOT_PRESENT") return "材料未载明";
  if (field.status === "EXTRACTION_FAILED") return "未取得可采信的值";
  if (field.status === "NOT_APPLICABLE") return "本次不适用";
  if (field.extraction_enabled === false) return "未启用抽取";
  if (field.material_uploaded === false) return "材料未上传";
  return "未取到有效值";
}

function searchText(field: AuditField): string {
  const mode = fieldMode(field);
  return [
    field.source_ref,
    field.name,
    field.material_name,
    ...fieldSourceFiles(field),
    field.actual_period_scope,
    field.actual_unit,
    field.actual_statement_scope,
    field.raw_value,
    String(field.normalized_value ?? ""),
    formatFieldValue(field),
    field.status,
    ...(evidenceIsValue(mode)
      ? field.evidence.map((item) => [item.quote_text, item.quote_label, item.quote_header, item.quote_period].filter(Boolean).map((text) => quoteInlineText(String(text))).join(" "))
      : [fieldReason(field)]),
  ]
    .join(" ")
    .toLocaleLowerCase();
}

function forwardCorrect(field: AuditField): void {
  emit("correct", field);
}

const search = ref("");
const statusFilter = ref("ALL");

function sourceFilesText(field: AuditField): string {
  return fieldSourceFiles(field).join("、") || "未记录来源文件";
}

const filters = [
  { value: "ALL", label: "全部" },
  { value: "EXTRACTED", label: "已抽取" },
  { value: "NO_VALUE", label: "未抽取到" },
];

function matchesFilter(field: AuditField, filter: (typeof filters)[number]): boolean {
  return filter.value === "ALL" || extractionOutcomeCode(field) === filter.value;
}

watch(() => props.filter, (value) => {
  if (value && filters.some((item) => item.value === value)) {
    statusFilter.value = value;
  }
}, { immediate: true });

watch(statusFilter, (value) => emit("update:filter", value));

/** 统计入口每次点击都重置搜索，即使选中的状态没有变化。 */
function applyFilter(value: string): void {
  search.value = "";
  statusFilter.value = value;
}
defineExpose({ applyFilter });

watch(() => props.fields, (fields) => {
  if (detailField.value) detailField.value = fields.find((field) =>
    field.field_key === detailField.value?.field_key && field.material_key === detailField.value?.material_key) ?? null;
});

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
    return "字段抽取已执行，但没有形成字段结果；可在规则结果或证据区域核对具体原因。";
  }
  return "字段抽取尚未执行。执行后这里会显示真实抽取到的原文与标准化结果。";
});
</script>

<template>
  <StateBlock v-if="!fields.length" kind="empty" title="当前还没有字段结果" :description="emptyDescription" />
  <div v-else class="stack">
    <div class="field-toolbar">
      <el-input v-model="search" clearable placeholder="搜索字段、来源文件或原文" aria-label="搜索字段结果" class="field-search" />
      <el-select v-model="statusFilter" aria-label="按字段状态筛选" class="field-status-filter" style="min-width: 260px">
        <el-option v-for="filter in filters" :key="filter.value" :value="filter.value"
          :label="`${filter.label}（${fields.filter((field) => matchesFilter(field, filter)).length}）`" />
      </el-select>
      <span class="text-hint field-count">显示 {{ rows.length }} / {{ fields.length }} 项</span>
    </div>

    <el-empty v-if="!rows.length" description="没有符合筛选条件的字段结果">
      <el-button @click="search = ''; statusFilter = 'ALL'">清除筛选</el-button>
    </el-empty>

    <el-table v-else :data="rows" border stripe class="audit-result-table field-table" @row-click="(field: AuditField) => detailField = field">
      <el-table-column label="字段编号" width="140">
        <template #default="{ row }">{{ row.source_ref || "—" }}</template>
      </el-table-column>
      <el-table-column label="字段" width="180">
        <template #default="{ row }">
          <span class="cell-strong">{{ row.name }}</span>
        </template>
      </el-table-column>
      <el-table-column label="来源文件" width="230">
        <template #default="{ row }">
          <div class="field-value" :title="sourceFilesText(row)">{{ sourceFilesText(row) }}</div>
        </template>
      </el-table-column>
      <el-table-column label="当前取值" min-width="240">
        <template #default="{ row }">
          <div class="field-value" :class="{ 'field-value--empty': row.normalized_value === null || row.normalized_value === undefined || row.normalized_value === '' }">{{ extractedText(row) }}</div>
        </template>
      </el-table-column>
      <el-table-column label="抽取结果" width="140">
        <template #default="{ row }">
          <StatusBadge v-if="extractionOutcome(row)" :status="row.status" :label="extractionOutcome(row)" :tone="extractionOutcomeCode(row) === 'EXTRACTED' ? 'success' : 'warning'" />
          <span v-else class="text-hint">—</span>
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
