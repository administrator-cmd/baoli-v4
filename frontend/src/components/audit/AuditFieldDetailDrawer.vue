<script setup lang="ts">
import { computed } from "vue";

import type { AuditField } from "@/api/contracts";
import DrawerPanel from "@/components/DrawerPanel.vue";
import QuoteBlock from "@/components/audit/QuoteBlock.vue";
import StatusBadge from "@/components/StatusBadge.vue";
import { factOriginLabel, periodScopeLabel, statementScopeLabel } from "@/constants/labels";
import {
  evidenceNote,
  evidenceTitle,
  extractionOutcome,
  extractionOutcomeCode,
  fieldMode,
  fieldReason,
  fieldSourceFiles,
  formatFieldValue,
  reasonTitle,
  type FieldMode,
} from "@/utils/fieldPresentation";

/** 字段详情：按字段状态决定展示取值、原文还是未抽取原因。 */
const props = defineProps<{
  open: boolean;
  field: AuditField | null;
  correctionEnabled?: boolean;
}>();

const emit = defineEmits<{
  close: [];
  correct: [field: AuditField];
}>();

const mode = computed<FieldMode>(() => (props.field ? fieldMode(props.field) : "NOT_RUN"));
const declaredScope = computed(() => props.field ? declaredScopeText(props.field) : "");
const recognizedScope = computed(() => props.field ? recognizedScopeText(props.field) : "");
const reason = computed(() => {
  const field = props.field;
  if (!field) return "";
  return mode.value === "UNNORMALIZED" ? normalizedText(field) : fieldReason(field);
});

/** 原文内容。没有原文时说明属于哪一种缺失，不留空白。 */
function rawText(field: AuditField): string {
  if (field.corrected || field.origin === "HUMAN_CORRECTION") {
    return field.extracted_raw_value !== null && field.extracted_raw_value !== undefined && field.extracted_raw_value !== ""
      ? field.extracted_raw_value : "原抽取未取得原始值";
  }
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
  if (field.status === "EVIDENCE_UNCLEAR" || field.status === "EVIDENCE_INSUFFICIENT") {
    return "抽取结果没有给出原文，未能形成可核对的事实";
  }
  return "服务端没有返回该字段的原文";
}

/** 标准化结果。它是按字段类型对原文确定性算出的可比对值，不代表审核通过。 */
function normalizedText(field: AuditField): string {
  const normalized = field.normalized_value;
  if (normalized !== null && normalized !== undefined && normalized !== "") {
    return formatFieldValue(field);
  }
  if (field.status === "NORMALIZATION_FAILED") {
    return "原文无法按字段类型标准化，未形成可比对的值";
  }
  if (field.raw_value !== null && field.raw_value !== undefined && field.raw_value !== "") {
    return "服务端没有返回标准化结果";
  }
  return "没有可标准化的原文";
}

function hasNormalizedValue(field: AuditField): boolean {
  return field.normalized_value !== null && field.normalized_value !== undefined && field.normalized_value !== "";
}

/** 原始值与标准化结果文本相同时只保留一个区块，避免重复展示。 */
function showRawSection(field: AuditField): boolean {
  if (field.corrected || field.origin === "HUMAN_CORRECTION") {
    return !(hasNormalizedValue(field) && rawText(field) === normalizedText(field));
  }
  if (mode.value !== "VALUE" && mode.value !== "UNNORMALIZED") return false;
  return !(mode.value === "VALUE" && hasNormalizedValue(field) && rawText(field) === normalizedText(field));
}

function reasonTone(current: FieldMode): string {
  if (current === "FAILED") return "detail-block--danger";
  if (current === "NOT_FOUND" || current === "UNCERTAIN" || current === "UNNORMALIZED") return "detail-block--warning";
  return "detail-block--muted";
}

/** 来源文件只取该结果实际证据对应的上传文件，不用材料位名称冒充。 */
function sourceFilesText(field: AuditField): string {
  return fieldSourceFiles(field).join("、") || "未记录来源文件";
}

const emptyScopeValues = new Set(["NOT_APPLICABLE", "N/A", "不适用", "未识别", "未声明", "—", "-"]);

/** 只整理展示文本；缺失或不适用的元信息不占一行，识别值与配置要求仍分开。 */
function scopePart(label: string, value: string | null | undefined, format = (text: string) => text): string | null {
  const text = value?.trim();
  return text && !emptyScopeValues.has(text.toUpperCase()) ? `${label} ${format(text)}` : null;
}

function declaredScopeText(field: AuditField): string {
  return [
    scopePart("期间", field.period_scope, periodScopeLabel),
    scopePart("报表口径", field.statement_scope, statementScopeLabel),
    scopePart("主体", field.entity_scope === "SELF" ? null : field.entity_scope),
    scopePart("单位", field.unit),
    scopePart("币种", field.currency),
  ].filter(Boolean).join(" · ");
}

function recognizedScopeText(field: AuditField): string {
  return [
    scopePart("期间", field.actual_period_scope),
    scopePart("报表口径", field.actual_statement_scope),
    scopePart("主体", field.actual_entity_scope === "SELF" ? null : field.actual_entity_scope),
    scopePart("单位", field.actual_unit),
  ].filter(Boolean).join(" · ");
}
</script>

<template>
  <DrawerPanel :open="open" :title="field?.name ?? '字段详情'" size="min(680px, 100%)" @close="emit('close')">
    <template v-if="field">
      <div class="field-detail-meta">
        <StatusBadge v-if="extractionOutcome(field)" :status="field.status" :label="extractionOutcome(field)" :tone="extractionOutcomeCode(field) === 'EXTRACTED' ? 'success' : 'warning'" />
        <span v-if="field.source_ref" class="text-muted">字段编号：{{ field.source_ref }}</span>
        <span v-if="field.fact_revision_no" class="text-muted">第 {{ field.fact_revision_no }} 版</span>
      </div>

      <section v-if="mode === 'VALUE'" class="detail-section">
        <h4 class="detail-section__title">{{ field.origin === 'HUMAN_CORRECTION' || field.status === 'REVIEWED' ? '人工确认结果' : '取值结果' }}</h4>
        <div
          class="detail-block detail-block--accent field-result-value"
          :class="{ 'detail-block--muted': !hasNormalizedValue(field) }"
        >
          {{ normalizedText(field) }}
        </div>
      </section>

      <section v-if="showRawSection(field)" class="detail-section">
        <h4 class="detail-section__title">抽取原始值</h4>
        <div class="detail-block">{{ rawText(field) }}</div>
      </section>

      <section v-if="mode !== 'VALUE'" class="detail-section">
        <h4 class="detail-section__title">{{ mode === 'FAILED' ? '未抽取到的原因' : reasonTitle(mode) }}</h4>
        <div class="detail-block" :class="reasonTone(mode)">{{ reason }}</div>
      </section>

      <section class="detail-section">
        <h4 class="detail-section__title">字段来源</h4>
        <dl class="detail-facts">
          <dt>规则要求</dt>
          <dd>{{ field.material_name?.trim() ? `规定从「${field.material_name.trim()}」中抽取` : '未记录规定的来源材料' }}</dd>
          <dt>实际来源文件</dt>
          <dd>{{ sourceFilesText(field) }}</dd>
        </dl>
      </section>

      <section v-if="field.evidence.length || mode !== 'NOT_RUN'" class="detail-section">
        <h4 class="detail-section__title">{{ evidenceTitle(mode) }}</h4>
        <p v-if="field.evidence.length && evidenceNote(mode)" class="detail-note">{{ evidenceNote(mode) }}</p>
        <div v-if="field.evidence.length" class="evidence-list">
          <div v-for="(item, index) in field.evidence" :key="index" class="evidence-item">
            <div class="evidence-source">
              <span>{{ item.document_name || "来源文件未记录" }}</span>
              <span v-if="item.field_path"> · {{ item.field_path }}</span>
            </div>
            <QuoteBlock
              :text="item.quote_text"
              :label="item.quote_label"
              :header="item.quote_header"
              :period="item.quote_period"
              :statement-scope="item.quote_statement_scope"
              :entity-scope="item.quote_entity_scope"
              :unit="item.quote_unit"
              :page="item.artifact_page"
            />
          </div>
        </div>
        <div v-else class="detail-block detail-block--muted">未记录原文片段</div>
      </section>

      <section class="detail-section">
        <h4 class="detail-section__title">结果信息</h4>
        <dl class="detail-facts">
          <template v-if="field.value_source || field.origin">
            <dt>取值方式</dt>
            <dd>{{ factOriginLabel(field.value_source || field.origin) }}</dd>
          </template>
          <template v-if="recognizedScope">
            <dt>识别口径</dt>
            <dd>{{ recognizedScope }}</dd>
          </template>
          <template v-if="declaredScope">
            <dt>配置口径</dt>
            <dd>{{ declaredScope }}</dd>
          </template>
          <template v-if="field.manual_review?.business_note">
            <dt>人工说明</dt>
            <dd>{{ field.manual_review.business_note }}</dd>
          </template>
          <template v-if="field.instruction">
            <dt>抽取要求</dt>
            <dd>{{ field.instruction }}</dd>
          </template>
          <template v-if="mode === 'VALUE' && field.extraction_message && field.extraction_presentation_version === 2">
            <dt>抽取说明</dt>
            <dd>{{ field.extraction_message }}</dd>
          </template>
          <template v-if="mode === 'NOT_RUN'">
            <dt>自动抽取</dt>
            <dd>{{ field.extraction_enabled === undefined ? "未记录" : field.extraction_enabled ? "已启用" : "未启用" }}</dd>
            <dt>材料状态</dt>
            <dd>{{ field.material_uploaded === undefined ? "未记录" : field.material_uploaded ? "已上传" : "未上传" }}</dd>
          </template>
        </dl>
      </section>

      <el-collapse
        v-if="mode === 'VALUE' && field.extraction_message && field.extraction_presentation_version !== 2"
        class="detail-section"
      >
        <el-collapse-item title="原执行说明（该结果尚未保存结构化说明）" name="original">
          <div class="detail-value">{{ field.extraction_message }}</div>
        </el-collapse-item>
      </el-collapse>
    </template>
    <template #footer>
      <div v-if="field && correctionEnabled" class="field-detail-actions">
        <el-button type="primary" @click="emit('correct', field)">纠正字段</el-button>
      </div>
    </template>
  </DrawerPanel>
</template>

<style scoped>
.field-detail-meta {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 10px;
  margin-bottom: 20px;
}

.detail-section + .detail-section {
  margin-top: 20px;
}

.detail-section__title {
  position: relative;
  margin: 0 0 8px;
  padding-left: 10px;
  color: var(--el-text-color-primary);
  font-size: 14px;
  font-weight: 600;
  line-height: 1.4;
}

.detail-section__title::before {
  content: "";
  position: absolute;
  top: 3px;
  left: 0;
  width: 3px;
  height: 14px;
  border-radius: 2px;
  background: var(--el-color-primary);
}

.detail-block {
  padding: 12px 14px;
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 6px;
  background: var(--el-fill-color-lighter);
  color: var(--el-text-color-primary);
  font-size: 14px;
  line-height: 1.75;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}

.detail-block--accent {
  border-color: var(--el-color-primary-light-7);
  background: var(--el-color-primary-light-9);
}

.field-result-value {
  font-weight: 500;
  font-variant-numeric: lining-nums tabular-nums;
}

.detail-block--muted {
  color: var(--el-text-color-secondary);
  background: var(--el-fill-color-lighter);
}

.detail-block--warning {
  border-color: var(--el-color-warning-light-7);
  background: var(--el-color-warning-light-9);
}

.detail-block--danger {
  border-color: var(--el-color-danger-light-7);
  background: var(--el-color-danger-light-9);
}

.detail-note {
  margin: 0 0 8px;
  color: var(--el-text-color-secondary);
  font-size: 13px;
  line-height: 1.6;
}

.evidence-item {
  overflow: hidden;
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 6px;
}

.evidence-item + .evidence-item {
  margin-top: 10px;
}

.evidence-source {
  padding: 8px 12px;
  background: var(--el-fill-color-lighter);
  color: var(--el-text-color-secondary);
  font-size: 12px;
  overflow-wrap: anywhere;
}

.detail-facts {
  display: grid;
  grid-template-columns: 88px minmax(0, 1fr);
  gap: 8px 12px;
  margin: 0;
  padding: 12px 14px;
  border-radius: 6px;
  background: var(--el-fill-color-lighter);
  font-size: 13px;
  line-height: 1.6;
}

.detail-facts dt {
  color: var(--el-text-color-secondary);
}

.detail-facts dd {
  margin: 0;
  color: var(--el-text-color-primary);
  overflow-wrap: anywhere;
}

.field-detail-actions {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
}

.detail-value {
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  line-height: 1.55;
}
</style>
