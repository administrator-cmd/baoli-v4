<script setup lang="ts">
import { computed, onUnmounted, watch } from "vue";

import StatusBadge from "@/components/StatusBadge.vue";
import type { MaterialProcessingState } from "@/composables/useMaterialProcessing";
import { formatDate } from "@/constants/labels";
import { materialQualityLabel, materialQualityTone } from "@/constants/materialQuality";

/** 材料处理状态与单项重试；异步响应由控制器维护。 */
const props = defineProps<{ state: MaterialProcessingState }>();
const quality = computed(() => props.state.processingDocument?.quality_precheck);
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
    :title="`${state.processingDocument?.material_name ?? '材料'} · 质量与处理详情`"
    width="min(920px, calc(100vw - 32px))"
    :close-on-click-modal="false"
    :close-on-press-escape="!state.processingSubmitting"
    :show-close="!state.processingSubmitting"
  >
    <p class="processing-filename">{{ state.processingDocument?.filename || "未上传文件" }}</p>
    <div class="quality-heading">
      <strong>前置质量检查</strong>
      <StatusBadge :status="quality?.status" :label="materialQualityLabel(quality?.status)" :tone="materialQualityTone(quality?.status)" />
      <span v-if="quality && quality.total_checks > 0" class="text-hint">已取得 {{ quality.completed_checks }} / {{ quality.total_checks }} 项结论</span>
    </div>
    <el-alert
      class="block-gap-sm"
      :type="quality?.blocked ? (quality.status === 'ABNORMAL' ? 'error' : 'warning') : 'info'"
      show-icon :closable="false"
      :title="quality?.message || '当前文件尚无前置检查记录。'"
    >
      <template v-if="quality?.blocked">该文件的后续处理与相关检查已停止。请按明细处理问题，或替换文件后重新审核；其他可继续的材料仍按流程执行。</template>
    </el-alert>
    <div v-if="quality?.checks.length" class="processing-table-scroll block-gap-sm">
      <el-table :data="quality.checks" border size="small" class="processing-table">
        <el-table-column label="检查要求" min-width="240">
          <template #default="{ row }">
            <strong>{{ row.rule_name }}</strong>
            <div v-if="row.requirement" class="quality-requirement">{{ row.requirement }}</div>
          </template>
        </el-table-column>
        <el-table-column label="检查结果" width="130">
          <template #default="{ row }">
            <StatusBadge :status="row.status" :label="materialQualityLabel(row.status)" :tone="materialQualityTone(row.status)" />
          </template>
        </el-table-column>
        <el-table-column prop="message" label="判断依据 / 待解决问题" min-width="280" />
      </el-table>
    </div>
    <p class="text-hint">这里仅展示处理前的质量检查；需要识别正文、印章或字段的检查，在后续规则中执行。</p>
    <strong class="processing-heading">材料处理</strong>
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
    <div class="processing-table-scroll">
      <el-table
        :data="state.processingStatuses"
        border
        size="small"
        v-loading="state.processingLoading"
        :empty-text="state.processingErrors.length ? '未能读取处理状态' : '暂无处理状态'"
        class="processing-table"
      >
        <el-table-column label="处理步骤" width="130">
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
            <el-button v-if="quality?.processing_allowed && state.processingRetryable(row)" :disabled="!state.processingRetryAllowed" :loading="state.processingSubmitting" link type="primary" size="small" @click="state.retrySingleArtifact(row)">{{ state.processingRetryLabel(row) }}</el-button>
            <span v-else>—</span>
          </template>
        </el-table-column>
      </el-table>
    </div>

    <el-alert v-if="state.processingError" class="block-gap-sm" type="error" show-icon :closable="false" :title="state.processingError" />
    <template #footer>
      <el-button :loading="state.processingLoading" :disabled="state.processingSubmitting" @click="state.loadProcessingStatuses">刷新状态</el-button>
      <el-button :disabled="state.processingSubmitting" @click="$emit('update:visible', false)">关闭</el-button>
    </template>
  </el-dialog>
</template>

<style scoped>
.processing-filename { margin: 0 0 16px; overflow-wrap: anywhere; }
.quality-heading { display: flex; align-items: center; flex-wrap: wrap; gap: 8px 12px; }
.processing-heading { display: block; margin: 20px 0 10px; }
.processing-table-scroll { width: 100%; overflow-x: auto; }
.processing-table { min-width: 680px; }
.processing-table :deep(.cell) { overflow-wrap: anywhere; }
.quality-requirement { margin-top: 4px; white-space: pre-wrap; color: var(--el-text-color-regular); }
</style>
