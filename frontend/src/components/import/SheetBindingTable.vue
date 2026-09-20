<script setup lang="ts">
import { computed } from "vue";
import { FileSpreadsheet } from "lucide-vue-next";

import type { ImportSheet, JsonValue } from "@/api/contracts";
import StatusBadge from "@/components/StatusBadge.vue";
import { formatBusinessValue, sheetRoleDescription, sheetRoleLabel, sheetRoleOptions } from "@/constants/labels";

export interface SheetBindingDraft {
  role: string | null;
  headerRow: number | null;
  excluded: boolean;
}

/**
 * Sheet 绑定表。
 *
 * 只列出需要业务确认的工作表（服务端未 fixed_excluded 的表）；模板说明页等元数据与
 * 没有业务数据的工作表由页面单独列出，不在这里出现，也不会被当成业务 Sheet。
 * 主信息是工作表中文名称和系统识别出的用途；技术标识放在展开行里。
 * 未选择用途的工作表明确标记为「不参与本次发布」，不会被静默忽略。
 */
const props = defineProps<{
  sheets: ImportSheet[];
  drafts: Record<string, SheetBindingDraft>;
  /** 行内错误：用途冲突、缺少表头行、未选择用途。 */
  problemsOf: (sheet: ImportSheet) => string[];
}>();

const emit = defineEmits<{
  "update:draft": [payload: { sheetKey: string; draft: SheetBindingDraft }];
}>();

function draftOf(sheet: ImportSheet): SheetBindingDraft {
  return props.drafts[sheet.sheet_key] ?? { role: sheet.role, headerRow: sheet.header_row, excluded: false };
}

function update(sheet: ImportSheet, patch: Partial<SheetBindingDraft>): void {
  emit("update:draft", { sheetKey: sheet.sheet_key, draft: { ...draftOf(sheet), ...patch } });
}

/** 不参与本次发布的工作表不需要再选用途与表头行。 */
function roleDisabled(sheet: ImportSheet): boolean {
  return draftOf(sheet).excluded;
}

/**
 * 预览列：取自全部行已填充单元格地址的并集，最多 24 列，避免不同行错位。
 */
function previewColumns(sheet: ImportSheet): string[] {
  const addresses = new Set<string>();
  for (const row of sheet.rows) {
    for (const address of Object.keys(row.cells)) {
      addresses.add(address);
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
      record[address] = formatBusinessValue(row.cells[address] as JsonValue);
    }
    return record;
  });
}

const sheetNameHint = computed(
  () => "系统已按表头识别用途；没有识别出用途的工作表必须手动选择用途，或明确设为不参与本次发布。",
);
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
              <span v-if="row.summary.truncated"> 本次只读取了文件的一部分。</span>
              <span v-if="row.summary.unprocessed_note"> {{ row.summary.unprocessed_note }}</span>
            </div>
          </div>
          <el-empty v-else description="这张工作表没有被读取到数据行" :image-size="60" />

          <el-descriptions :column="2" size="small" border class="block-gap">
            <el-descriptions-item label="工作表标识">
              <span class="mono">{{ row.sheet_key }}</span>
            </el-descriptions-item>
            <el-descriptions-item label="识别状态">
              <span class="mono">{{ row.status }}</span>
            </el-descriptions-item>
            <el-descriptions-item label="识别到的用途编码">
              <span class="mono">{{ row.role ?? "未识别" }}</span>
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
          <template v-if="row.summary.truncated"> · 本次只读取了一部分</template>
        </span>
        <div v-if="problemsOf(row).length" class="sheet-problems">
          <el-tag v-for="problem in problemsOf(row)" :key="problem" type="danger" size="small" effect="plain" disable-transitions>
            {{ problem }}
          </el-tag>
        </div>
      </template>
    </el-table-column>

    <el-table-column label="系统建议用途" width="140">
      <template #default="{ row }">
        <span v-if="row.role">{{ sheetRoleLabel(row.role) }}</span>
        <span v-else class="text-hint">未识别出用途</span>
      </template>
    </el-table-column>

    <el-table-column label="本表用途" width="200">
      <template #default="{ row }">
        <el-select
          :model-value="draftOf(row).role ?? ''"
          :disabled="roleDisabled(row)"
          placeholder="不参与本次发布"
          class="sheet-select"
          @update:model-value="update(row, { role: $event || null })"
        >
          <el-option label="不参与本次发布" value="" />
          <el-option v-for="role in sheetRoleOptions" :key="role.value" :label="role.label" :value="role.value" />
        </el-select>
        <span class="table-note">{{ sheetRoleDescription(roleDisabled(row) ? null : draftOf(row).role) }}</span>
      </template>
    </el-table-column>

    <el-table-column label="表头行" width="130">
      <template #default="{ row }">
        <el-input-number
          :model-value="draftOf(row).headerRow ?? undefined"
          :min="1"
          :max="row.summary.max_row"
          :disabled="roleDisabled(row)"
          controls-position="right"
          class="sheet-number"
          @update:model-value="update(row, { headerRow: typeof $event === 'number' ? $event : null })"
        />
        <span class="table-note">{{ row.header_row ? `系统识别在 ${row.header_row} 行` : "请按实际文件填写" }}</span>
      </template>
    </el-table-column>

    <el-table-column label="参与发布" width="110" align="center">
      <template #default="{ row }">
        <el-switch
          :model-value="!draftOf(row).excluded"
          @update:model-value="update(row, { excluded: !$event })"
        />
        <span class="table-note">{{ draftOf(row).excluded ? "不参与" : "参与" }}</span>
      </template>
    </el-table-column>

    <el-table-column label="状态" width="120">
      <template #default="{ row }">
        <StatusBadge :status="row.status" />
      </template>
    </el-table-column>
  </el-table>

  <p class="text-hint block-gap-sm">{{ sheetNameHint }}</p>
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
