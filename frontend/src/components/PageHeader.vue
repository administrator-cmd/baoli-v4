<script setup lang="ts">
import { RefreshCw } from "lucide-vue-next";

defineProps<{
  /** 当前页面名称，作为主标题。 */
  title: string;
  /** 页面上方的小字，标明所属模块。 */
  eyebrow?: string;
  /** 当前作业/任务的中文名称。 */
  objectName?: string | null;
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
    <el-page-header :icon="null" class="page-header">
      <template #title>
        <span>{{ eyebrow || "工作台" }}</span>
      </template>
      <template #content>
        <h1 class="page-header__title">
          {{ title }}
          <span v-if="objectName" class="page-header__title-text">{{ objectName }}</span>
        </h1>
      </template>
      <template #extra>
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
      </template>
    </el-page-header>

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

.page-header__sub {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
  margin-top: 8px;
}
</style>
