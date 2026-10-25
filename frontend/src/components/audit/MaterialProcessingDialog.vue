<script setup lang="ts">
import { onUnmounted, watch } from "vue";

import StatusBadge from "@/components/StatusBadge.vue";
import type { MaterialProcessingState } from "@/composables/useMaterialProcessing";
import { formatDate } from "@/constants/labels";

/** 材料处理状态与单项重试；异步响应由控制器维护。 */
const props = defineProps<{ state: MaterialProcessingState }>();
defineEmits<{
  "update:visible": [value: boolean];
}>();

/**
 * 弹窗打开期间每 5 秒刷新一次四项产物状态。
 * 识别页数由服务端轮询写入，只有重新查询才能看到进展；间隔放宽是为了不放大请求量。
 */
const REFRESH_INTERVAL_MS = 5_000;
let timer: ReturnType<typeof setInterval> | null = null;

function stopTimer(): void {
  if (timer !== null) {
    clearInterval(timer);
    timer = null;
  }
}

watch(
  () => props.state.processingDialogVisible,
  (visible) => {
    stopTimer();
    if (!visible) return;
    timer = setInterval(() => {
      if (!props.state.processingLoading && !props.state.processingSubmitting) {
        void props.state.loadProcessingStatuses();
      }
    }, REFRESH_INTERVAL_MS);
  },
  { immediate: true },
);

onUnmounted(stopTimer);
</script>

<template>
  <el-dialog
    :model-value="state.processingDialogVisible"
    @update:model-value="$emit('update:visible', $event)"
    :title="`${state.processingDocument?.material_name ?? '材料'} · 处理状态`"
    width="760px"
    :close-on-click-modal="false"
    :close-on-press-escape="!state.processingSubmitting"
    :show-close="!state.processingSubmitting"
  >
    <el-alert
      v-for="error in state.processingErrors"
      :key="error.artifact_type"
      class="block-gap-sm"
      type="error"
      show-icon
      :closable="false"
      :title="`${state.processingArtifactLabel(error.artifact_type)}：${error.message}`"
    >
      <el-button size="small" :loading="state.processingLoading" @click="state.loadProcessingStatuses">重新查询</el-button>
    </el-alert>
    <el-table
      :data="state.processingStatuses"
      border
      size="small"
      v-loading="state.processingLoading"
      :empty-text="state.processingErrors.length ? '未能读取处理状态' : '暂无处理状态'"
    >
      <el-table-column label="处理服务" width="150">
        <template #default="{ row }">{{ state.processingArtifactLabel(row.artifact_type) }}</template>
      </el-table-column>
      <el-table-column label="状态" width="140">
        <template #default="{ row }">
          <StatusBadge :status="row.status" :label="state.processingStatusLabel(row)" :tone="state.processingStatusTone(row)" />
        </template>
      </el-table-column>
      <el-table-column label="详情" min-width="220">
        <template #default="{ row }">
          <div>{{ row.message }}</div>
          <div v-if="typeof row.extract_progress?.extractedPages === 'number'" class="text-hint">
            已识别 {{ row.extract_progress.extractedPages }}<template v-if="typeof row.extract_progress.totalPages === 'number'"> / {{ row.extract_progress.totalPages }}</template> 页
          </div>
          <div v-if="row.progress_updated_at" class="text-hint">进度更新：{{ formatDate(row.progress_updated_at) }}</div>
          <div v-if="row.deadline_at" class="text-hint">等待截止：{{ formatDate(row.deadline_at) }}</div>
        </template>
      </el-table-column>
      <el-table-column label="操作" width="170">
        <template #default="{ row }">
          <el-button v-if="state.processingRetryable(row)" :disabled="!state.processingRetryAllowed" :loading="state.processingSubmitting" link type="primary" size="small" @click="state.retrySingleArtifact(row)">{{ state.processingRetryLabel(row) }}</el-button>
          <span v-else>—</span>
        </template>
      </el-table-column>
    </el-table>

    <el-alert v-if="state.processingError" class="block-gap-sm" type="error" show-icon :closable="false" :title="state.processingError" />
    <template #footer>
      <el-button :loading="state.processingLoading" :disabled="state.processingSubmitting" @click="state.loadProcessingStatuses">刷新状态</el-button>
      <el-button :disabled="state.processingSubmitting" @click="$emit('update:visible', false)">关闭</el-button>
    </template>
  </el-dialog>
</template>
