<script setup lang="ts">
import type { ImportStep } from "@/constants/importSteps";

/**
 * 导入流程步骤条。
 *
 * 每一步的状态来自服务端真实数据（是否已上传、是否已转换、是否还有未处理问题、
 * 发布检查是否通过）；被阻断的步骤在说明里写出中文原因，并禁止点击进入。
 */
const props = defineProps<{ steps: ImportStep[]; active: number; clickable?: boolean }>();

const emit = defineEmits<{
  select: [index: number];
}>();

/** 每个步骤自己的状态：被阻断/失败是红色，处理中是蓝色，完成是绿色。 */
function stepStatus(step: ImportStep): "wait" | "process" | "finish" | "success" | "error" {
  if (step.state === "blocked") {
    return "error";
  }
  if (step.state === "failed") {
    return "error";
  }
  if (step.state === "running") {
    return "process";
  }
  if (step.state === "done") {
    return "success";
  }
  if (step.index === props.active) {
    return "process";
  }
  return "wait";
}

function onSelect(step: ImportStep): void {
  if (!props.clickable || !step.enabled) {
    return;
  }
  emit("select", step.index);
}
</script>

<template>
  <el-steps :active="active" finish-status="success" align-center class="import-steps">
    <el-step
      v-for="step in steps"
      :key="step.label"
      :title="step.label"
      :description="step.blockedReason ?? step.description"
      :status="stepStatus(step)"
      :class="{ 'is-blocked': !step.enabled }"
      @click="onSelect(step)"
    />
  </el-steps>
</template>

<style scoped>
.import-steps {
  padding: 8px 0 16px;
}

.import-steps :deep(.el-step__head) {
  cursor: default;
}

.import-steps :deep(.el-step.is-blocked .el-step__head) {
  cursor: not-allowed;
}

.import-steps :deep(.el-step__description) {
  padding: 0 8px;
  font-size: 12px;
  line-height: 1.5;
}

/* 说明文字可能较长：限制行数，避免步骤条被撑高 */
.import-steps :deep(.el-step__description.is-wait),
.import-steps :deep(.el-step__description.is-finish),
.import-steps :deep(.el-step__description.is-success) {
  color: var(--el-text-color-secondary);
}
</style>
