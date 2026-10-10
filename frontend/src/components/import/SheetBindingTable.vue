<script setup lang="ts">
import { FileSpreadsheet } from "lucide-vue-next";

import type { ImportSheet, JsonValue } from "@/api/contracts";
import { formatBusinessValue } from "@/constants/labels";
import type { SheetBindingDraft } from "@/constants/sheetBinding";

const props = defineProps<{
  sheets: ImportSheet[];
  drafts: Record<string, SheetBindingDraft>;
}>();
const emit = defineEmits<{
  "update:draft": [payload: { sheetKey: string; draft: SheetBindingDraft }];
}>();
function draftOf(sheet: ImportSheet): SheetBindingDraft {
  return props.drafts[sheet.sheet_key] ?? {
    excluded: sheet.status === "EXCLUDED",
  };
}
function update(sheet: ImportSheet, patch: Partial<SheetBindingDraft>): void {
  emit("update:draft", { sheetKey: sheet.sheet_key, draft: { ...draftOf(sheet), ...patch } });
}

/** 按列字母合并各行地址，避免 A1、A2 被当成两个不同列。 */
function previewColumns(sheet: ImportSheet): string[] {
  const addresses = new Set<string>();
  for (const row of sheet.rows) {
    for (const address of Object.keys(row.cells)) {
      addresses.add(columnLabel(address));
    }
  }
  return Array.from(addresses)
    .sort((left, right) => {
      const leftColumn = left.replace(/[0-9]/g, "");
      const rightColumn = right.replace(/[0-9]/g, "");
      if (leftColumn.length !== rightColumn.length) {
        return leftColumn.length - rightColumn.length;
      }
      if (leftColumn !== rightColumn) {
        return leftColumn < rightColumn ? -1 : 1;
      }
      return Number(left.replace(/[^0-9]/g, "")) - Number(right.replace(/[^0-9]/g, ""));
    })
    .slice(0, 24);
}

function columnLabel(address: string): string {
  return address.replace(/[0-9]/g, "");
}

/**
 * 预览行：以单元格地址为键的扁平记录，值可能是文本也可能是行号。
 */
type PreviewRow = Record<string, string | number>;

function previewRows(sheet: ImportSheet): PreviewRow[] {
  const columns = previewColumns(sheet);
  return sheet.rows.map((row) => {
    const record: PreviewRow = { row_no: row.row_no };
    for (const address of columns) {
      record[address] = formatBusinessValue(row.cells[`${address}${row.row_no}`] as JsonValue);
    }
    return record;
  });
}

</script>

<template>
  <el-table :data="sheets" row-key="sheet_key" border size="default" class="sheet-table">
    <el-table-column type="expand">
      <template #default="{ row }">
        <div class="sheet-expand">
          <div v-if="row.rows.length">
            <div class="text-muted">前 {{ row.rows.length }} 行数据（最多显示 24 列，空白表示该单元格没有内容）</div>
            <el-table
              :data="previewRows(row)"
              border
              size="small"
              max-height="280"
              class="block-gap-sm"
            >
              <el-table-column prop="row_no" label="行号" width="72" fixed />
              <el-table-column
                v-for="address in previewColumns(row)"
                :key="address"
                :prop="address"
                :label="columnLabel(address)"
                min-width="120"
                show-overflow-tooltip
              />
            </el-table>
            <div class="text-hint block-gap-sm">
              <span v-if="row.summary.merged_ranges.length">本表存在合并单元格 {{ row.summary.merged_ranges.length }} 处。</span>
              <span v-if="row.summary.truncated"> 此处展示预览，解析读取完整工作表。</span>
              <span v-if="row.summary.unprocessed_note"> {{ row.summary.unprocessed_note }}</span>
            </div>
          </div>
          <el-empty v-else description="这张工作表没有被读取到数据行" :image-size="60" />

          <el-descriptions :column="2" size="small" border class="block-gap">
            <el-descriptions-item label="工作表">
              {{ row.name }}
            </el-descriptions-item>
            <el-descriptions-item label="最大行 / 最大列">
              {{ row.summary.max_row }} / {{ row.summary.max_column }}
            </el-descriptions-item>
          </el-descriptions>
        </div>
      </template>
    </el-table-column>

    <el-table-column label="工作表" min-width="200">
      <template #default="{ row }">
        <div class="sheet-name">
          <el-icon><FileSpreadsheet /></el-icon>
          <span>{{ row.name }}</span>
          <el-tag v-if="!row.visible" size="small" type="info" disable-transitions>隐藏</el-tag>
        </div>
        <span class="table-note">
          {{ row.summary.non_empty_rows }} 行 · {{ row.summary.max_column }} 列
          <template v-if="row.summary.truncated"> · 预览已截取</template>
        </span>
      </template>
    </el-table-column>

    <el-table-column label="参与导入" width="120" align="center">
      <template #default="{ row }">
        <el-switch
          :model-value="!draftOf(row).excluded"
          @update:model-value="update(row, { excluded: !$event })"
        />
      </template>
    </el-table-column>
  </el-table>

</template>

<style scoped>
.sheet-table {
  width: 100%;
}

.sheet-name {
  display: flex;
  align-items: center;
  gap: 6px;
  font-weight: 600;
  overflow-wrap: anywhere;
}

.sheet-problems {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
  margin-top: 6px;
}

.sheet-select,
.sheet-number {
  width: 100%;
}

.sheet-expand {
  padding: 8px 12px 12px;
}
</style>
