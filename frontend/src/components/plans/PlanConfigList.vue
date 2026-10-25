<script setup lang="ts" generic="T">
import { computed, reactive, ref, watch } from "vue";
import { compareNaturalLabels } from "@/utils/naturalSort";

/** 方案各配置页签共用的查找与分页；只筛选视图，不裁剪待保存的完整配置。 */
const props = defineProps<{
  items: T[];
  rowKey: string;
  numberOf: (item: T) => string | null | undefined;
  searchText: (item: T) => string;
  statusText: (item: T) => string;
  keywordPlaceholder: string;
  emptyText: string;
  materials?: { material_key: string; name: string }[];
  materialKeys?: (item: T) => readonly string[];
  groupLabel?: string;
  groupText?: (item: T) => string;
  summary?: string;
}>();

const query = reactive({ keyword: "", material: "", group: "", status: "" });
const page = ref(1);
const pageSize = ref(20);

function countedOptions(values: string[], selected: string) {
  const counts = new Map<string, number>();
  for (const value of values) counts.set(value, (counts.get(value) ?? 0) + 1);
  // 编辑使某一类变成零条后，仍保留当前筛选，明确展示零条结果。
  if (selected && !counts.has(selected)) counts.set(selected, 0);
  return Array.from(counts, ([value, count]) => ({ value, label: `${value}（${count}）` }))
    .sort((left, right) => compareNaturalLabels(left.value, right.value));
}

const groupOptions = computed(() => props.groupText
  ? countedOptions(props.items.map(props.groupText), query.group) : []);
const statusOptions = computed(() => countedOptions(props.items.map(props.statusText), query.status));
const filteredItems = computed(() => {
  const keyword = query.keyword.trim().toLocaleLowerCase();
  return props.items.filter((item) => {
    if (keyword && !props.searchText(item).toLocaleLowerCase().includes(keyword)) return false;
    if (query.material && !props.materialKeys?.(item).includes(query.material)) return false;
    if (query.group && props.groupText?.(item) !== query.group) return false;
    return !query.status || props.statusText(item) === query.status;
  }).sort((left, right) => compareNaturalLabels(props.numberOf(left), props.numberOf(right)));
});
const pagedItems = computed(() => filteredItems.value.slice((page.value - 1) * pageSize.value, page.value * pageSize.value));

watch(() => [query.keyword, query.material, query.group, query.status, pageSize.value], () => {
  page.value = 1;
});
watch(() => filteredItems.value.length, (total) => {
  page.value = Math.min(page.value, Math.max(1, Math.ceil(total / pageSize.value)));
});

function resetFilters(): void {
  Object.assign(query, { keyword: "", material: "", group: "", status: "" });
  page.value = 1;
}

</script>

<template>
  <div class="plan-config-list">
    <el-form inline class="app-query" @submit.prevent>
      <el-form-item label="搜索">
        <el-input v-model="query.keyword" clearable :placeholder="keywordPlaceholder" class="config-search" />
      </el-form-item>
      <el-form-item v-if="materialKeys" label="材料">
        <el-select v-model="query.material" clearable filterable placeholder="全部材料" class="config-material">
          <el-option v-for="material in materials" :key="material.material_key" :value="material.material_key" :label="material.name" />
        </el-select>
      </el-form-item>
      <el-form-item v-if="groupText" :label="groupLabel">
        <el-select v-model="query.group" clearable filterable placeholder="全部" class="config-group">
          <el-option v-for="option in groupOptions" :key="option.value" :value="option.value" :label="option.label" />
        </el-select>
      </el-form-item>
      <el-form-item label="状态">
        <el-select v-model="query.status" clearable placeholder="全部" class="config-status">
          <el-option v-for="option in statusOptions" :key="option.value" :value="option.value" :label="option.label" />
        </el-select>
      </el-form-item>
      <el-form-item><el-button @click="resetFilters">重置</el-button></el-form-item>
    </el-form>

    <div class="app-toolbar">
      <div class="app-toolbar__left"><slot name="actions" /></div>
      <div class="app-toolbar__right text-muted" aria-live="polite">
        <span>显示 {{ filteredItems.length }} / {{ items.length }} 条</span>
        <span v-if="summary">{{ summary }}</span>
      </div>
    </div>

    <el-table :data="pagedItems" :row-key="rowKey" border stripe class="management-table" :empty-text="items.length ? '未找到匹配的配置，请调整筛选条件' : emptyText">
      <slot />
    </el-table>

    <div v-if="filteredItems.length" class="app-pagination">
      <el-pagination
        v-model:current-page="page"
        v-model:page-size="pageSize"
        :total="filteredItems.length"
        :page-sizes="[10, 20, 50, 100]"
        layout="total, sizes, prev, pager, next, jumper"
        background
      />
    </div>
  </div>
</template>

<style scoped>
.config-search { width: 240px; }
.config-material { width: 180px; }
.config-group { width: 220px; }
.config-status { width: 160px; }
.app-toolbar__right { flex-wrap: wrap; gap: 8px 16px; }

@media (max-width: 640px) {
  .app-query :deep(.el-form-item) { display: flex; margin-right: 0; }
  .app-query :deep(.el-form-item__content) { min-width: 0; }
  .app-query :deep(.el-input),
  .app-query :deep(.el-select) { width: 100%; }
  .app-pagination { justify-content: flex-start; overflow-x: auto; }
}
</style>
