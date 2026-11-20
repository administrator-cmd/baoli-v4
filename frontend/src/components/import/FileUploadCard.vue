<script setup lang="ts">
import { computed, ref } from "vue";
import { ElMessage } from "element-plus";
import { FileSpreadsheet, Upload } from "lucide-vue-next";

import StatusBadge from "@/components/StatusBadge.vue";
import { formatDate, formatFileSize } from "@/constants/labels";
import type { UploadKindPolicy } from "@/api/contracts";

/**
 * 第一步：上传规则 Excel。
 *
 * 只显示业务需要的文件信息（文件名、大小、状态、失败原因），不显示服务器路径或内部编号。
 * 文件格式与大小先在前端给出明确提示，真正校验仍由服务端执行并返回真实错误。
 *
 * 允许扩展名与大小上限一律从后端 /system/upload-policy 读取，本组件不复制常量：
 * 复制的那一份会在配置变更后与后端实际校验不一致，让用户看到错误的提示。
 */
const props = defineProps<{
  /** 作业已经创建时显示的文件名。 */
  jobFilename: string | null;
  jobCreatedAt: string | null;
  jobStatus: string | null;
  /** 尚未创建作业时，明确显示浏览器中已经选中的文件。 */
  selectedFilename?: string | null;
  /** 本次浏览器选择的文件大小；刷新后浏览器不再保留该信息。 */
  selectedFileSize: number | null;
  /** 后端上传策略中的规则库一项；未取得前不展示大小与格式提示，也不做前端预检。 */
  policy: UploadKindPolicy | null;
  pending: boolean;
  /** 已形成转换结果时不能再换文件，只能重新上传开始新的导入。 */
  disabled: boolean;
}>();

const emit = defineEmits<{
  pick: [file: File];
  remove: [];
}>();

const uploadRef = ref<{ clearFiles: () => void } | null>(null);

/** 未取得策略时为空数组 / null：不做前端预检，交给服务端返回真实错误。 */
const allowedExtensions = computed(() => props.policy?.allowed_extensions ?? []);
const maxBytes = computed(() => props.policy?.max_bytes ?? null);
const acceptText = computed(() => allowedExtensions.value.join(","));
const maxSizeText = computed(() =>
  maxBytes.value === null ? null : formatFileSize(maxBytes.value),
);

const sizeText = computed(() => (props.selectedFileSize === null ? null : formatFileSize(props.selectedFileSize)));

function onFileChange(file: { raw?: File; name: string }): void {
  uploadRef.value?.clearFiles();
  if (props.pending || props.disabled) return;
  const raw = file.raw;
  if (!raw) {
    return;
  }
  const lower = raw.name.toLowerCase();
  // 未取得后端策略时不做预检：宁可让服务端返回真实错误，也不用一份可能过期的名单挡下文件。
  if (allowedExtensions.value.length && !allowedExtensions.value.some((extension) => lower.endsWith(extension))) {
    ElMessage.error(`只支持 ${allowedExtensions.value.join("、")} 格式的规则库文件。`);
    return;
  }
  if (maxBytes.value !== null && raw.size > maxBytes.value) {
    ElMessage.error(
      `文件大小为 ${formatFileSize(raw.size)}，超过 ${formatFileSize(maxBytes.value)} 上限，不会被解析。`,
    );
    return;
  }
  emit("pick", raw);
}
</script>

<template>
  <div>
    <!-- 已上传：显示真实文件名与状态，不显示本地路径 -->
    <template v-if="jobFilename">
      <el-descriptions :column="2" size="small" border>
        <el-descriptions-item label="文件名称">
          <span class="upload-summary__name">
            <el-icon><FileSpreadsheet /></el-icon>
            {{ jobFilename }}
          </span>
        </el-descriptions-item>
        <el-descriptions-item label="导入状态">
          <StatusBadge :status="jobStatus" />
        </el-descriptions-item>
        <el-descriptions-item label="文件大小">{{ sizeText ?? "刷新后不再显示" }}</el-descriptions-item>
        <el-descriptions-item label="上传时间">{{ jobCreatedAt ? formatDate(jobCreatedAt) : "未提供" }}</el-descriptions-item>
        <el-descriptions-item label="解析方式" :span="2">
          LLM 解析规则库
        </el-descriptions-item>
      </el-descriptions>

      <el-alert
        v-if="disabled"
        class="block-gap"
        type="info"
        show-icon
        :closable="false"
        title="当前导入内容已生成"
      >
        如需调整规则内容，请在下方重新上传规则库文件开始新的导入。
      </el-alert>
    </template>

    <div v-else-if="selectedFilename" class="selected-file">
      <div class="selected-file__info" role="status" aria-live="polite">
        <FileSpreadsheet class="selected-file__icon" :size="32" aria-hidden="true" />
        <div class="selected-file__content">
          <div class="selected-file__heading">
            <strong class="selected-file__name">{{ selectedFilename }}</strong>
            <slot name="selected-status"><el-tag type="success" size="small">已选择</el-tag></slot>
          </div>
          <div class="selected-file__meta">Excel 工作簿<template v-if="sizeText"> · {{ sizeText }}</template></div>
          <div class="selected-file__hint"><slot name="selected-hint">文件已选择，提交后上传。</slot></div>
        </div>
      </div>
      <div class="selected-file__actions">
        <el-upload
          ref="uploadRef"
          :accept="acceptText"
          :auto-upload="false"
          :show-file-list="false"
          :disabled="pending || disabled"
          :on-change="onFileChange"
        >
          <el-button :disabled="pending || disabled">更换文件</el-button>
        </el-upload>
        <el-button link type="danger" :disabled="pending || disabled" @click="emit('remove')">移除</el-button>
      </div>
    </div>

    <!-- 没有选择文件时才显示空的拖拽区域。 -->
    <el-upload
      v-else
      ref="uploadRef"
      class="upload-block"
      drag
      :accept="acceptText"
      :auto-upload="false"
      :show-file-list="false"
      :disabled="pending || disabled"
      :on-change="onFileChange"
    >
      <el-icon class="upload-block__icon"><Upload /></el-icon>
      <div class="el-upload__text">
        将文件拖到此处，或<em>点击选择文件</em>
      </div>
      <template #tip>
        <div class="el-upload__tip">
          <template v-if="policy">
            支持 {{ policy.allowed_extensions.join("、") }} 格式，单个文件不超过 {{ maxSizeText }}。
          </template>
          <template v-else>文件格式与大小上限以上传策略为准，当前未取得该策略。</template>
        </div>
      </template>
    </el-upload>
  </div>
</template>

<style scoped>
.upload-summary__name {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 15px;
  overflow-wrap: anywhere;
}
.selected-file {
  display: flex;
  align-items: center;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: 16px;
  padding: 20px;
  border: 1px solid var(--el-color-primary-light-5);
  border-radius: var(--el-border-radius-base);
  background: var(--el-color-primary-light-9);
}
.selected-file__info { display: flex; align-items: flex-start; gap: 14px; min-width: 0; flex: 1 1 260px; }
.selected-file__icon { flex-shrink: 0; color: var(--el-color-success-dark-2); }
.selected-file__content { min-width: 0; flex: 1; }
.selected-file__heading { display: flex; align-items: center; flex-wrap: wrap; gap: 8px 12px; }
.selected-file__name { color: var(--el-text-color-primary); font-size: 16px; line-height: 1.5; overflow-wrap: anywhere; }
.selected-file__meta, .selected-file__hint { margin-top: 6px; color: var(--el-text-color-regular); font-size: 13px; line-height: 1.5; }
.selected-file__actions { display: flex; align-items: center; flex-wrap: wrap; gap: 12px; }
.selected-file__actions :deep(.el-button + .el-button) { margin-left: 0; }
</style>
