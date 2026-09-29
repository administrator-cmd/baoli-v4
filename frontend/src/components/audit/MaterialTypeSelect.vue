<script setup lang="ts">
import { onMounted } from "vue";

import { useMaterialTypeCatalog, type MaterialTypeCatalog } from "@/composables/useMaterialTypeCatalog";

/**
 * 材料类型选择：上传材料时由用户从 8018 支持目录中选择。
 *
 * 目录是**外部服务的当前事实**，本组件不内置任何默认清单，也不把 8018 的取值与
 * 材料名称做映射。目录读不到时明确报错并让上传入口不可用——不退回自由输入，
 * 也不跳过类型声明：8018 的 doc_type 是严格枚举，自由输入的值会在服务端被直接拒绝。
 */
const model = defineModel<string>({ required: true });

const props = defineProps<{
  /** 上传等操作进行中：与目录不可用一起决定选择框能否操作。 */
  disabled?: boolean;
  catalog?: MaterialTypeCatalog;
  showError?: boolean;
}>();

const catalog = props.catalog ?? useMaterialTypeCatalog();
onMounted(() => {
  if (!props.catalog) void catalog.reload();
});
</script>

<template>
  <div class="material-type-select">
    <el-select
      v-model="model"
      filterable
      clearable
      :loading="catalog.state.loading"
      :disabled="props.disabled || !catalog.state.available"
      placeholder="请选择材料类型"
      size="small"
      style="min-width: 180px"
    >
      <el-option v-for="item in catalog.state.items" :key="item.id" :label="item.name" :value="item.name" />
    </el-select>
    <div v-if="catalog.state.error && props.showError !== false" class="text-hint">
      {{ catalog.state.error }}（该目录不可用时不能上传）
      <el-button link type="primary" size="small" :loading="catalog.state.loading" @click="catalog.reload">重试</el-button>
    </div>
  </div>
</template>

<style scoped>
.material-type-select {
  display: inline-flex;
  flex-direction: column;
  gap: 4px;
}
</style>
