<script setup lang="ts">
import { computed, ref } from "vue";
import { FileSearch } from "lucide-vue-next";

import type { AuditFieldView } from "@/api/audits";
import StateBlock from "@/components/StateBlock.vue";
import StatusBadge from "@/components/StatusBadge.vue";
import {
  factOriginLabel,
  fieldStatusTone,
  formatBusinessValue,
  statusLabel,
} from "@/constants/labels";

/**
 * 字段抽取结果。
 *
 * 阅读顺序固定为：字段与来源材料 → 原文内容 → 标准化结果 → 当前状态 → 事实来源 → 操作。
 * 原文与标准化结果分列：原文是材料里真实写着的那几个字，标准化结果是系统为比较
 * 确定性算出来的值。两者不是一回事，合并成一格会让「系统算成了什么」和「材料里写了什么」
 * 无法对照，也无法发现标准化出错。
 *
 * 标准化结果不代表审核通过：它只说明系统按字段类型把原文算成了可比对的值。
 * 人工已确认的有效值（事实来源=人工纠错 / 状态=已复核）单独标注。
 *
 * 未取到值时区分原因：材料未上传、本次未执行抽取、抽取失败、未抽取到——它们是
 * 四种不同的缺什么，统一写成「字段为空」会让人不知道该去补哪一步。
 */
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

/** 原文与标准化结果是否不同：不同才需要并排提示，避免同值重复两次。 */
function normalizedDiffers(field: AuditFieldView): boolean {
  const normalized = field.normalized_value;
  if (normalized === null || normalized === undefined) {
    return false;
  }
  return String(field.raw_value ?? "") !== String(normalized);
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
    statuses: ["NOT_PRESENT", "EVIDENCE_UNCLEAR", "NORMALIZATION_FAILED", "EXTRACTION_FAILED"],
  },
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
      <el-radio-group v-model="statusFilter" aria-label="按字段状态筛选">
        <el-radio-button v-for="filter in filters" :key="filter.value" :value="filter.value">
          {{ filter.label }}（{{ fields.filter((field) => matchesFilter(field, filter)).length }}）
        </el-radio-button>
      </el-radio-group>
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

    <el-table v-else :data="rows" border stripe size="small">
      <el-table-column label="字段名称" min-width="150" show-overflow-tooltip>
        <template #default="{ row }">
          <span class="cell-strong">{{ row.name }}</span>
          <div v-if="row.fact_revision_no" class="table-note">事实修订 {{ row.fact_revision_no }}</div>
        </template>
      </el-table-column>

      <el-table-column label="来源材料" min-width="140" show-overflow-tooltip>
        <template #default="{ row }">
          <span>{{ row.material_name }}</span>
          <div v-if="row.material_required" class="table-note">必传</div>
        </template>
      </el-table-column>

      <!-- 原文内容：材料里真实写着的内容，不做任何改写 -->
      <el-table-column label="原文内容" min-width="200">
        <template #default="{ row }">
          <div class="cell-quote">{{ rawText(row) }}</div>
        </template>
      </el-table-column>

      <!-- 标准化结果：系统按字段类型确定性算出的可比对值；与原文不同才单独强调 -->
      <el-table-column label="标准化结果" min-width="150">
        <template #default="{ row }">
          <span :class="{ 'cell-strong': normalizedDiffers(row) }">{{ normalizedText(row) }}</span>
          <div v-if="normalizedDiffers(row)" class="table-note">由原文确定性算出，不代表审核通过</div>
        </template>
      </el-table-column>

      <el-table-column label="当前状态" width="130">
        <template #default="{ row }">
          <StatusBadge :status="row.status" :tone="fieldStatusTone(row.status)" />
          <div v-if="humanConfirmedText(row)" class="table-note">已有人工确认值</div>
        </template>
      </el-table-column>

      <el-table-column label="事实来源" width="120">
        <template #default="{ row }">
          <StatusBadge
            :status="row.origin === 'HUMAN_CORRECTION' ? 'MANUAL' : 'DOCUMENT_EXTRACTION'"
            :label="factOriginLabel(row.origin)"
            :tone="row.origin === 'HUMAN_CORRECTION' ? 'info' : 'neutral'"
          />
        </template>
      </el-table-column>

      <el-table-column label="人工确认值" min-width="160">
        <template #default="{ row }">
          <span v-if="humanConfirmedText(row)">{{ humanConfirmedText(row) }}</span>
          <span v-else class="text-muted">服务端未返回人工确认值</span>
        </template>
      </el-table-column>

      <el-table-column label="原文依据" min-width="170">
        <template #default="{ row }">
          <div v-if="evidenceItems(row).length" class="table-note">已记录 {{ evidenceItems(row).length }} 条原文证据</div>
          <div v-else-if="row.extraction_enabled === false" class="table-note">本方案未启用自动抽取</div>
          <div v-else-if="row.material_uploaded === false" class="table-note">材料未上传，无法抽取</div>
          <div v-else-if="row.status === 'PENDING' || row.status === 'NOT_EXECUTED'" class="table-note">
            本次未执行抽取
          </div>
          <div v-else-if="row.status === 'EXTRACTION_FAILED'" class="table-note">抽取失败，没有证据</div>
          <div v-else class="table-note">已抽取，未记录原文证据</div>
        </template>
      </el-table-column>

      <el-table-column label="操作" width="170" fixed="right" align="center">
        <template #default="{ row }">
          <el-button v-if="evidenceItems(row).length === 1" link type="primary" size="small" title="查看证据原文" @click="openEvidence(row, evidenceItems(row)[0].evidence_no)">
            <template #icon><FileSearch :size="15" /></template>
            原文证据
          </el-button>
          <el-dropdown
            v-else-if="evidenceItems(row).length > 1"
            trigger="click"
            @command="(evidenceNo: unknown) => openEvidence(row, String(evidenceNo))"
          >
            <el-button link type="primary" size="small" title="选择字段属性的原文证据">
              <template #icon><FileSearch :size="15" /></template>
              原文证据（{{ evidenceItems(row).length }}）
            </el-button>
            <template #dropdown>
              <el-dropdown-menu>
                <el-dropdown-item
                  v-for="item in evidenceItems(row)"
                  :key="item.evidence_no"
                  :command="item.evidence_no"
                >
                  {{ item.field_path || "字段原文" }}
                </el-dropdown-item>
              </el-dropdown-menu>
            </template>
          </el-dropdown>
          <el-button
            v-if="correctionEnabled"
            link
            type="primary"
            size="small"
            title="发起字段纠错"
            @click="$emit('correct', row)"
          >
            纠正
          </el-button>
        </template>
      </el-table-column>
    </el-table>

    <p class="text-hint">
      标准化结果只表示系统把原文算成了可比对的值，不代表审核通过；是否通过要看使用该字段的规则结论。
    </p>
  </div>
</template>

<style scoped>
.field-toolbar {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 12px;
}

.field-search {
  width: min(100%, 320px);
}

.field-toolbar :deep(.el-radio-group) {
  flex-wrap: wrap;
  gap: 4px 0;
}

.cell-strong {
  font-weight: 600;
}

.cell-quote {
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  line-height: 1.7;
}
</style>
