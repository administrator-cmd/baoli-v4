<script setup lang="ts">
import type { RuleInputSnapshot } from "@/api/contracts";
import StatusBadge from "@/components/StatusBadge.vue";
import { formatBusinessValue } from "@/constants/labels";
import { quoteInlineText } from "@/utils/quoteText";

/** 规则输入快照：材料级检查逐份列出，字段级检查展示原文与标准化结果。 */
defineProps<{
  materialRows: RuleInputSnapshot[];
  values: RuleInputSnapshot[];
}>();
</script>

<template>
  <el-table v-if="materialRows.length" :data="materialRows" size="small" border empty-text="没有材料级输入">
    <el-table-column label="材料" min-width="150">
      <template #default="{ row }">{{ row.material_name || "未知材料" }}</template>
    </el-table-column>
    <el-table-column label="文件名" min-width="180" show-overflow-tooltip>
      <template #default="{ row }">{{ row.filename || "—" }}</template>
    </el-table-column>
    <el-table-column label="本份判定" width="120">
      <template #default="{ row }">
        <StatusBadge v-if="row.status" domain="rule" :status="row.status" />
        <span v-else>—</span>
      </template>
    </el-table-column>
    <el-table-column label="说明" min-width="220" show-overflow-tooltip>
      <template #default="{ row }">{{ row.message || "—" }}</template>
    </el-table-column>
  </el-table>

  <el-table v-else-if="values.length" :data="values" size="small" border>
    <el-table-column label="材料 · 字段" min-width="200">
      <template #default="{ row }">{{ row.material_name || "未知材料" }} · {{ row.field_name || row.field_key }}</template>
    </el-table-column>
    <el-table-column label="原文内容" min-width="200">
      <template #default="{ row }">
        <span v-if="row.raw_value" class="detail-quote">{{ quoteInlineText(row.raw_value) }}</span>
        <span v-else class="text-muted">未取得原文</span>
      </template>
    </el-table-column>
    <el-table-column label="标准化结果" min-width="150">
      <template #default="{ row }">
        <span v-if="row.normalized_value !== null && row.normalized_value !== undefined">
          {{ formatBusinessValue(row.normalized_value) }}
        </span>
        <span v-else class="text-muted">未形成标准化结果</span>
      </template>
    </el-table-column>
    <el-table-column label="取值状态" width="120">
      <template #default="{ row }">
        <StatusBadge v-if="row.status" domain="field" :status="row.status" />
        <span v-else>—</span>
      </template>
    </el-table-column>
  </el-table>
</template>

<style scoped>
.detail-quote {
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}
</style>
