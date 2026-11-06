<script setup lang="ts">
import type { ReportGenerationInfo } from "@/api/contracts/reports";

defineProps<{ generation?: ReportGenerationInfo | null }>();
</script>

<template>
  <el-alert
    v-if="generation?.fallback_reason"
    type="warning"
    show-icon
    :closable="false"
    :title="generation.fallback_reason"
    :description="generation.template_name ? `原选模板：${generation.template_name}` : undefined"
  />
  <div v-else-if="generation" class="report-generation-mode">
    生成方式：{{ generation.mode === "TEMPLATE" ? "仿照模板" : "按规则生成" }}
    <span v-if="generation.mode === 'TEMPLATE' && generation.template_name"> · {{ generation.template_name }}</span>
  </div>
</template>

<style scoped>
.report-generation-mode {
  overflow-wrap: anywhere;
}
</style>
