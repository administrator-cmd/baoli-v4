<script setup lang="ts">
/**
 * 统计卡片组。数字全部来自服务端真实数据；
 * 页面不使用它展示推测出来的进度或百分比。
 */
export interface StatItem {
  label: string;
  value: number;
  hint?: string;
  /** 语义色：只在数字本身需要提示注意力时使用。 */
  tone?: "neutral" | "primary" | "success" | "warning" | "danger";
}

withDefaults(defineProps<{ items: StatItem[] }>(), {});

const TONE_CLASS: Record<string, string> = {
  neutral: "",
  primary: "stat-card--primary",
  success: "stat-card--success",
  warning: "stat-card--warning",
  danger: "stat-card--danger",
};
</script>

<template>
  <el-row :gutter="12">
    <el-col v-for="item in items" :key="item.label" :xs="12" :sm="12" :md="6" :lg="6">
      <el-card shadow="never" class="stat-card" :class="TONE_CLASS[item.tone ?? 'neutral']">
        <el-statistic :title="item.label" :value="item.value" />
        <div v-if="item.hint" class="text-hint">{{ item.hint }}</div>
      </el-card>
    </el-col>
  </el-row>
</template>

<style scoped>
.stat-card {
  margin-bottom: 12px;
}

.stat-card :deep(.el-statistic__content) {
  font-size: 24px;
  font-weight: 600;
}

.stat-card--warning :deep(.el-statistic__content) {
  color: var(--el-color-warning);
}

.stat-card--danger :deep(.el-statistic__content) {
  color: var(--el-color-danger);
}

.stat-card--success :deep(.el-statistic__content) {
  color: var(--el-color-success);
}

.stat-card--primary :deep(.el-statistic__content) {
  color: var(--el-color-primary);
}
</style>
