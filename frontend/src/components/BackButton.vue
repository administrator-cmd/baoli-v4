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
  <el-button class="app-back-button" plain @click="$emit('click', $event)">
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
</style>
