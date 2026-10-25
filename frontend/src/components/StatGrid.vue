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
  primary: "stat-item--primary",
  success: "stat-item--success",
  warning: "stat-item--warning",
  danger: "stat-item--danger",
};
</script>

<template>
  <div class="stat-strip">
    <div v-for="item in items" :key="item.label" class="stat-item" :class="TONE_CLASS[item.tone ?? 'neutral']">
      <span class="stat-value">{{ item.value }}</span>
      <div class="stat-text">
        <span class="stat-label">{{ item.label }}</span>
        <span v-if="item.hint" class="stat-hint">{{ item.hint }}</span>
      </div>
    </div>
  </div>
</template>

<style scoped>
.stat-strip {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 4px;
  background: var(--el-bg-color);
}

.stat-item {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 10px 16px;
  border-right: 1px solid var(--el-border-color-lighter);
}

.stat-item:last-child {
  border-right: 0;
}

.stat-value {
  font-size: 24px;
  font-weight: 600;
  line-height: 1;
  font-variant-numeric: tabular-nums;
}

.stat-text {
  display: flex;
  flex-direction: column;
  gap: 3px;
  min-width: 0;
}

.stat-label {
  font-size: 13px;
  color: var(--el-text-color-regular);
  line-height: 1.2;
}

.stat-hint {
  font-size: 12px;
  color: var(--el-text-color-secondary);
  line-height: 1.2;
}

.stat-item--warning .stat-value {
  color: var(--el-color-warning);
}

.stat-item--danger .stat-value {
  color: var(--el-color-danger);
}

.stat-item--success .stat-value {
  color: var(--el-color-success);
}

.stat-item--primary .stat-value {
  color: var(--el-color-primary);
}

@media (max-width: 900px) {
  .stat-strip {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }

  .stat-item:nth-child(2n) {
    border-right: 0;
  }

  .stat-item:nth-child(n + 3) {
    border-top: 1px solid var(--el-border-color-lighter);
  }
}

@media (max-width: 560px) {
  .stat-strip {
    grid-template-columns: minmax(0, 1fr);
  }

  .stat-item {
    border-right: 0;
  }

  .stat-item + .stat-item {
    border-top: 1px solid var(--el-border-color-lighter);
  }
}
</style>
