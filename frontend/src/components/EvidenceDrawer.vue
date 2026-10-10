<script setup lang="ts">

import type { Evidence } from "@/api/contracts";
import DrawerPanel from "./DrawerPanel.vue";

/** 原文依据：展示来源材料、引用原文、内容核对及当前或历史版本。 */
const props = defineProps<{
  open: boolean;
  /** 材料中文名称。 */
  materialName: string;
  /** 字段或规则的业务名称。 */
  fieldName: string;
  evidence: Evidence | null;
  /** 证据读取中：显示加载态而不是「没有证据」。 */
  loading?: boolean;
  /** 读取失败时的真实原因；与「没有证据」分开显示。 */
  errorMessage?: string | null;
}>();

const emit = defineEmits<{
  close: [];
}>();

</script>

<template>
  <DrawerPanel
    :open="open"
    title="查看原文依据"
    :description="fieldName ? `${materialName || '材料'} · ${fieldName}` : materialName"
    @close="emit('close')"
  >
    <el-skeleton v-if="loading" :rows="5" animated />

    <el-alert
      v-else-if="errorMessage"
      type="error"
      show-icon
      :closable="false"
      :title="`证据读取失败：${errorMessage}`"
    >
      读取失败与「没有证据」是两件事：请关闭后重试，或到对应任务的字段结果里重新打开。
    </el-alert>

    <template v-else-if="evidence">
      <el-descriptions :column="1" size="small" border>
        <el-descriptions-item label="材料">{{ materialName || evidence.document_name || "未关联材料" }}</el-descriptions-item>
        <el-descriptions-item label="文件">{{ evidence.document_name || "未记录文件名" }}</el-descriptions-item>
        <el-descriptions-item v-if="evidence.field_path" label="字段属性">{{ evidence.field_path }}</el-descriptions-item>
        <el-descriptions-item label="原文内容核对">{{ evidence.quote_check_label || "未记录内容核对结论" }}</el-descriptions-item>
      </el-descriptions>
      <p v-if="evidence.historical || evidence.is_current_upload === false" class="text-hint">这段原文来自历史上传文件。</p>

      <el-card shadow="never" class="block-gap">
        <template #header>引用原文</template>
        <blockquote class="evidence-quote">{{ evidence.quote_text || "本条证据没有记录引用原文。" }}</blockquote>
      </el-card>

    </template>

    <el-empty v-else description="还没有读取到证据内容" />
  </DrawerPanel>
</template>

<style scoped>
.evidence-quote {
  margin: 0;
  padding: 0;
  color: var(--el-text-color-regular);
  font-size: 14px;
  line-height: 1.8;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}
</style>
