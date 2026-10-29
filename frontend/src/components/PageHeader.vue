<script setup lang="ts">
import { RefreshCw } from "lucide-vue-next";

defineProps<{
  /** 当前页面名称，作为主标题。 */
  title: string;
  /** 次级说明：版本、发布时间等。 */
  notes?: string[];
  /** 刷新按钮的忙碌状态。 */
  busy?: boolean;
  /** 是否显示刷新按钮。 */
  showRefresh?: boolean;
  refreshText?: string;
}>();

defineEmits<{
  refresh: [];
}>();
</script>

<template>
  <div class="page-header-wrap">
    <div class="page-header">
      <div class="page-header__main">
        <slot name="leading" />
        <h1 class="page-header__title">{{ title }}</h1>
      </div>
      <div class="card-actions">
        <slot name="actions" />
        <el-button
          v-if="showRefresh !== false"
          :loading="busy"
          @click="$emit('refresh')"
        >
          <template #icon><RefreshCw :size="15" /></template>
          {{ refreshText || "刷新" }}
        </el-button>
      </div>
    </div>

    <div v-if="$slots.status" class="page-header__sub">
      <slot name="status" />
    </div>

    <div v-if="notes && notes.length" class="page-header__meta">
      <span v-for="note in notes" :key="note" class="page-header__meta-item">{{ note }}</span>
    </div>

    <slot />
  </div>
</template>

<style scoped>
.page-header-wrap {
  margin-bottom: 16px;
}

.page-header {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  justify-content: space-between;
  gap: 12px 16px;
}

.page-header__main {
  display: flex;
  align-items: center;
  min-width: 0;
  gap: 12px;
}

.page-header__sub {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
  margin-top: 8px;
}
</style>
