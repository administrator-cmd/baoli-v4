<script setup lang="ts">
import StatusBadge from "@/components/StatusBadge.vue";
import type { MaterialProcessingState } from "@/composables/useMaterialProcessing";
import { formatDate, statusTone } from "@/constants/labels";

/** 弹窗展示及表单；查询、对账和过期响应处理由控制器维护。 */
defineProps<{ state: MaterialProcessingState }>();
defineEmits<{
  "update:visible": [value: boolean];
  "update:job-id": [value: string];
  "update:reason": [value: string];
  "cancel-action": [];
}>();
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
          <StatusBadge :status="row.status" :label="state.processingStatusLabel(row)" :tone="state.processingRecoverable(row) ? 'warning' : statusTone(row.status)" />
        </template>
      </el-table-column>
      <el-table-column label="详情" min-width="220">
        <template #default="{ row }">
          <div>{{ row.message }}</div>
          <div v-if="typeof row.extract_progress?.extractedPages === 'number'" class="text-hint">
            已识别 {{ row.extract_progress.extractedPages }}<template v-if="typeof row.extract_progress.totalPages === 'number'"> / {{ row.extract_progress.totalPages }}</template> 页
          </div>
          <div v-if="row.progress_updated_at" class="text-hint">进度更新：{{ formatDate(row.progress_updated_at) }}</div>
          <div v-if="row.artifact_no" class="text-hint">产物编号：{{ row.artifact_no }}</div>
          <div v-if="row.job_id" class="text-hint">作业号：{{ row.job_id }}</div>
          <div v-if="row.deadline_at" class="text-hint">等待截止：{{ formatDate(row.deadline_at) }}</div>
        </template>
      </el-table-column>
      <el-table-column label="操作" width="170">
        <template #default="{ row }">
          <template v-if="state.processingRecoverable(row) && state.processingActionAllowed">
            <el-button v-if="row.artifact_type === 'OCR' && (row.status === 'PENDING' || row.status === 'FAILED')" link type="primary" size="small" @click="state.retrySingleOcr(row)">单份重试</el-button>
            <el-button v-if="row.artifact_type === 'OCR' && row.status !== 'FAILED'" link type="primary" size="small" @click="state.chooseProcessingAction(row, 'ADOPT_JOB')">接管作业</el-button>
            <el-button link type="warning" size="small" @click="state.chooseProcessingAction(row, row.artifact_type === 'OCR' ? 'RETRY_SUBMIT' : 'CLEAR')">
              {{ row.artifact_type === "OCR" ? "重新提交" : "解除占位" }}
            </el-button>
          </template>
          <span v-else>—</span>
        </template>
      </el-table-column>
    </el-table>

    <div v-if="state.processingAction" class="block-gap">
      <el-alert
        type="warning"
        show-icon
        :closable="false"
        :title="state.processingAction.action === 'ADOPT_JOB' ? '接管已确认的 OCR 作业' : state.processingAction.action === 'RETRY_SUBMIT' ? '重新提交 OCR 作业' : '解除结果未知的处理占位'"
      >
        请先核对外部服务的实际处理情况；重新提交或解除占位后再次执行节点，可能产生新的外部请求。
      </el-alert>
      <el-form label-position="top" class="block-gap-sm">
        <el-form-item v-if="state.processingAction.action === 'ADOPT_JOB'" label="已确认的作业号" required>
          <el-input :model-value="state.processingJobId" maxlength="200" @update:model-value="$emit('update:job-id', $event)" />
        </el-form-item>
        <el-form-item label="对账原因" required>
          <el-input :model-value="state.processingReason" type="textarea" :rows="3" maxlength="2000" show-word-limit @update:model-value="$emit('update:reason', $event)" />
        </el-form-item>
      </el-form>
      <el-alert v-if="state.processingActionError" class="block-gap-sm" type="error" show-icon :closable="false" :title="state.processingActionError" />
      <el-button type="primary" :loading="state.processingSubmitting" :disabled="!state.processingActionAllowed" @click="state.submitProcessingAction">确认对账</el-button>
      <el-button :disabled="state.processingSubmitting" @click="$emit('cancel-action')">取消</el-button>
    </div>
    <template #footer>
      <el-button :loading="state.processingLoading" :disabled="state.processingSubmitting" @click="state.loadProcessingStatuses">刷新状态</el-button>
      <el-button :disabled="state.processingSubmitting" @click="$emit('update:visible', false)">关闭</el-button>
    </template>
  </el-dialog>
</template>
