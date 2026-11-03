<script setup lang="ts">
import { computed } from "vue";

import type { ApiError } from "@/api/http";

/**
 * 统一错误提示：服务端原因、字段错误与诊断信息。
 * 具体操作由调用页面按实际状态提供，不根据错误码拼接通用建议。
 * 请求编号等排查信息放在折叠区，不占主要位置。
 *
 * 一个页面只挂一处；AppShell 不重复显示页面已经显示过的错误。
 */
const props = defineProps<{
  error: ApiError | null;
  /** 关闭按钮由页面决定是否显示。 */
  closable?: boolean;
}>();

defineEmits<{
  dismiss: [];
}>();

/** 后端可能把补充说明放在 details 里；这里只显示可读的标量项。 */
const detailEntries = computed(() => {
  const details = props.error?.details ?? {};
  return Object.entries(details)
    .filter(([, value]) => typeof value === "string" || typeof value === "number")
    .map(([key, value]) => ({ key, value: String(value) }));
});

const fieldErrors = computed(() => {
  const fields = props.error?.details.fields;
  if (!Array.isArray(fields)) return [];
  return fields.flatMap((entry) => {
    if (typeof entry !== "object" || entry === null || Array.isArray(entry)) return [];
    if (typeof entry.field !== "string" || typeof entry.message !== "string") return [];
    return [{ field: entry.field, message: entry.message }];
  });
});
</script>

<template>
  <el-alert
    v-if="error"
    class="failure-notice"
    type="error"
    show-icon
    :closable="closable !== false"
    @close="$emit('dismiss')"
  >
    <template #title>
      <span class="failure-notice__title">{{ error.message }}</span>
    </template>
    <div class="failure-notice__body">
      <p>错误码：{{ error.code }}</p>
      <div v-for="(entry, index) in fieldErrors" :key="index">
        {{ entry.field }}：{{ entry.message }}
      </div>
      <el-collapse v-if="detailEntries.length || error.requestId" class="failure-notice__more">
        <el-collapse-item title="查看技术信息" name="tech">
          <div v-if="error.statusCode !== null" class="mono">HTTP 状态：{{ error.statusCode }}</div>
          <div v-for="entry in detailEntries" :key="entry.key" class="mono">
            {{ entry.key }}：{{ entry.value }}
          </div>
          <div v-if="error.requestId" class="mono">请求编号：{{ error.requestId }}</div>
        </el-collapse-item>
      </el-collapse>
    </div>
  </el-alert>
</template>

<style scoped>
.failure-notice {
  margin-bottom: 16px;
  align-items: flex-start;
}

.failure-notice__title {
  font-size: 14px;
  font-weight: 600;
}

.failure-notice__body {
  margin-top: 4px;
  font-size: 13px;
  line-height: 1.7;
}

.failure-notice__more {
  margin-top: 4px;
  border-top: 0;
}

.failure-notice__more :deep(.el-collapse-item__header),
.failure-notice__more :deep(.el-collapse-item__wrap) {
  background: transparent;
  border-bottom: 0;
  height: 28px;
  line-height: 28px;
  font-size: 12px;
}
</style>
