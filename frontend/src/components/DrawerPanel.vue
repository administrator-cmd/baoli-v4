<script setup lang="ts">
/**
 * 通用抽屉骨架：标题 + 说明 + 内容。
 * 尺寸按视口自适应，小屏下自动占满宽度，不与页面其它区域重叠。
 */
withDefaults(
  defineProps<{
    open: boolean;
    title: string;
    description?: string;
    direction?: "rtl" | "ltr" | "ttb" | "btt";
    /** 宽屏下的抽屉宽度百分比。 */
    size?: string | number;
  }>(),
  { description: "", direction: "rtl", size: "42%" },
);

defineEmits<{
  close: [];
}>();
</script>

<template>
  <el-drawer
    :model-value="open"
    :title="title"
    :direction="direction"
    :size="size"
    :destroy-on-close="false"
    append-to-body
    @update:model-value="$emit('close')"
  >
    <p v-if="description" class="text-muted drawer-panel__description">{{ description }}</p>
    <slot />
    <template v-if="$slots.footer" #footer>
      <div class="drawer-panel__footer">
        <slot name="footer" />
      </div>
    </template>
  </el-drawer>
</template>

<style scoped>
.drawer-panel__description {
  margin-bottom: 12px;
}

.drawer-panel__footer {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
}
</style>
