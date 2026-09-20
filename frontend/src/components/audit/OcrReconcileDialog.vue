<script setup lang="ts">
import { computed, ref, watch } from "vue";

import type { AuditDocument } from "@/api/contracts";
import type { DocumentOcrStatus, OcrReconcileRequest } from "@/api/audits";
import { formatDate } from "@/constants/labels";

/**
 * OCR 查询 / 对账弹窗。
 *
 * 先查询只读状态，再按真实情况选择：ADOPT_JOB 人工提供已确认的作业号继续查询；
 * RETRY_SUBMIT 明确重新提交外部作业（会记录新的提交尝试，必须填理由）。
 * 无证据时既不自动重发也不标成功。
 */
const props = defineProps<{
  open: boolean;
  document: AuditDocument | null;
  /** 只读查询结果；null 表示尚未查询或查询失败。 */
  status: DocumentOcrStatus | null;
  statusError: string | null;
  loading: boolean;
  /** 对账受理后的返回。 */
  result: { status: string; job_id: string | null; deadline_at: string | null; message: string } | null;
  pending: boolean;
}>();

const emit = defineEmits<{
  close: [];
  query: [];
  reconcile: [payload: OcrReconcileRequest];
}>();

const action = ref<"ADOPT_JOB" | "RETRY_SUBMIT">("ADOPT_JOB");
const jobId = ref("");
const reason = ref("");

watch(
  () => [props.open, props.document] as const,
  () => {
    if (!props.open) {
      return;
    }
    action.value = "ADOPT_JOB";
    jobId.value = "";
    reason.value = "";
  },
  { immediate: true },
);

const valid = computed(() => reason.value.trim().length > 0 && (action.value === "RETRY_SUBMIT" || jobId.value.trim().length > 0));

function submit(): void {
  if (!valid.value) {
    return;
  }
  emit("reconcile", {
    action: action.value,
    ...(action.value === "ADOPT_JOB" ? { job_id: jobId.value.trim() } : {}),
    reason: reason.value.trim(),
  });
}
</script>

<template>
  <el-dialog :model-value="open" title="OCR 查询与对账" width="600px" append-to-body @update:model-value="emit('close')">
    <el-descriptions :column="1" size="small" border>
      <el-descriptions-item label="材料">{{ document?.material_name || "未关联材料" }}</el-descriptions-item>
      <el-descriptions-item label="文件">{{ document?.filename || "未上传" }}</el-descriptions-item>
      <el-descriptions-item label="处理状态">
        <template v-if="loading">正在查询服务端真实状态…</template>
        <template v-else-if="statusError">
          <span class="text-danger">{{ statusError }}</span>
        </template>
        <template v-else-if="status">
          <div>{{ status.message }}</div>
          <div class="text-muted">
            作业号：<span class="mono">{{ status.job_id || "未知" }}</span>
            <template v-if="status.deadline_at"> · 截止 {{ formatDate(status.deadline_at) }}</template>
          </div>
        </template>
        <span v-else class="text-muted">还没有查询到状态。</span>
      </el-descriptions-item>
    </el-descriptions>

    <div class="card-actions block-gap">
      <el-button :loading="loading" @click="emit('query')">重新查询只读状态</el-button>
    </div>

    <el-alert
      v-if="result"
      class="block-gap"
      type="success"
      show-icon
      :closable="false"
      title="对账已受理"
    >
      <div>{{ result.message }}</div>
      <div class="text-muted">
        新的状态：{{ result.status }}
        <template v-if="result.job_id"> · 作业号 {{ result.job_id }}</template>
      </div>
    </el-alert>

    <template v-else>
      <el-form label-position="top" class="block-gap">
        <el-form-item label="对账动作" required>
          <el-radio-group v-model="action">
            <el-radio value="ADOPT_JOB">绑定已知作业号继续查询</el-radio>
            <el-radio value="RETRY_SUBMIT">放弃未知状态，重新提交外部作业</el-radio>
          </el-radio-group>
        </el-form-item>

        <el-form-item v-if="action === 'ADOPT_JOB'" label="供应商作业号" required>
          <el-input v-model="jobId" placeholder="填写已核对确认的供应商作业号" style="max-width: 360px" />
        </el-form-item>

        <el-form-item label="对账理由" required>
          <el-input
            v-model="reason"
            type="textarea"
            :rows="3"
            maxlength="2000"
            show-word-limit
            placeholder="说明本次对账的判断依据"
          />
        </el-form-item>
      </el-form>

      <el-alert v-if="action === 'RETRY_SUBMIT'" type="warning" show-icon :closable="false" title="这将重新提交外部作业">
        系统不会自动重发结果未知的作业；只有在你明确确认后才会重新提交，并记录为一次新的提交尝试。
      </el-alert>
    </template>

    <template #footer>
      <el-button @click="emit('close')">关闭</el-button>
      <el-button v-if="!result" type="primary" :disabled="!valid" :loading="pending" @click="submit">提交对账</el-button>
    </template>
  </el-dialog>
</template>
