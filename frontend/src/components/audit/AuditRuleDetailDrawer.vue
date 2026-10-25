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
import { statusTone } from "@/constants/labels";
import {
  applicabilityText,
  capabilityOf,
  comparisonOf,
  hasBusinessResult,
  manualEffectiveStatus,
  manualNote,
  manuallyReviewed,
  materialsOf,
  materialRowsOf,
  materialTypeNotice,
  payloadOf,
  reasonText,
  resultStatusLabel,
  routeText,
  ruleCodeOf,
  standardDetail,
  standardText,
  valueSourcesOf,
} from "@/utils/auditRulePresentation";

const props = defineProps<{ rule: AuditRule | null }>();
defineEmits<{ close: [] }>();
const detailRule = computed(() => props.rule);

const detailPayload = computed(() => (detailRule.value ? payloadOf(detailRule.value) : {}));
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
const detailCalculationSteps = computed(() => {
  const steps = detailPayload.value.calculation_steps;
  return Array.isArray(steps) ? steps.filter((step): step is string => typeof step === "string") : [];
});

/** 详情抽屉的取值来源：与列表同一份事实，不重新查库也不重新计算。 */
const detailValues = computed(() => (detailRule.value ? valueSourcesOf(detailRule.value) : []));
const detailMaterialRows = computed(() => (detailRule.value ? materialRowsOf(detailRule.value) : []));

/** 抽屉里的一次性读取：避免在模板里反复调用 payloadOf。 */
const detailCapability = computed(() => (detailRule.value ? capabilityOf(detailRule.value) : null));
const detailManualNote = computed(() => (detailRule.value ? manualNote(detailRule.value) : null));
const detailManualStatus = computed(() => (detailRule.value ? manualEffectiveStatus(detailRule.value) : null));
const detailReason = computed(() => (detailRule.value ? reasonText(detailRule.value) : null));
const detailStandardText = computed(() => (detailRule.value ? standardText(detailRule.value) : ""));
const detailStandardDetail = computed(() => (detailRule.value ? standardDetail(detailRule.value) : ""));
const detailComparison = computed(() => (detailRule.value ? comparisonOf(detailRule.value) : null));
const detailFindings = computed(() => (detailPayload.value.tool_findings ?? []) as Array<{
  comparison?: RuleCheckComparison;
  per_material?: RuleInputSnapshot[];
}>);
const detailIsReviewed = computed(() => (detailRule.value ? manuallyReviewed(detailRule.value) : false));
const detailTypeNotice = computed(() => (detailRule.value ? materialTypeNotice(detailRule.value) : null));
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
        <StatusBadge :status="detailManualStatus ?? detailRule.status" :label="resultStatusLabel(detailRule)" :tone="statusTone(detailManualStatus ?? detailRule.status)" size="default" />
        <span v-if="detailRule.source_sheet" class="text-hint">{{ detailRule.source_sheet }}</span>
        <span class="text-hint">执行方式：{{ executionModeLabel(detailRule.execution_mode) }}</span>
        <span v-if="detailRuleCode" class="text-hint mono">{{ detailRuleCode }}</span>
        <span class="text-muted">{{ materialsOf(detailRule) }}</span>

        <span v-if="detailTypeNotice" class="text-hint">{{ detailTypeNotice }}</span>
      </div>

      <div class="detail-block detail-conclusion">
        {{ detailIsReviewed ? detailManualNote || "本次人工复核没有记录说明" : auditResultSummary(detailRule.status, detailRule.message, detailPayload) }}
        <div v-if="detailReason" class="text-muted">{{ detailReason }}</div>
      </div>
      <div v-if="detailRule.kind === 'REPORT' && !detailBasisCount" class="text-muted detail-block">
        报告正文在「报告」页签查看。
      </div>
      <AuditBusinessDetails :result="detailPayload" />

      <div v-if="detailIsReviewed" class="text-muted detail-block">系统原说明：{{ detailRule.message || "未记录" }}</div>

      <!-- 1. 审核标准 -->
      <h4 class="app-section-title">审核标准</h4>
      <div class="detail-block">{{ detailStandardText }}</div>
      <div v-if="detailStandardDetail" class="text-muted detail-block">{{ detailStandardDetail }}</div>

      <!-- 2. 原文内容与标准化结果 -->
      <h4 v-if="showValueTableInMain && (detailValues.length || detailMaterialRows.length)" class="app-section-title">实际取值</h4>
      <RuleInputSnapshotTable
        v-if="showValueTableInMain"
        :material-rows="detailMaterialRows"
        :values="detailValues"
      />

      <!-- 3. 实际比较：跨材料并排展示双方内容；金额与日期给出真实算式 -->
      <h4 v-if="detailComparison" class="app-section-title">实际比较</h4>
      <CrossComparisonPanel
        v-if="detailComparison"
        :comparison="detailComparison"
        :rule-name="detailRule.name"
      />
      <template v-for="(finding, index) in detailFindings" :key="index">
        <template v-if="finding.comparison">
          <h4 class="app-section-title">实际比较</h4>
          <CrossComparisonPanel :comparison="finding.comparison" :rule-name="detailRule.name" />
        </template>
        <template v-if="finding.per_material?.length">
          <h4 class="app-section-title">逐份材料检查</h4>
          <el-table :data="finding.per_material" size="small" border>
            <el-table-column prop="material_name" label="材料" min-width="130" />
            <el-table-column prop="filename" label="文件" min-width="180" />
            <el-table-column label="结论" width="120"><template #default="{ row }"><StatusBadge :status="row.status" /></template></el-table-column>
            <el-table-column prop="message" label="检查说明" min-width="240" />
          </el-table>
        </template>
      </template>

      <template v-if="detailCalculationSteps.length">
        <h4 class="app-section-title">AI 计算说明</h4>
        <ol class="detail-block">
          <li v-for="(step, index) in detailCalculationSteps" :key="index">{{ step }}</li>
        </ol>
      </template>

      <!-- 5. 原文依据 -->
      <h4 v-if="detailRule.evidence.length" class="app-section-title">{{ detailEvidenceTitle }}</h4>
      <p v-if="detailEvidenceNote" class="rule-evidence-note">{{ detailEvidenceNote }}</p>
      <div v-for="(source, index) in detailRule.evidence" :key="index" class="rule-source">
        <div class="text-muted">
          {{ source.material_name || "来源材料未记录" }}
          <template v-if="source.document_name"> · {{ source.document_name }}</template>
          <template v-if="source.field_path"> · {{ source.field_path }}</template>
        </div>
        <QuoteBlock v-if="source.quote_text" :text="source.quote_text" />
        <div v-else class="text-muted">该引用记录产物来源，未提供具体原文。</div>
      </div>

      <div v-if="detailRule.status === 'PENDING_REVIEW'" class="text-muted detail-block">
        该项业务尚未确认；人工审核暂不启用，后续规则继续执行。
      </div>
      <!-- 已有人工记录仍按实际内容展示。 -->
      <template v-if="detailIsReviewed || detailRule.status === 'REVIEWED'">
        <h4 class="app-section-title">人工复核记录</h4>
        <div v-if="detailManualNote" class="detail-block">{{ detailManualNote }}</div>
        <div v-if="detailManualStatus" class="detail-block">
          复核后有效状态：<StatusBadge :status="detailManualStatus" :label="resultStatusLabel(detailRule)" />
        </div>
      </template>

      <!-- 7. 技术执行详情：能力与材料范围等辅助信息 -->
      <el-collapse class="block-gap">
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
          <template v-if="Array.isArray(detailPayload.execution_issues) && detailPayload.execution_issues.length">
            <div class="text-muted">执行过程中出现的历史问题（不等同于最终业务缺项）：</div>
            <pre>{{ JSON.stringify(detailPayload.execution_issues, null, 2) }}</pre>
          </template>
          <div class="text-muted">适用条件：{{ applicabilityText(detailRule) }}</div>
          <div class="text-muted">判定方式：{{ routeText(detailRule) }}</div>
          <div class="text-muted">
            引用原文：{{ detailRule.evidence.length ? `已保存 ${detailRule.evidence.length} 段` : "未保存" }}
          </div>
          <div class="text-muted">
            判定能力：
            <template v-if="detailCapability">
              {{ detailCapability.label || detailCapability.capability_id || "未记录" }}
              <span class="mono">（{{ detailCapability.capability_id }} · v{{ detailCapability.version }}）</span>
              <template v-if="detailCapability.known === false">｜该能力不在系统能力目录中</template>
              <template v-else-if="detailCapability.implemented === false">
                ｜当前未实现：{{ detailCapability.unimplemented_reason || "未说明原因" }}
              </template>
            </template>
            <template v-else>本条结果未记录判定能力</template>
          </div>
          <div v-if="detailRule.rule_key" class="mono">规则键：{{ detailRule.rule_key }}</div>
          <template v-if="Array.isArray(detailPayload.verdict_errors) && detailPayload.verdict_errors.length">
            <div class="text-muted">
              结论格式修正记录（接口错误）：
            </div>
            <pre>{{ JSON.stringify(detailPayload.verdict_errors, null, 2) }}</pre>
          </template>
        </el-collapse-item>
      </el-collapse>
    </template>
  </DrawerPanel>
</template>

<style scoped>
.detail-conclusion {
  padding: 12px 16px;
  background: var(--el-fill-color-light);
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}
.original-execution-message {
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}

.rule-source + .rule-source {
  margin-top: 12px;
}

.rule-evidence-note {
  margin: 0 0 8px;
  color: var(--el-text-color-secondary);
  font-size: 13px;
  line-height: 1.6;
}

.detail-head {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
  margin-bottom: 8px;
}

.detail-block {
  margin-bottom: 8px;
  line-height: 1.7;
}
</style>
