<script setup lang="ts">
import { computed, ref, shallowRef } from "vue";
import { FileSearch } from "lucide-vue-next";

import type { AuditFieldView } from "@/api/audits";
import DrawerPanel from "@/components/DrawerPanel.vue";
import StateBlock from "@/components/StateBlock.vue";
import StatusBadge from "@/components/StatusBadge.vue";
import {
  factOriginLabel,
  fieldStatusTone,
  formatBusinessValue,
  statusLabel,
} from "@/constants/labels";

/** 字段列表展示名称、抽取结果和状态；完整取值、来源和纠错入口在详情中。 */
const props = defineProps<{
  fields: AuditFieldView[];
  /** 字段抽取节点是否已经执行过；未执行时说明原因，而不是显示成空值。 */
  extractionFinished: boolean;
  /** 任务是否允许发起纠错（未结束的任务才可）。 */
  correctionEnabled?: boolean;
}>();

const emit = defineEmits<{
  "open-evidence": [field: AuditFieldView];
  correct: [field: AuditFieldView];
}>();

function evidenceItems(field: AuditFieldView): Array<{ evidence_no: string; field_path: string | null }> {
  if (field.evidences?.length) return field.evidences;
  return field.evidence_no
    ? [{ evidence_no: field.evidence_no, field_path: null }]
    : [];
}

function openEvidence(field: AuditFieldView, evidenceNo: string): void {
  emit("open-evidence", { ...field, evidence_no: evidenceNo });
}

/** 原文内容。没有原文时说明属于哪一种缺失，不留空白。 */
function rawText(field: AuditFieldView): string {
  if (field.raw_value !== null && field.raw_value !== undefined && field.raw_value !== "") {
    return field.raw_value;
  }
  if (field.extraction_enabled === false) {
    return "本方案未启用该字段的自动抽取";
  }
  if (field.material_uploaded === false) {
    return "材料未上传，没有可抽取的原文";
  }
  if (field.status === "PENDING" || field.status === "NOT_EXECUTED") {
    return "本次未执行抽取，没有原文";
  }
  if (field.status === "EXTRACTION_FAILED") {
    return "抽取失败，未取得原文";
  }
  if (field.status === "NOT_PRESENT") {
    return "材料中未找到该字段的原文";
  }
  if (field.status === "EVIDENCE_UNCLEAR") {
    return "抽取结果没有给出原文，未能形成可核对的事实";
  }
  return "服务端没有返回该字段的原文";
}

/** 标准化结果。它是按字段类型对原文确定性算出的可比对值，不代表审核通过。 */
function normalizedText(field: AuditFieldView): string {
  const normalized = field.normalized_value;
  if (normalized !== null && normalized !== undefined && normalized !== "") {
    return formatBusinessValue(normalized);
  }
  if (field.status === "NORMALIZATION_FAILED") {
    return "原文无法按字段类型标准化，未形成可比对的值";
  }
  if (field.raw_value !== null && field.raw_value !== undefined && field.raw_value !== "") {
    return "服务端没有返回标准化结果";
  }
  return "没有可标准化的原文";
}

/**
 * 人工已确认的有效值。
 *
 * 只认服务端返回的事实：事实来源为人工纠错，或状态已复核。服务端没有给出人工
 * 确认标志时不做前端推断——那会把系统抽取值说成人工确认过的值。
 */
function humanConfirmedText(field: AuditFieldView): string | null {
  if (field.origin !== "HUMAN_CORRECTION" && field.status !== "REVIEWED") {
    return null;
  }
  const raw = field.raw_value;
  if (raw !== null && raw !== undefined && raw !== "") {
    return raw;
  }
  return "人工已确认该字段在材料中不存在";
}

const detailField = shallowRef<AuditFieldView | null>(null);

function extractedText(field: AuditFieldView): string {
  const value = field.normalized_value;
  if (value !== null && value !== undefined && value !== "") return formatBusinessValue(value);
  if (field.status === "NOT_PRESENT") return "材料未载明";
  if (field.status === "NOT_APPLICABLE") return "本次不适用";
  if (field.extraction_enabled === false) return "未启用抽取";
  if (field.material_uploaded === false) return "材料未上传";
  return "未取到有效值";
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
    const text = [field.name, field.material_name, field.raw_value, String(field.normalized_value ?? ""), field.status]
      .join(" ")
      .toLocaleLowerCase();
    return matchesFilter(field, selected) && (!query || text.includes(query));
  });
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
      <el-input v-model="search" clearable placeholder="搜索字段、材料或原文" aria-label="搜索字段结果" class="field-search" />
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

    <el-table v-else :data="rows" class="field-table" @row-click="(field: AuditFieldView) => detailField = field">
      <el-table-column label="字段" min-width="240">
        <template #default="{ row }">
          <span class="cell-strong">{{ row.name }}</span>
          <div class="table-note">{{ row.material_name }}</div>
        </template>
      </el-table-column>
      <el-table-column label="抽取结果" min-width="360">
        <template #default="{ row }">
          <div class="field-value">{{ extractedText(row) }}</div>
        </template>
      </el-table-column>
      <el-table-column label="状态" width="132">
        <template #default="{ row }">
          <StatusBadge :status="row.status" :tone="fieldStatusTone(row.status)" />
        </template>
      </el-table-column>
      <el-table-column label="操作" width="100" fixed="right" align="center">
        <template #default="{ row }">
          <el-button link type="primary" @click.stop="detailField = row">详情</el-button>
        </template>
      </el-table-column>
    </el-table>
  </div>
  <DrawerPanel :open="Boolean(detailField)" :title="detailField?.name ?? '字段详情'" size="min(680px, 100%)" @close="detailField = null">
    <template v-if="detailField">
      <div class="field-detail-head">
        <StatusBadge :status="detailField.status" :tone="fieldStatusTone(detailField.status)" />
        <span class="text-muted">{{ detailField.material_name }}</span>
      </div>
      <h4 class="app-section-title">抽取原始值</h4>
      <div class="detail-value">{{ rawText(detailField) }}</div>
      <h4 class="app-section-title">标准化结果</h4>
      <div class="detail-value">{{ normalizedText(detailField) }}</div>
      <el-descriptions :column="1" class="block-gap">
        <el-descriptions-item v-if="detailField.origin" label="事实来源">{{ factOriginLabel(detailField.origin) }}</el-descriptions-item>
        <el-descriptions-item v-if="humanConfirmedText(detailField)" label="人工确认值">{{ humanConfirmedText(detailField) }}</el-descriptions-item>
        <el-descriptions-item v-if="detailField.extraction_message && detailField.extraction_presentation_version === 2" label="抽取说明">{{ detailField.extraction_message }}</el-descriptions-item>
      </el-descriptions>
      <el-collapse v-if="detailField.extraction_message && detailField.extraction_presentation_version !== 2" class="block-gap">
        <el-collapse-item title="原执行说明（该结果尚未保存结构化说明）" name="original">
          <div class="detail-value">{{ detailField.extraction_message }}</div>
        </el-collapse-item>
      </el-collapse>
      <div class="field-detail-actions">
        <el-button v-for="(item, index) in evidenceItems(detailField)" :key="item.evidence_no" @click="openEvidence(detailField, item.evidence_no)">
          <template #icon><FileSearch :size="15" /></template>
          {{ item.field_path || (evidenceItems(detailField).length > 1 ? `原文证据 ${index + 1}` : "原文证据") }}
        </el-button>
        <span v-if="!evidenceItems(detailField).length" class="text-hint">未记录原文证据</span>
        <el-button v-if="correctionEnabled" type="primary" plain @click="$emit('correct', detailField)">纠正字段</el-button>
      </div>
    </template>
  </DrawerPanel>
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
}

.field-table :deep(.el-table__cell) {
  padding: 6px 0;
  vertical-align: top;
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
}

.field-detail-head,
.field-detail-actions {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 12px;
}

.field-detail-actions {
  margin-top: 20px;
}

.cell-strong {
  font-weight: 600;
}

.detail-value {
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  line-height: 1.55;
}
</style>
