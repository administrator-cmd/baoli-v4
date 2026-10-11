<script setup lang="ts">
import StateBlock from "@/components/StateBlock.vue";

export interface CandidateColumn {
  key: string;
  label: string;
  /** 该列是主列（中文业务名称）。 */
  primary?: boolean;
  width?: number;
  minWidth?: number;
}

export interface CandidateRow {
  ref: string;
  /** 中文业务名称，作为主列。 */
  name: string;
  /** 已按列顺序填好的业务值。 */
  values: string[];
  /** 来源信息：Excel 工作表、行号、单元格、原始内容摘要。 */
  source: string;
  /** 技术编码等次要信息，只在展开行显示。 */
  details: Array<{ label: string; value: string }>;
}

/**
 * 候选清单表格。
 *
 * 主列永远是中文业务名称，来源用业务语言描述；
 * 技术编码、候选引用、原始内容等统一收进展开行。
 * 导入页面只展示解析结果；编辑与启停在场景草稿和方案中进行。
 */
defineProps<{
  columns: CandidateColumn[];
  rows: CandidateRow[];
  /** 空候选中显示的业务说明，避免出现空白区域。 */
  emptyTitle: string;
  emptyDescription: string;
  loading?: boolean;
}>();

</script>

<template>
  <StateBlock v-if="loading" kind="loading" />
  <StateBlock v-else-if="!rows.length" kind="empty" :title="emptyTitle" :description="emptyDescription" />
  <el-table v-else :data="rows" row-key="ref" border class="candidate-table">
    <el-table-column type="expand">
      <template #default="{ row }">
        <el-descriptions :column="1" size="small" border class="candidate-detail">
          <el-descriptions-item v-for="(detail, index) in row.details" :key="index" :label="detail.label">
            {{ detail.value }}
          </el-descriptions-item>
        </el-descriptions>
      </template>
    </el-table-column>

    <el-table-column
      v-for="(column, index) in columns"
      :key="column.key"
      :label="column.label"
      :width="column.width"
      :min-width="column.minWidth ?? (column.primary ? 220 : 140)"
      show-overflow-tooltip
    >
      <template #default="{ row }">
        <template v-if="column.primary">
          <span class="candidate-name">{{ row.values[index] }}</span>
          <span class="table-note">来源：{{ row.source }}</span>
        </template>
        <span v-else>{{ row.values[index] }}</span>
      </template>
    </el-table-column>

  </el-table>
</template>

<style scoped>
.candidate-table {
  width: 100%;
}

.candidate-name {
  font-weight: 600;
}

.candidate-detail {
  padding: 8px 12px;
}
.candidate-detail :deep(.el-descriptions__content) {
  overflow-wrap: anywhere;
  white-space: normal;
}
</style>
