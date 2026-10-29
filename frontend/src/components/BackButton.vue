<script setup lang="ts">
import { ArrowLeft } from "lucide-vue-next";

withDefaults(defineProps<{ label?: string }>(), {
  label: "返回",
});

defineEmits<{
  click: [event: MouseEvent];
}>();
</script>

<template>
  <el-button class="app-back-button" type="info" plain @click="$emit('click', $event)">
    <template #icon><ArrowLeft :size="16" aria-hidden="true" /></template>
    {{ label }}
  </el-button>
</template>

<style scoped>
.app-back-button {
  flex: 0 0 auto;
  min-width: 96px;
  font-weight: 500;
}

/* 返回是导航动作，用中性浅灰底与页面动作按钮区分 */
.app-back-button.el-button--info.is-plain {
  --el-button-bg-color: var(--el-color-info-light-8);
}
</style>
