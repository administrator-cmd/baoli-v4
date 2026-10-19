<script setup lang="ts">
import { computed, ref } from "vue";

import type { DraftCalculationItem } from "@/api/contracts";
import { sourceSheetLabel, useRuleSourceFilter } from "@/composables/useRuleSourceFilter";

const props = defineProps<{ items: DraftCalculationItem[] }>();
const search = ref("");
const { selectedSheet, sourceSheetOptions, matchesSourceSheet } = useRuleSourceFilter(() => props.items);
const keyword = computed(() => search.value.trim().toLowerCase());
const filteredCalculations = computed(() => props.items.filter((item) => {
  if (!matchesSourceSheet(item)) return false;
  return !keyword.value || `${item.name ?? ""}\n${item.rule_code ?? ""}`.toLowerCase().includes(keyword.value);
}));

function resetFilters(): void {
  search.value = "";
  selectedSheet.value = "";
}
</script>

<template>
  <div>
    <el-form inline class="app-query block-gap-sm">
      <el-form-item label="来源工作表">
        <el-select v-model="selectedSheet" clearable filterable placeholder="全部工作表" style="width: 240px">
          <el-option v-for="sheet in sourceSheetOptions" :key="sheet.value" :value="sheet.value" :label="sheet.label" />
        </el-select>
      </el-form-item>
      <el-form-item label="规则搜索">
        <el-input v-model="search" clearable placeholder="规则名称或业务编号" style="width: 220px" />
      </el-form-item>
      <el-form-item>
        <el-button @click="resetFilters">重置</el-button>
      </el-form-item>
    </el-form>

    <div class="app-toolbar">
      <span class="text-muted">显示 {{ filteredCalculations.length }} / {{ items.length }} 条</span>
    </div>

    <el-table :data="filteredCalculations" border stripe :empty-text="items.length ? '未找到匹配的规则' : '当前配置没有计算规则'">
      <el-table-column prop="rule_code" label="规则编号" min-width="120" />
      <el-table-column prop="name" label="名称" min-width="160" />
      <el-table-column label="来源工作表" min-width="180" show-overflow-tooltip>
        <template #default="{ row }">
          {{ sourceSheetLabel(row.origin) }}<span v-if="row.origin?.sheet_name" class="text-hint"> · 第 {{ row.origin.row_no }} 行</span>
        </template>
      </el-table-column>
      <el-table-column prop="formula" label="计算方法" min-width="220" show-overflow-tooltip />
      <el-table-column prop="requirement" label="业务要求" min-width="220" show-overflow-tooltip />
      <el-table-column prop="result_code" label="结果编号" min-width="120" />
      <el-table-column prop="unit" label="单位" width="90" />
      <el-table-column prop="currency" label="币种" width="90" />
      <el-table-column prop="status" label="状态" width="110" />
    </el-table>
  </div>
</template>
