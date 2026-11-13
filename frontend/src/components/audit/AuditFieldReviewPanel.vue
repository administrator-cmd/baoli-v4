<script setup lang="ts">
import { computed, onBeforeUnmount, ref, shallowRef, watch } from "vue";
import { ElMessageBox } from "element-plus";

import type {
  AuditField,
  FieldReviewCorrection,
  FieldReviewMissing,
  FieldReviewState,
} from "@/api/contracts";

import AuditFieldDetailDrawer from "@/components/audit/AuditFieldDetailDrawer.vue";
import StatusBadge from "@/components/StatusBadge.vue";
import { formatBusinessValue } from "@/constants/labels";
import { fieldSourceFiles, formatFieldValue, hasEffectiveFieldValue } from "@/utils/fieldPresentation";
import { compareNaturalLabels } from "@/utils/naturalSort";

/**
 * 字段核对。
 *
 * 抽取完成后任务停在这里：用户按材料逐项核对字段，可以改错值、补填漏抽、
 * 明确标记材料中不存在，然后「保存修改」继续停在这里，或「确认并继续审核」。
 *
 * 修改只在本地暂存，提交时带上页面看到的字段版本；服务端据此拒绝旧页面。
 * 原抽取值与本次采用值分开显示，人工修改不覆盖原抽取记录。
 */
const props = defineProps<{
  fields: AuditField[];
  state: FieldReviewState;
  /** 正在保存修改。 */
  saving?: boolean;
  /** 正在提交确认。 */
  confirming?: boolean;
  /** 任务有进行中的后台操作或已不在等待点。 */
  disabled?: boolean;
  filter?: string;
}>();

const emit = defineEmits<{
  save: [corrections: FieldReviewCorrection[]];
  confirm: [payload: { corrections: FieldReviewCorrection[]; acknowledgeMissing: boolean; note: string | null }];
  "update:dirty": [dirty: boolean];
  "update:filter": [value: string];
}>();

interface Draft {
  fieldKey: string;
  materialKey: string;
  decision: "MODIFY_FIELD" | "MARK_NOT_PRESENT";
  value: string;
  expectedRevisionNo: number | null;
}

const drafts = ref<Map<string, Draft>>(new Map());
const detailField = shallowRef<AuditField | null>(null);
const note = ref("");
const search = ref("");
const statusFilter = ref("ALL");

function keyOf(field: AuditField): string {
  return `${field.field_key ?? field.name}::${field.material_key ?? field.material_name}`;
}

function draftOf(field: AuditField): Draft | null {
  return drafts.value.get(keyOf(field)) ?? null;
}

function currentText(field: AuditField): string {
  const value = field.normalized_value;
  if (value === null || value === undefined || value === "") return "";
  // 结构化字段的值是对象：编辑框必须给出可还原的 JSON，展示用的拼接文本存回去会被
  // 后端按 JSON 校验拒绝，保存时字段结构也会丢。
  if (isStructured(field)) {
    return JSON.stringify(value, null, 2);
  }
  return formatBusinessValue(value);
}

/** 对象和数组均按 JSON 编辑，避免把结构化值转换成不可还原的展示文本。 */
function isStructured(field: AuditField): boolean {
  const value = field.normalized_value;
  return typeof value === "object" && value !== null;
}

/** 列表展示的采用值：数值按字段类型加千分位，人工标记为不存在的单独说明。 */
function adoptedText(field: AuditField): string {
  if (field.status === "NOT_PRESENT" && field.corrected) return "人工已确认材料中不存在";
  const value = field.normalized_value;
  if (value === null || value === undefined || value === "") return "";
  return formatFieldValue(field);
}

function extractedText(field: AuditField): string {
  const value = field.extracted_raw_value;
  if (value !== null && value !== undefined && value !== "") return String(value);
  if (field.corrected) return "原抽取未取得取值，本次由人工补填";
  if (field.status === "NOT_PRESENT") return "材料中未载明";
  if (field.extraction_enabled === false) return "本方案未启用该字段抽取";
  if (field.material_uploaded === false) return "材料未上传";
  return "未取到取值";
}

function sourceFilesText(field: AuditField): string {
  return fieldSourceFiles(field).join("、") || "未记录来源文件";
}

const missingByKey = computed(() => {
  const map = new Map<string, FieldReviewMissing>();
  for (const item of props.state.missing_fields) {
    map.set(`${item.field_key}::${item.material_key}`, item);
  }
  return map;
});

function missingOf(field: AuditField): FieldReviewMissing | null {
  return missingByKey.value.get(keyOf(field)) ?? null;
}

/** 该行提交后是否仍会被服务端判为缺项：改值会补上，标记「不存在」本身就是缺项。 */
function stillMissing(field: AuditField): boolean {
  const draft = draftOf(field);
  if (draft) {
    if (draft.decision === "MARK_NOT_PRESENT") return true;
    return !draft.value.trim();
  }
  return missingOf(field) !== null;
}

function startEdit(field: AuditField): void {
  const next = new Map(drafts.value);
  next.set(keyOf(field), {
    fieldKey: field.field_key ?? field.name,
    materialKey: field.material_key ?? field.material_name,
    decision: "MODIFY_FIELD",
    value: currentText(field),
    expectedRevisionNo: field.fact_revision_no ?? null,
  });
  drafts.value = next;
}

function markMissing(field: AuditField): void {
  const next = new Map(drafts.value);
  next.set(keyOf(field), {
    fieldKey: field.field_key ?? field.name,
    materialKey: field.material_key ?? field.material_name,
    decision: "MARK_NOT_PRESENT",
    value: "",
    expectedRevisionNo: field.fact_revision_no ?? null,
  });
  drafts.value = next;
}

function revert(field: AuditField): void {
  const next = new Map(drafts.value);
  next.delete(keyOf(field));
  drafts.value = next;
}

/** 用户明确放弃草稿后清空暂存：面板仍挂载，不清会让「离开」反复提示同一批修改。 */
function discardDrafts(): void {
  drafts.value = new Map();
  note.value = "";
}

/** 材料未上传时字段没有可核对的原文，服务端也会拒绝整批提交。 */
function materialMissing(field: AuditField): boolean {
  return field.material_uploaded === false;
}

function updateDraft(field: AuditField, value: string): void {
  const draft = draftOf(field);
  if (!draft) return;
  const next = new Map(drafts.value);
  next.set(keyOf(field), { ...draft, value });
  drafts.value = next;
}

const dirty = computed(() => drafts.value.size > 0);

watch(dirty, (value) => emit("update:dirty", value), { immediate: true });
// 面板关闭（确认已受理、任务离开等待点）后不能再让页面以为还有未保存修改。
onBeforeUnmount(() => emit("update:dirty", false));
// 输入修订变化说明服务端字段版本已经变了（例如刚保存成功）：旧草稿不能继续贴在新版本上。
// 普通刷新不会改修订号，因此不会悄悄丢掉用户正在编辑的内容。
watch(() => props.state.input_revision, () => {
  drafts.value = new Map();
  note.value = "";
});

function corrections(): FieldReviewCorrection[] {
  return [...drafts.value.values()].map((draft) => ({
    field_key: draft.fieldKey,
    material_key: draft.materialKey,
    decision: draft.decision,
    ...(draft.decision === "MODIFY_FIELD" ? { field_value: draft.value.trim() } : {}),
    business_note: note.value.trim(),
    basis_type: "HUMAN_STATEMENT" as const,
    expected_revision_no: draft.expectedRevisionNo,
  }));
}

/** 本次提交后仍缺值的字段：改值会补上，标记「材料中不存在」仍然是缺项。 */
const remainingMissing = computed(() =>
  props.fields.filter((field) => stillMissing(field)),
);

const canSubmit = computed(() => !props.disabled && !props.saving && !props.confirming);

function save(): void {
  if (!canSubmit.value || !dirty.value) return;
  emit("save", corrections());
}

async function confirm(): Promise<void> {
  if (!canSubmit.value) return;
  if (remainingMissing.value.length) {
    const names = remainingMissing.value.slice(0, 8).map((field) => field.name).join("、");
    const more = remainingMissing.value.length > 8 ? ` 等 ${remainingMissing.value.length} 项` : "";
    try {
      await ElMessageBox.confirm(
        `仍有 ${remainingMissing.value.length} 项字段没有可执行取值：${names}${more}。`
          + "继续审核时不会补齐这些字段，相关规则会按真实缺项记录为无法执行或证据不足。确认继续？",
        "仍有字段缺值",
        { type: "warning", confirmButtonText: "确认并继续", cancelButtonText: "返回核对" },
      );
    } catch {
      return;
    }
  }
  emit("confirm", {
    corrections: corrections(),
    // 只有在用户看过缺项并确认继续时才算确认；没有缺项时不冒充已确认。
    acknowledgeMissing: remainingMissing.value.length > 0,
    note: note.value.trim() || null,
  });
}

/**
 * 提交说明是否填写：服务端要求每条修改都带真实说明。
 * 没有修改时可以不填，因为此时确认的是「当前取值可用」。
 */
const noteRequired = computed(() => dirty.value && !note.value.trim());

const filters = [
  { value: "ALL", label: "全部" },
  { value: "EXTRACTED", label: "有有效值" },
  { value: "NO_VALUE", label: "无有效值" },
  { value: "MISSING", label: "未取到值" },
  { value: "FAILED", label: "抽取失败" },
  { value: "EDITED", label: "已修改" },
];

watch(() => props.filter, (value) => {
  if (value && filters.some((item) => item.value === value)) statusFilter.value = value;
}, { immediate: true });
watch(statusFilter, (value) => emit("update:filter", value));

function applyFilter(value: string): void {
  search.value = "";
  statusFilter.value = value;
}
defineExpose({ discardDrafts, applyFilter });

watch(() => props.fields, (fields) => {
  if (detailField.value) detailField.value = fields.find((field) => keyOf(field) === keyOf(detailField.value!)) ?? null;
});

const MISSING_STATUSES = new Set([
  "NOT_PRESENT", "EVIDENCE_INSUFFICIENT", "EVIDENCE_UNCLEAR",
  "NOT_EXECUTED", "PENDING", "EXTRACTION_FAILED", "NORMALIZATION_FAILED",
]);

function matchesFilterValue(field: AuditField, value: string): boolean {
  switch (value) {
    case "EXTRACTED":
      return hasEffectiveFieldValue(field);
    case "NO_VALUE":
      return field.status !== "NOT_APPLICABLE" && !hasEffectiveFieldValue(field);
    case "MISSING":
      return stillMissing(field) || MISSING_STATUSES.has(field.status);
    case "FAILED":
      return ["EXTRACTION_FAILED", "NORMALIZATION_FAILED", "EVIDENCE_INSUFFICIENT", "EVIDENCE_UNCLEAR"].includes(field.status);
    case "EDITED":
      return Boolean(field.corrected) || Boolean(draftOf(field));
    default:
      return true;
  }
}

function searchText(field: AuditField): string {
  return [field.source_ref, field.name, field.material_name, ...fieldSourceFiles(field), field.actual_period_scope, field.actual_unit, field.actual_statement_scope, field.actual_entity_scope, extractedText(field), currentText(field)]
    .join(" ").toLocaleLowerCase();
}

const rows = computed(() => {
  const query = search.value.trim().toLocaleLowerCase();
  return props.fields
    .filter((field) => matchesFilterValue(field, statusFilter.value) && (!query || searchText(field).includes(query)))
    .sort((left, right) => (
      left.material_name === right.material_name
        ? compareNaturalLabels(left.source_ref, right.source_ref)
        : left.material_name.localeCompare(right.material_name, "zh-CN")
    ));
});

/** 各筛选档的数量只统计，不改写当前筛选条件。 */
function countOf(value: string): number {
  return props.fields.filter((field) => matchesFilterValue(field, value)).length;
}
</script>

<template>
  <div class="stack">
    <el-alert
      v-if="state.blocked_reason"
      type="error"
      show-icon
      :closable="false"
      title="本次配置无法建立字段核对边界"
      :description="state.blocked_reason"
    />
    <template v-else>
      <el-alert
        v-if="state.reconfirm_reason"
        type="warning"
        show-icon
        :closable="false"
        :title="state.reconfirm_reason"
      />
      <div class="review-summary">
        <span>共 <strong>{{ fields.length }}</strong> 项字段</span>
        <span>仍缺可执行取值 <strong>{{ remainingMissing.length }}</strong> 项</span>
        <span>确认后还需执行 <strong>{{ state.after_review_rule_count }}</strong> 条规则</span>
        <span v-if="state.confirmed" class="text-hint">
          已在输入修订 {{ state.confirmed_input_revision }} 上确认
        </span>
      </div>

      <div class="field-toolbar">
        <el-input v-model="search" clearable placeholder="搜索字段、文件或取值" aria-label="搜索字段" class="field-search" />
        <el-select v-model="statusFilter" aria-label="按核对范围筛选" class="field-status-filter">
          <el-option v-for="filter in filters" :key="filter.value" :value="filter.value"
            :label="`${filter.label}（${countOf(filter.value)}）`" />
        </el-select>
        <span class="text-hint field-count">显示 {{ rows.length }} / {{ fields.length }} 项</span>
      </div>

      <el-empty v-if="!rows.length" description="没有符合筛选条件的字段">
        <el-button @click="search = ''; statusFilter = 'ALL'">清除筛选</el-button>
      </el-empty>

      <el-table v-else :data="rows" border stripe class="audit-result-table field-table" :row-key="keyOf">
        <el-table-column label="来源文件" min-width="210" show-overflow-tooltip>
          <template #default="{ row }">{{ sourceFilesText(row) }}</template>
        </el-table-column>
        <el-table-column label="字段" width="190">
          <template #default="{ row }">
            <div class="cell-strong">{{ row.name }}</div>
            <div class="text-hint">{{ row.source_ref || "无业务编号" }}</div>
          </template>
        </el-table-column>
        <el-table-column label="抽取原始值" min-width="220">
          <template #default="{ row }">
            <div class="field-value" :class="{ 'field-value--empty': row.extracted_raw_value === null || row.extracted_raw_value === undefined || row.extracted_raw_value === '' }">{{ extractedText(row) }}</div>
          </template>
        </el-table-column>
        <el-table-column label="本次采用值" min-width="220">
          <template #default="{ row }">
            <template v-if="draftOf(row)">
              <el-tag v-if="draftOf(row)?.decision === 'MARK_NOT_PRESENT'" type="warning" size="small" disable-transitions>
                已标记材料中不存在
              </el-tag>
              <el-input
                v-else
                :model-value="draftOf(row)?.value ?? ''"
                :type="isStructured(row) ? 'textarea' : 'text'"
                :autosize="isStructured(row) ? { minRows: 2, maxRows: 8 } : false"
                :disabled="!canSubmit"
                placeholder="填写核对后的真实值"
                @update:model-value="(value: string) => updateDraft(row, value)"
              />
            </template>
            <div v-else class="field-value" :class="{ 'field-value--empty': !adoptedText(row) }">
              {{ adoptedText(row) || "没有可执行的取值" }}
            </div>
          </template>
        </el-table-column>
        <el-table-column label="详情" width="120" align="center">
          <template #default="{ row }">
            <el-button link type="primary" size="small" @click="detailField = row">
              查看{{ row.evidence.length ? `（${row.evidence.length}）` : "" }}
            </el-button>
          </template>
        </el-table-column>
        <el-table-column label="状态" width="150">
          <template #default="{ row }">
            <StatusBadge domain="field" :status="row.status" />
            <el-tag v-if="row.corrected" class="block-gap-sm" type="success" size="small" effect="plain" disable-transitions>
              人工修改
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="190" fixed="right">
          <template #default="{ row }">
            <template v-if="draftOf(row)">
              <el-button link type="primary" size="small" @click="revert(row)">撤销</el-button>
            </template>
            <template v-else>
              <el-button link type="primary" size="small" :disabled="!canSubmit || materialMissing(row)" @click="startEdit(row)">
                {{ adoptedText(row) ? "修改" : "补填" }}
              </el-button>
              <el-button link type="warning" size="small" :disabled="!canSubmit || materialMissing(row)" @click="markMissing(row)">
                材料中不存在
              </el-button>
            </template>
          </template>
        </el-table-column>
      </el-table>

      <div class="review-footer">
        <el-input
          v-model="note"
          type="textarea"
          :rows="2"
          maxlength="2000"
          show-word-limit
          placeholder="填写本次核对说明（修改字段时必填）：核对依据、与原文的差异等"
          class="review-note"
        />
        <div class="review-actions">
          <span v-if="noteRequired" class="text-danger">已暂存 {{ drafts.size }} 项修改，请填写核对说明。</span>
          <span v-else-if="dirty" class="text-hint">已暂存 {{ drafts.size }} 项修改，保存后仍停在等待核对。</span>
          <span v-else class="text-hint">未修改的字段沿用本次抽取值。</span>
          <el-button :disabled="!canSubmit || !dirty || noteRequired" :loading="saving" @click="save">保存修改</el-button>
          <el-button type="primary" :disabled="!canSubmit || noteRequired" :loading="confirming" @click="confirm">
            确认并继续审核
          </el-button>
        </div>
      </div>

      <el-alert
        v-if="remainingMissing.length"
        type="warning"
        show-icon
        :closable="false"
        :title="`仍有 ${remainingMissing.length} 项字段没有可执行取值，继续审核时这些字段不会被补齐`"
      >
        相关规则会按真实缺项记录为无法执行或证据不足，不会自动通过。
      </el-alert>
    </template>

    <AuditFieldDetailDrawer
      :open="Boolean(detailField)"
      :field="detailField"
      :correction-enabled="false"
      @close="detailField = null"
    />
  </div>
</template>

<style scoped>
.review-summary {
  display: flex;
  flex-wrap: wrap;
  gap: 18px;
  color: var(--el-text-color-regular);
  font-size: 13px;
}

.review-summary strong {
  color: var(--el-text-color-primary);
  font-size: 15px;
}

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

.review-footer {
  display: flex;
  flex-direction: column;
  gap: 10px;
  padding-top: 4px;
}

.review-note {
  width: 100%;
}

.review-actions {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 10px;
}

.review-actions .text-hint,
.review-actions .text-danger {
  margin-right: auto;
  font-size: 13px;
}
</style>
