<script setup lang="ts">
import { computed, ref, watch } from "vue";
import type { ExecutionDetail } from "@/api/contracts";
import StatusBadge from "@/components/StatusBadge.vue";
import { formatDate, formatDuration } from "@/constants/labels";
import { statusTextOf } from "@/constants/statusVocabulary";
import { executionOutcomes, executionSummaryText, unresolvedText } from "@/utils/executionPresentation";

const props = defineProps<{ detail: ExecutionDetail | null; error: string | null }>();
const keyword = ref("");
const selectedStatus = ref("");
watch(() => props.detail?.execution_id, () => {
  keyword.value = "";
  selectedStatus.value = "";
});
const outcomes = computed(() => executionOutcomes(props.detail?.result_summary ?? null));
const statuses = computed(() => [...new Set(outcomes.value.map((item) => item.status))]);
const filtered = computed(() => outcomes.value.filter((item) =>
  (!selectedStatus.value || item.status === selectedStatus.value)
  && [item.rule_name, ...item.unresolved_items.map((reason) => reason.message ?? reason.kind)]
    .join("\n").toLowerCase().includes(keyword.value.trim().toLowerCase()),
));
const missingCount = computed(() =>
  (props.detail?.result_summary?.missing_rule_keys as string[] | undefined)?.length ?? 0,
);
</script>

<template>
  <div v-loading="!detail && !error" class="execution-detail">
    <el-alert v-if="error" type="error" show-icon :closable="false" :title="error" />
    <template v-else-if="detail">
      <el-descriptions :column="2" border>
        <el-descriptions-item label="执行批次">第 {{ detail.sequence_no }} 次</el-descriptions-item>
        <el-descriptions-item label="执行状态"><StatusBadge :status="detail.status" /></el-descriptions-item>
        <el-descriptions-item label="任务">{{ detail.task_name }}</el-descriptions-item>
        <el-descriptions-item label="耗时">{{ formatDuration(detail.duration_ms) }}</el-descriptions-item>
        <el-descriptions-item label="开始时间">{{ detail.started_at ? formatDate(detail.started_at) : "—" }}</el-descriptions-item>
        <el-descriptions-item label="结束时间">{{ detail.finished_at ? formatDate(detail.finished_at) : "—" }}</el-descriptions-item>
      </el-descriptions>
      <el-alert v-if="detail.invalidated" class="block-gap-sm" type="warning" show-icon :closable="false"
        :title="detail.invalidation_reason || '本次结果已被后续操作取代'" />
      <el-alert v-if="detail.failure_message" class="block-gap-sm" type="error" show-icon :closable="false" :title="detail.failure_message" />
      <p class="execution-summary">{{ executionSummaryText(detail) }}</p>
      <el-alert v-if="missingCount" class="block-gap-sm" type="warning" show-icon :closable="false" :title="`另有 ${missingCount} 项尚无执行结果`" />
      <div v-if="outcomes.length" class="execution-filters">
        <el-input v-model="keyword" clearable placeholder="搜索项目名称或原因" aria-label="搜索执行项目" />
        <el-select v-model="selectedStatus" clearable placeholder="全部结果" aria-label="筛选执行结果">
          <el-option v-for="status in statuses" :key="status" :value="status" :label="statusTextOf('rule', status)" />
        </el-select>
      </div>
      <el-table :data="filtered" border stripe class="management-table" max-height="480" empty-text="暂无匹配的逐项执行结果">
        <el-table-column prop="rule_name" label="执行项目" min-width="200" show-overflow-tooltip />
        <el-table-column label="结果" width="148"><template #default="{ row }"><StatusBadge domain="rule" :status="row.status" /></template></el-table-column>
        <el-table-column label="缺项 / 未完成原因" min-width="280">
          <template #default="{ row }">{{ unresolvedText(row) }}</template>
        </el-table-column>
      </el-table>
      <details class="execution-technical block-gap">
        <summary>技术信息</summary>
        <el-descriptions :column="1" border class="block-gap-sm">
          <el-descriptions-item label="执行节点">{{ detail.node_name }}</el-descriptions-item>
          <el-descriptions-item label="关联操作编号">{{ detail.operation_no ?? '—' }}</el-descriptions-item>
          <el-descriptions-item label="错误码">{{ detail.failure_code ?? '—' }}</el-descriptions-item>
        </el-descriptions>
      </details>
    </template>
  </div>
</template>

<style scoped>
.execution-detail { min-height: 120px; }
.execution-summary { margin: 16px 0; font-weight: 600; }
.execution-filters { display: flex; flex-wrap: wrap; gap: 12px; margin-bottom: 12px; }
.execution-filters .el-input { width: 280px; max-width: 100%; }
.execution-filters .el-select { width: 160px; }
.execution-technical summary { color: var(--el-text-color-secondary); cursor: pointer; }
</style>
