<script setup lang="ts">
import { computed } from "vue";
import { FileSpreadsheet } from "lucide-vue-next";

import type { ImportSheet, JsonValue } from "@/api/contracts";
import type { ProfileSheet } from "@/api/imports";
import { formatBusinessValue } from "@/constants/labels";
import {
  DEFAULT_HEADER_ROW,
  defaultNodeCodeForRole,
  nodeOptionsForRole,
  type NodeOption,
  type SheetBindingDraft,
} from "@/constants/sheetBinding";

/**
 * Sheet 绑定表。
 *
 * 只列出需要业务确认的工作表（服务端未 fixed_excluded 的表）；模板说明页等元数据与
 * 没有业务数据的工作表由页面单独列出，不在这里出现，也不会被当成业务 Sheet。
 * 主信息是工作表中文名称和它的用途；技术标识放在展开行里。
 *
 * 「本表用途」与「绑定节点」的初值由 `constants/sheetBinding.initialBindingOf` 给出
 * （与作业侧的「是否有未提交改动」共用同一份口径）。绑定仍以用户提交的值为准。
 */
const props = defineProps<{
  sheets: ImportSheet[];
  drafts: Record<string, SheetBindingDraft>;
  /**
   * 统一模板协议声明的 Sheet：用途下拉的**唯一**来源。
   *
   * 不能在组件里写死固定的几个用途：协议里的 Sheet 数量与名称由协议声明，
   * 写死的那一套会让用户根本选不到某些用途。
   */
  profileSheets: ProfileSheet[];
  /**
   * 当前场景里用户配置的**节点实例**：节点绑定的唯一来源。
   *
   * 一张 Sheet 只能绑定一个节点；一个节点可以绑定多张 Sheet。
   * 该用途下只有一个可选实例时，绑定初值会直接选中它（见 `constants/sheetBinding`）。
   */
  nodeOptions: NodeOption[];
  /** 行内错误：用途冲突、缺少表头行、未选择用途或未绑定节点。 */
  problemsOf: (sheet: ImportSheet) => string[];
}>();

const emit = defineEmits<{
  "update:draft": [payload: { sheetKey: string; draft: SheetBindingDraft }];
}>();

/** 用途下拉选项：名称与可绑定节点类型都来自统一模板协议。 */
const roleOptions = computed(() =>
  props.profileSheets.map((item) => ({
    value: item.sheet_role,
    label: item.display_name,
  })),
);

/** 该用途可以绑定的节点实例：与预选默认值共用同一条规则（见 constants/sheetBinding）。 */
function nodeOptionsFor(role: string | null | undefined): Array<{ value: string; label: string }> {
  return nodeOptionsForRole(props.profileSheets, props.nodeOptions, role).map((item) => ({
    value: item.nodeCode,
    label: item.label,
  }));
}

/** 换用途时把节点带上该用途的默认值：只有一个可选实例就选中，否则清空由用户选。 */
function defaultNodeCodeOf(role: string | null | undefined): string | null {
  return defaultNodeCodeForRole(props.profileSheets, props.nodeOptions, role);
}

function draftOf(sheet: ImportSheet): SheetBindingDraft {
  return (
    props.drafts[sheet.sheet_key] ?? {
      role: sheet.role ?? sheet.suggested_role ?? null,
      nodeCode: sheet.node_code ?? defaultNodeCodeOf(sheet.role ?? sheet.suggested_role),
      headerRow: sheet.header_row ?? DEFAULT_HEADER_ROW,
      excluded: sheet.status === "EXCLUDED",
    }
  );
}

function update(sheet: ImportSheet, patch: Partial<SheetBindingDraft>): void {
  emit("update:draft", { sheetKey: sheet.sheet_key, draft: { ...draftOf(sheet), ...patch } });
}

/** 不参与本次导入的工作表不需要再选用途与表头行。 */
function roleDisabled(sheet: ImportSheet): boolean {
  return draftOf(sheet).excluded;
}

/** 预览列：取自全部行已填充单元格地址的并集，最多 24 列，避免不同行错位。 */
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
              <span class="mono">{{ row.role ?? row.suggested_role ?? "未识别" }}</span>
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

    <el-table-column label="本表用途" width="220">
      <template #default="{ row }">
        <el-select
          :model-value="draftOf(row).role ?? ''"
          :disabled="roleDisabled(row)"
          placeholder="请选择用途"
          class="sheet-select"
          @update:model-value="update(row, { role: $event || null, nodeCode: defaultNodeCodeOf($event || null) })"
        >
          <el-option label="尚未选择" value="" />
          <el-option v-for="role in roleOptions" :key="role.value" :label="role.label" :value="role.value" />
        </el-select>
      </template>
    </el-table-column>

    <el-table-column label="绑定节点" width="200">
      <template #default="{ row }">
        <el-select
          :model-value="draftOf(row).nodeCode ?? ''"
          :disabled="roleDisabled(row) || !draftOf(row).role"
          placeholder="请选择业务节点"
          class="sheet-select"
          @update:model-value="update(row, { nodeCode: $event || null })"
        >
          <el-option
            v-for="option in nodeOptionsFor(draftOf(row).role)"
            :key="option.value"
            :label="option.label"
            :value="option.value"
          />
        </el-select>
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
