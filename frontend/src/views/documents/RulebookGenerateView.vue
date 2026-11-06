<script setup lang="ts">
import { onMounted, onUnmounted, ref, watch } from "vue";
import { useRouter } from "vue-router";
import { ElMessage } from "element-plus";

import { createGenerationJob, startGeneration } from "@/api/rulebookGenerations";
import { getUploadPolicy } from "@/api/system";
import type { GenerationJob, UploadKindPolicy } from "@/api/contracts";
import { ApiError, operationIdempotencyKey } from "@/api/http";
import { useRequestRunner } from "@/composables/useRequestRunner";
import BackButton from "@/components/BackButton.vue";
import FailureNotice from "@/components/FailureNotice.vue";
import FileUploadCard from "@/components/import/FileUploadCard.vue";
import PageHeader from "@/components/PageHeader.vue";

/**
 * 用户点击“上传并开始编制”后，依次保存原件并提交真实后台编制。
 * 启动失败时保留已创建记录和幂等键，不重复上传或伪称已开始。
 */
const router = useRouter();
const { pending, failure, formMessage, run, clear } = useRequestRunner();

const selectedFile = ref<File | null>(null);
const selectedFileSize = ref<number | null>(null);
const uploadKey = ref("");
const uploadPercent = ref<number | null>(null);
const businessBackground = ref("");
const generationRequirements = ref("");
const policy = ref<UploadKindPolicy | null>(null);
const createdJob = ref<GenerationJob | null>(null);
const generationKey = ref("");
const stage = ref("");
let disposed = false;

async function loadPolicy(): Promise<void> {
  try {
    const payload = await getUploadPolicy();
    policy.value = payload.rulebook;
  } catch (error) {
    // 上传策略只用于提示；服务端仍会做真实校验。读取失败不伪装成已配置。
    formMessage.value = error instanceof Error ? `上传策略读取失败：${error.message}` : "上传策略读取失败";
  }
}

function onPick(file: File): void {
  if (pending.value) return;
  selectedFile.value = file;
  selectedFileSize.value = file.size;
  // 同一次上传提交的重发复用这个键；重新选择文件或修改生成输入就是新的显式上传操作。
  regenerateUploadKey();
  uploadPercent.value = null;
  clear();
}

function removeSelectedFile(): void {
  if (pending.value) return;
  selectedFile.value = null;
  selectedFileSize.value = null;
  uploadKey.value = "";
  uploadPercent.value = null;
  createdJob.value = null;
  generationKey.value = "";
  stage.value = "";
  clear();
}

async function submit(): Promise<void> {
  if (pending.value) return;
  const file = selectedFile.value;
  if (!file) {
    ElMessage.warning("请先选择规则库 Excel 文件");
    return;
  }
  if (!uploadKey.value) {
    uploadKey.value = operationIdempotencyKey("generation-upload", file.name);
  }
  await run(async () => {
    if (!createdJob.value) {
      stage.value = "正在保存原始业务资料";
      const created = await createGenerationJob(
        file,
        {
          businessBackground: businessBackground.value,
          generationRequirements: generationRequirements.value,
          idempotencyKey: uploadKey.value,
        },
        (percent) => { if (!disposed) uploadPercent.value = percent; },
      );
      if (disposed) return;
      createdJob.value = created.job;
    }
    const job = createdJob.value;
    if (job.can_generate) {
      stage.value = "原始资料已保存，正在开始 AI 编制";
      if (!generationKey.value) generationKey.value = operationIdempotencyKey("generation", job.job_no);
      try {
        await startGeneration(job.job_no, generationKey.value);
      } catch (error) {
        if (error instanceof ApiError && error.statusCode !== null && error.statusCode >= 400 && error.statusCode < 500) {
          generationKey.value = "";
        }
        stage.value = "原始资料已保存，编制是否开始请查看记录；重试将继续使用此记录";
        throw error;
      }
      if (disposed) return;
      ElMessage.success("AI 编制已受理，可在记录中查看进度");
    }
    await router.push({ name: "rulebook-generation-detail", params: { jobNo: job.job_no } });
  });
}

function regenerateUploadKey(): void {
  if (!selectedFile.value) return;
  createdJob.value = null;
  generationKey.value = "";
  stage.value = "";
  uploadKey.value = operationIdempotencyKey(
    "generation-upload", selectedFile.value.name, String(selectedFile.value.size),
    String(selectedFile.value.lastModified),
  );
}

// 修改业务背景或补充要求就是不同的上传请求，不能复用旧标识。
watch([businessBackground, generationRequirements], regenerateUploadKey);

onMounted(() => { void loadPolicy(); });
onUnmounted(() => { disposed = true; });
</script>

<template>
  <div class="app-panel">
    <PageHeader title="AI 辅助编制规则库" :show-refresh="false">
      <template #leading>
        <BackButton label="返回规则库" @click="router.push({ name: 'rulebooks' })" />
      </template>
    </PageHeader>

    <FailureNotice :error="failure" @dismiss="clear" />
    <el-alert v-if="formMessage" class="block-gap-sm" type="warning" show-icon :closable="false" :title="formMessage" />

    <el-steps :active="0" simple class="block-gap-sm"><el-step title="上传业务资料" /><el-step title="AI 整理编制" /><el-step title="核对与导出" /></el-steps>

    <FileUploadCard
      :job-filename="null"
      :job-created-at="null"
      :job-status="null"
      :selected-filename="selectedFile?.name"
      :selected-file-size="selectedFileSize"
      :policy="policy"
      :pending="pending"
      :disabled="pending"
      @pick="onPick"
      @remove="removeSelectedFile"
    >
      <template #selected-status>
        <el-tag :type="createdJob ? 'success' : failure ? 'warning' : 'primary'" size="small">
          {{ createdJob ? '已上传' : pending ? '上传中' : failure ? '上传未完成' : '已选择，待上传' }}
        </el-tag>
      </template>
      <template #selected-hint>
        <div v-if="pending && uploadPercent !== null && !createdJob" class="generation-file-progress">
          <el-progress :percentage="uploadPercent" :stroke-width="6" />
        </div>
        <span v-if="stage && (pending || createdJob)">{{ stage }}</span>
      </template>
    </FileUploadCard>

    <el-form class="block-gap" label-position="top" :disabled="pending">
      <el-form-item label="这份规则库用于什么业务？（可选）">
        <el-input
          v-model="businessBackground"
          type="textarea"
          :maxlength="16000"
          show-word-limit
          :rows="3"
          placeholder="说明使用对象、业务场景和适用范围，帮助 AI 正确理解条款。"
        />
      </el-form-item>
      <el-form-item label="希望怎样整理？（可选）">
        <el-input
          v-model="generationRequirements"
          type="textarea"
          :maxlength="16000"
          show-word-limit
          :rows="3"
          placeholder="例如：合并重复条款、保留原编号、把每条规则的适用范围和判断标准写清楚。"
        />
      </el-form-item>
    </el-form>

    <div class="generation-actions">
      <el-button type="primary" :loading="pending" :disabled="!selectedFile" @click="submit">
        {{ createdJob ? '继续开始 AI 编制' : '上传并开始 AI 编制' }}
      </el-button>
      <el-button v-if="createdJob && !pending" @click="router.push({ name: 'rulebook-generation-detail', params: { jobNo: createdJob.job_no } })">查看编制记录</el-button>
    </div>
  </div>
</template>

<style scoped>
.generation-actions {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 12px;
}
.generation-file-progress { max-width: 420px; margin-bottom: 4px; }
</style>
