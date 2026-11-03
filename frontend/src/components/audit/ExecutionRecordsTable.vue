<script setup lang="ts">
import type { ExecutionRow } from "@/api/contracts";
import StatusBadge from "@/components/StatusBadge.vue";
import { formatDate, formatDuration } from "@/constants/labels";
import {
  MODEL_TURNS_HINT,
  executionProgressText,
  executionStatusLabel,
  executionStatusTone,
  modelTurnsText,
} from "@/utils/executionPresentation";

/**
 * 执行记录列表：任务详情页与独立执行记录页共用。
 *
 * 列表只回答批次执行到哪里、是否仍在阻断；完整失败原因由接口保留，不在表格里展开，
 * 避免长文本把行高撑开，也避免历史批次逐行重复同一段渠道故障说明。
 */
withDefaults(defineProps<{
  rows: ExecutionRow[];
  loading?: boolean;
  /** 跨任务列表需要保留任务身份；任务详情页里任务相同，不再重复一列。 */
  showTask?: boolean;
  emptyText?: string;
}>(), {
  loading: false,
  showTask: false,
  emptyText: "暂无执行记录",
});

const emit = defineEmits<{
  openTask: [row: ExecutionRow];
}>();

function timeText(value: string | null): string {
  return value ? formatDate(value) : "—";
}
</script>

<template>
  <el-table
    :data="rows"
    v-loading="loading"
    border
    stripe
    size="small"
    class="management-table execution-records"
    :empty-text="emptyText"
  >
    <el-table-column label="执行批次" :width="showTask ? 184 : 132">
      <template #default="{ row }">
        <div class="batch-line">第 {{ row.sequence_no }} 次</div>
        <el-button v-if="showTask" link type="primary" class="batch-task" @click="emit('openTask', row)">
          {{ row.task_name }}
        </el-button>
      </template>
    </el-table-column>
    <el-table-column label="执行状态" width="129">
      <template #default="{ row }">
        <StatusBadge
          :status="row.status"
          :label="executionStatusLabel(row.status)"
          :tone="executionStatusTone(row.status)"
        />
      </template>
    </el-table-column>
    <el-table-column label="开始时间" width="195">
      <template #default="{ row }"><span class="table-nowrap">{{ timeText(row.started_at) }}</span></template>
    </el-table-column>
    <el-table-column label="耗时" width="140">
      <template #default="{ row }"><span class="table-nowrap">{{ formatDuration(row.duration_ms) }}</span></template>
    </el-table-column>
    <el-table-column label="执行概况" min-width="250">
      <template #default="{ row }">
        <div class="overview-line" :title="executionProgressText(row)">{{ executionProgressText(row) }}</div>
        <div class="overview-muted" :title="MODEL_TURNS_HINT">{{ modelTurnsText(row.model_turns) }}</div>
        <div
          v-if="row.block_reason && !row.invalidated"
          class="overview-reason"
          :title="row.block_reason.text"
        >{{ row.block_reason.text }}</div>
      </template>
    </el-table-column>
  </el-table>
</template>

<style scoped>
.batch-line { line-height: 1.5; }
.batch-task {
  display: block;
  max-width: 100%;
  overflow: hidden;
  text-align: left;
}
.batch-task :deep(span) {
  display: block;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.overview-line,
.overview-muted,
.overview-reason {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  line-height: 1.5;
}
.overview-muted { color: var(--el-text-color-secondary); font-size: 12px; }
.overview-reason { color: var(--el-color-danger); font-size: 12px; }
</style>
