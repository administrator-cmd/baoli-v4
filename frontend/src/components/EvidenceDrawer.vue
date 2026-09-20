<script setup lang="ts">
import { computed } from "vue";

import type { Evidence } from "@/api/contracts";
import DrawerPanel from "./DrawerPanel.vue";
import StatusBadge from "./StatusBadge.vue";
import { locatorStatusLabel, locatorStatusTone } from "@/constants/labels";

/**
 * 证据抽屉：标题优先显示材料中文名称。
 * 位置信息只在服务端真实返回坐标时显示，没有坐标时明确说明，不伪造高亮位置。
 * 定位核对状态与所属上传版本必须显式呈现：未核实/无法定位不能显示成已核实。
 */
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

const evidenceMeta = computed(() => {
  const parts: string[] = [];
  if (props.evidence?.document_name) {
    parts.push(props.evidence.document_name);
  }
  if (props.evidence?.page_no) {
    parts.push(`第 ${props.evidence.page_no} 页`);
  }
  return parts.join(" · ") || "未提供文件名与页码";
});

/** 定位信息只在服务端真实返回坐标时显示。 */
const locationText = computed(() => {
  const bbox = props.evidence?.bbox;
  if (!bbox || !bbox.length) {
    return null;
  }
  return bbox.map((value) => Math.round(value)).join(", ");
});

/** 定位核对说明：无法核对与没有可定位坐标是两件事。 */
const locatorNote = computed(() => {
  const status = props.evidence?.locator_status;
  if (props.evidence?.locator_note) {
    return props.evidence.locator_note;
  }
  if (status === "UNLOCATABLE") {
    return "声明的位置无法核对：引文/页码/坐标未能在所属产物里定位。";
  }
  if (status === "UNVERIFIED") {
    return "该记录的位置尚未核对。";
  }
  if (status === "VERIFIED") {
    return "已记录定位核对通过，具体位置以产物提供的信息为准。";
  }
  return "服务端没有返回定位核对状态。";
});

const uploadVersionText = computed(() => {
  const evidence = props.evidence;
  if (!evidence) {
    return "未提供";
  }
  const parts: string[] = [];
  if (evidence.document_upload_no) {
    parts.push(evidence.document_upload_no);
  }
  if (evidence.artifact_type) {
    parts.push(evidence.artifact_type);
  }
  if (evidence.historical || evidence.is_current_upload === false) {
    parts.push("历史上传版本");
  } else if (evidence.is_current_upload) {
    parts.push("当前上传版本");
  }
  return parts.join(" · ") || "未提供";
});
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
        <el-descriptions-item label="原文内容核对">{{ evidence.quote_check_label || "未记录内容核对结论" }}</el-descriptions-item>
      </el-descriptions>
      <p v-if="evidence.historical || evidence.is_current_upload === false" class="text-hint">这段原文来自历史上传文件。</p>

      <el-card shadow="never" class="block-gap">
        <template #header>引用原文</template>
        <blockquote class="evidence-quote">{{ evidence.quote_text || "本条证据没有记录引用原文。" }}</blockquote>
      </el-card>

      <el-collapse class="block-gap">
        <el-collapse-item title="来源与位置详情（辅助信息）" name="tech">
          <div>所属上传版本：{{ uploadVersionText }}</div>
          <div>文件与页码：{{ evidenceMeta }}</div>
          <StatusBadge
            :status="evidence.locator_status"
            :label="locatorStatusLabel(evidence.locator_status)"
            :tone="locatorStatusTone(evidence.locator_status)"
          />
          <div class="text-muted block-gap-sm">{{ locatorNote }}</div>
          <div v-if="locationText">页面坐标：{{ locationText }}</div>
          <div class="mono">证据编号：{{ evidence.evidence_no }}</div>
        </el-collapse-item>
      </el-collapse>
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
