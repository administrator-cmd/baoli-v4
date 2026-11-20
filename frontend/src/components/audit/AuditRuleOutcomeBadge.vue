<script setup lang="ts">
import { computed } from "vue";
import type { AuditRule } from "@/api/contracts";
import StatusBadge from "@/components/StatusBadge.vue";
import { extractionLabels, isExtractionRule, ruleExtractionOutcome } from "@/utils/extractionResult";
import { isReportRuleGenerated, REPORT_GENERATED_LABEL } from "@/utils/reportPresentation";

const props = defineProps<{ rule: AuditRule; size?: "small" | "default" }>();
const outcome = computed(() => ruleExtractionOutcome(props.rule));
</script>

<template>
  <StatusBadge v-if="outcome" :status="rule.status" :size="size" :label="extractionLabels[outcome]" :tone="outcome === 'EXTRACTED' ? 'success' : 'warning'" />
  <span v-else-if="isExtractionRule(rule)" class="text-muted" aria-label="暂无抽取结果">—</span>
  <StatusBadge v-else-if="isReportRuleGenerated(rule)" :status="rule.status" :size="size" :label="REPORT_GENERATED_LABEL" tone="success" />
  <StatusBadge v-else domain="rule" :status="rule.status" :size="size" />
</template>
