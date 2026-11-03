<script setup lang="ts">
import { computed } from "vue";

import type { AuditRule, JsonValue, RuleCheckComparison, RuleInputSnapshot } from "@/api/contracts";
import CrossComparisonPanel from "@/components/audit/CrossComparisonPanel.vue";
import AuditBusinessDetails from "@/components/audit/AuditBusinessDetails.vue";
import QuoteBlock from "@/components/audit/QuoteBlock.vue";
import RuleInputSnapshotTable from "@/components/audit/RuleInputSnapshotTable.vue";
import DrawerPanel from "@/components/DrawerPanel.vue";
import StatusBadge from "@/components/StatusBadge.vue";
import { auditResultSummary } from "@/constants/auditStatus";
import { executionModeLabel } from "@/constants/executionModes";
import {
  hasBusinessResult,
  materialRowsOf,
  payloadOf,
  ruleCodeOf,
  sourceFilesOf,
  standardDetail,
  standardText,
  valueSourcesOf,
} from "@/utils/auditRulePresentation";

const props = defineProps<{ rule: AuditRule | null }>();
defineEmits<{ close: [] }>();
const detailRule = computed(() => props.rule);

const detailPayload = computed(() => (detailRule.value ? payloadOf(detailRule.value) : {}));
const inputResolutions = computed(() => {
  const items = detailPayload.value.input_resolutions;
  if (!Array.isArray(items)) return [];
  return items.filter((item): item is Record<string, JsonValue> => item !== null && typeof item === "object" && !Array.isArray(item))
    .map((item) => {
      const reference = item.evidence;
      const evidence: Record<string, JsonValue> = reference && typeof reference === "object" && !Array.isArray(reference) ? reference : {};
      const source = detailRule.value?.evidence.find((entry) => evidence.artifact_id != null && entry.artifact_id === evidence.artifact_id);
      return {
        label: String(item.missing_input || item.parameter_code || "补取输入"),
        value: displayInputValue(item.value),
        rawValue: typeof item.raw_value === "string" ? item.raw_value : "未记录原文值",
        source: source?.document_name || (evidence.fact_id != null ? "已保存事实" : "未记录来源文件"),
        evidence,
      };
    });
});

function displayInputValue(value: JsonValue | undefined): string {
  if (value === null || value === undefined) return "未取得值";
  if (typeof value === "boolean") return value ? "是" : "否";
  return typeof value === "object" ? JSON.stringify(value) : String(value);
}

function quoteFragment(evidence: Record<string, JsonValue>, key: string): string | null {
  return typeof evidence[key] === "string" ? evidence[key] as string : null;
}
const detailBasisCount = computed(() => {
  const basis = detailPayload.value.basis;
  return Array.isArray(basis) ? basis.length : 0;
});
/** 抽取规则的产出就是取值本身；其余规则只要已有判断依据，输入快照就下沉到技术详情。 */
const showValueTableInMain = computed(() => {
  if (!detailRule.value) {
    return false;
  }
  return detailRule.value.kind === "EXTRACTION" || detailBasisCount.value === 0;
});
const detailRuleCode = computed(() => detailRule.value ? ruleCodeOf(detailRule.value) : null);

/** 详情抽屉的取值来源：与列表同一份事实，不重新查库也不重新计算。 */
const detailValues = computed(() => (detailRule.value ? valueSourcesOf(detailRule.value) : []));
const detailMaterialRows = computed(() => (detailRule.value ? materialRowsOf(detailRule.value) : []));

const detailStandardText = computed(() => (detailRule.value ? standardText(detailRule.value) : ""));
const detailStandardDetail = computed(() => (detailRule.value ? standardDetail(detailRule.value) : ""));

const detailFindings = computed(() => (detailPayload.value.tool_findings ?? []) as Array<{
  comparison?: RuleCheckComparison;
  per_material?: RuleInputSnapshot[];
}>);
/** 未解决事项：结论转人工或阻断的真实原因。 */
const detailUnresolvedItems = computed<Record<string, JsonValue>[]>(() => {
  const value = detailPayload.value.unresolved_items;
  if (!Array.isArray(value)) {
    return [];
  }
  return value.filter((item): item is Record<string, JsonValue> => Boolean(item) && typeof item === "object" && !Array.isArray(item));
});
const detailEvidenceTitle = computed(() => {
  const status = detailRule.value?.status;
  if (status === "EVIDENCE_INSUFFICIENT") return "已读取的原文";
  if (status === "NOT_EXECUTED" || status === "PENDING") return "已有原文";
  return "原文依据";
});
const detailEvidenceNote = computed(() => (
  detailRule.value?.status === "EVIDENCE_INSUFFICIENT" ? "以下原文不足以形成确定结论。" : ""
));
/** 同一上传版本的引用合并成一组；同名文件的不同上传不能合并。 */
const evidenceGroups = computed(() => {
  const groups: Array<{ source: string; items: Array<{ key: string; label: string; quote: string; quoteLabel?: string | null; quoteHeader?: string | null; quotePeriod?: string | null; statementScope?: string | null; entityScope?: string | null; unit?: string | null; page?: number | null }> }> = [];
  const positions = new Map<string, number>();
  for (const item of detailRule.value?.evidence ?? []) {
    const sourceParts = [item.document_name || "来源文件未记录"];
    if (!item.document_name && item.material_name) sourceParts.push(`材料位：${item.material_name}`);
    const source = sourceParts.join(" · ");
    const identity = JSON.stringify([item.document_upload_id, item.artifact_id, item.document_name, item.material_name]);
    let position = positions.get(identity);
    if (position === undefined) {
      position = groups.length;
      positions.set(identity, position);
      groups.push({ source, items: [] });
    }
    groups[position].items.push({
      key: `${position}-${groups[position].items.length}`,
      label: item.field_path ?? "",
      quote: item.quote_text,
      quoteLabel: item.quote_label,
      quoteHeader: item.quote_header,
      quotePeriod: item.quote_period,
      statementScope: item.quote_statement_scope,
      entityScope: item.quote_entity_scope,
      unit: item.quote_unit,
      page: item.artifact_page,
    });
  }
  return groups;
});
/** 三项技术信息都取不到时整块不渲染，避免出现点开是空白的折叠项。 */
const hasTechnicalDetail = computed(() => {
  if (!detailRule.value) {
    return false;
  }
  const hasSnapshot = !showValueTableInMain.value
    && (detailMaterialRows.value.length > 0 || detailValues.value.length > 0);
  return hasSnapshot || !hasBusinessResult(detailRule.value) || detailUnresolvedItems.value.length > 0;
});
</script>

<template>
  <!-- 详情先展示结论，再查看标准与取值；内部执行信息默认折叠。 -->
  <DrawerPanel
    :open="Boolean(detailRule)"
    :title="detailRule?.name ?? '检查详情'"
    size="min(900px, 100%)"
    @close="$emit('close')"
  >
    <template v-if="detailRule">
      <div class="detail-head">
        <StatusBadge domain="rule" :status="detailRule.status" size="default" />
        <span v-if="detailRuleCode" class="mono detail-head__code">{{ detailRuleCode }}</span>
        <span class="detail-head__meta">执行方式：{{ executionModeLabel(detailRule.execution_mode) }}</span>
        <span v-if="detailRule.source_sheet" class="detail-head__sheet">{{ detailRule.source_sheet }}</span>
      </div>

      <section class="detail-section detail-section--lead">
        <h4 class="app-section-title">检查结论</h4>
        <div class="detail-conclusion">{{ auditResultSummary(detailRule.status, detailRule.message, detailPayload) }}</div>
        <div v-if="detailRule.kind === 'REPORT' && !detailBasisCount" class="text-muted detail-note">
          报告正文在「报告」页签查看。
        </div>
        <div v-if="detailRule.status === 'PENDING_REVIEW'" class="text-muted detail-note">
          该项业务尚未确认；人工审核暂不启用，后续规则继续执行。
        </div>
      </section>

      <section v-if="detailRule.input_gaps?.length" class="detail-section">
        <h4 class="app-section-title">配置输入缺项</h4>
        <div v-for="(gap, index) in (detailRule.input_gaps ?? [])" :key="index" class="text-muted detail-note">
          {{ gap.missing_input || "未命名输入" }}<template v-if="gap.parameter_code">（参数 {{ gap.parameter_code }}）</template>：{{ gap.reason || "未建立绑定" }}
        </div>
      </section>

      <section class="detail-section">
        <h4 class="app-section-title">审核标准</h4>
        <div class="detail-standard">{{ detailStandardText }}</div>
        <div v-if="detailStandardDetail" class="text-muted detail-note">{{ detailStandardDetail }}</div>
      </section>

      <AuditBusinessDetails
        class="detail-section"
        :result="detailPayload"
        :source-files="sourceFilesOf(detailRule)"
        :material-names="detailRule.material_names ?? []"
      />

      <section v-if="showValueTableInMain" class="detail-section">
        <h4 v-if="detailValues.length || detailMaterialRows.length" class="app-section-title">实际取值</h4>
        <RuleInputSnapshotTable :material-rows="detailMaterialRows" :values="detailValues" />
      </section>

      <section v-if="inputResolutions.length" class="detail-section">
        <h4 class="app-section-title">本次补取输入</h4>
        <el-table :data="inputResolutions" size="small" border>
          <el-table-column prop="label" label="输入项" min-width="140" />
          <el-table-column prop="value" label="采用值" min-width="120" />
          <el-table-column prop="rawValue" label="原文值" min-width="120" />
          <el-table-column prop="source" label="来源" min-width="180" />
          <el-table-column label="原文依据" min-width="240">
            <template #default="{ row }">
              <QuoteBlock
                v-if="quoteFragment(row.evidence, 'quote_text')"
                :text="quoteFragment(row.evidence, 'quote_text')"
                :label="quoteFragment(row.evidence, 'quote_label')"
                :header="quoteFragment(row.evidence, 'quote_header')"
                :period="quoteFragment(row.evidence, 'quote_period')"
                :statement-scope="quoteFragment(row.evidence, 'quote_statement_scope')"
                :entity-scope="quoteFragment(row.evidence, 'quote_entity_scope')"
                :unit="quoteFragment(row.evidence, 'quote_unit')"
              />
              <span v-else class="text-muted">引用已保存事实，原文见下方依据</span>
            </template>
          </el-table-column>
        </el-table>
      </section>

      <!-- 实际比较：只展示工具实际保存的比较结果 -->
      <template v-for="(finding, index) in detailFindings" :key="index">
        <section v-if="finding.comparison" class="detail-section">
          <h4 class="app-section-title">实际比较</h4>
          <CrossComparisonPanel :comparison="finding.comparison" :rule-name="detailRule.name" />
        </section>
        <section v-if="finding.per_material?.length" class="detail-section">
          <h4 class="app-section-title">逐份材料检查</h4>
          <el-table :data="finding.per_material" size="small" border>
            <el-table-column prop="material_name" label="材料" min-width="130" />
            <el-table-column prop="filename" label="文件" min-width="180" />
            <!-- 结论列要容纳最长的状态文案「已产出（无结论）」，列宽不足时标签会向右溢出压到列边界。 -->
            <el-table-column label="结论" width="184"><template #default="{ row }"><StatusBadge domain="rule" :status="row.status" /></template></el-table-column>
            <el-table-column prop="message" label="检查说明" min-width="240" />
          </el-table>
        </section>
      </template>

      <section v-if="evidenceGroups.length" class="detail-section">
        <h4 class="app-section-title">{{ detailEvidenceTitle }}</h4>
        <p v-if="detailEvidenceNote" class="rule-evidence-note">{{ detailEvidenceNote }}</p>
        <div v-for="(group, groupIndex) in evidenceGroups" :key="groupIndex" class="evidence-group">
          <div class="evidence-group__source">{{ group.source }}</div>
          <div class="evidence-group__list">
            <div v-for="item in group.items" :key="item.key" class="evidence-group__item">
              <div v-if="item.label" class="evidence-group__label">{{ item.label }}</div>
              <QuoteBlock
                v-if="item.quote"
                :text="item.quote"
                :label="item.quoteLabel"
                :header="item.quoteHeader"
                :period="item.quotePeriod"
                :statement-scope="item.statementScope"
                :entity-scope="item.entityScope"
                :unit="item.unit"
                :page="item.page"
              />
              <div v-else class="text-muted">该引用记录产物来源，未提供具体原文。</div>
            </div>
          </div>
        </div>
      </section>

      <!-- 技术执行详情：能力与材料范围等辅助信息 -->
      <el-collapse v-if="hasTechnicalDetail" class="block-gap">
        <el-collapse-item title="技术执行详情（辅助信息）" name="tech">
          <div v-if="!showValueTableInMain && (detailMaterialRows.length || detailValues.length)" class="detail-block">
            <div class="text-muted">输入快照：</div>
            <RuleInputSnapshotTable :material-rows="detailMaterialRows" :values="detailValues" />
          </div>
          <template v-if="!hasBusinessResult(detailRule)">
            <div class="text-muted">原执行说明：</div>
            <div class="detail-block original-execution-message">{{ detailRule.message || "未记录" }}</div>
          </template>
          <div v-if="detailUnresolvedItems.length" class="detail-block">
            <div class="text-muted">最终缺项 / 未完成原因：</div>
            <ul><li v-for="(item, index) in detailUnresolvedItems" :key="index">{{ item.kind }}：{{ item.message }}</li></ul>
          </div>
        </el-collapse-item>
      </el-collapse>
    </template>
  </DrawerPanel>
</template>

<style scoped>
/* 每个区块之间留同一份间距，标题不再各自带上下留白 */
.detail-section {
  margin-top: 24px;
}

.detail-section--lead {
  margin-top: 0;
}

:deep(.app-section-title) {
  margin: 0 0 10px;
}

.detail-head {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px 12px;
  margin-bottom: 16px;
}

.detail-head__code,
.detail-head__meta {
  color: var(--el-text-color-secondary);
  font-size: 12px;
}

.detail-head__sheet {
  padding: 1px 8px;
  border-radius: 10px;
  background: var(--el-fill-color-light);
  color: var(--el-text-color-secondary);
  font-size: 12px;
}

/* 结论是抽屉里最该先看到的内容 */
.detail-conclusion {
  padding: 12px 16px;
  background: var(--el-fill-color-light);
  border-left: 3px solid var(--el-color-primary-light-5);
  border-radius: 0 4px 4px 0;
  line-height: 1.7;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}

/* 审核标准单独成块，与下面的依据正文区分开 */
.detail-standard {
  padding: 10px 14px;
  background: var(--el-fill-color-lighter);
  border-radius: 4px;
  line-height: 1.75;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}

.detail-note {
  margin-top: 8px;
}

.original-execution-message {
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}

.rule-evidence-note {
  margin: 0 0 8px;
  color: var(--el-text-color-secondary);
  font-size: 13px;
  line-height: 1.6;
}

/* 原文依据：同材料同文件的来源只写一次，多条引用排成带分隔线的一栏 */
.evidence-group + .evidence-group {
  margin-top: 16px;
}

.evidence-group__source {
  margin-bottom: 6px;
  color: var(--el-text-color-secondary);
  font-size: 12px;
}

.evidence-group__list {
  border-left: 3px solid var(--el-border-color);
  border-radius: 0 4px 4px 0;
  background: var(--el-fill-color-lighter);
}

.evidence-group__item {
  padding: 8px 12px;
}

.evidence-group__item + .evidence-group__item {
  border-top: 1px solid var(--el-border-color-lighter);
}

.evidence-group__label {
  margin-bottom: 4px;
  color: var(--el-text-color-regular);
  font-size: 13px;
}

.evidence-group__item :deep(.evidence-quote) {
  padding: 0;
}

.detail-block {
  margin-bottom: 8px;
  line-height: 1.7;
}
</style>
