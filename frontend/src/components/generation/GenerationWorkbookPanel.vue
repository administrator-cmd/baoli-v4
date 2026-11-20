<script setup lang="ts">
import { computed, ref, watch } from "vue";
import { ElMessageBox } from "element-plus";
import { Info, SlidersHorizontal } from "lucide-vue-next";
import { useMediaQuery } from "@/composables/useMediaQuery";
import type { GenerationDocument, GenerationEntry, GenerationSheet } from "@/api/contracts";
import { businessCodeColumn, cloneValue, entryBusinessCode, entryCellValue, entryLabel, entryReferenceLabel, generationSourceText, newGenerationRef, newUserSource, updateEntry } from "@/utils/generationDocument";
import GenerationEntryEditor from "./GenerationEntryEditor.vue";
import GenerationSheetEditor from "./GenerationSheetEditor.vue";

const props = defineProps<{ document: GenerationDocument; disabled?: boolean; refreshToken?: number }>();
const emit = defineEmits<{ change: [document: GenerationDocument] }>();
const pinActions = useMediaQuery("(min-width: 993px)");
const selectedSheet = ref("");
const search = ref("");
const page = ref(1);
const pageSize = ref(20);
const current = computed(() => props.document.sheets.find((sheet) => sheet.sheet_id === selectedSheet.value) ?? null);
const showCodeColumn = computed(() => {
  const sheet = current.value;
  return Boolean(sheet && !businessCodeColumn(sheet) && sheet.rows.some(row => entryBusinessCode(sheet, row)));
});
const allEntries = computed(() => props.document.sheets.flatMap(sheet => sheet.rows.map(row => ({
  ...row, business_code: entryBusinessCode(sheet, row), referenceLabel: entryReferenceLabel(sheet, row),
  label: `${sheet.name} · ${entryLabel(sheet, row)}`,
}))));
const codeById = computed(() => new Map(allEntries.value.map(row => [row.row_id, row.referenceLabel])));
watch(() => props.document.sheets.map((sheet) => sheet.sheet_id), (identifiers) => {
  if (!identifiers.includes(selectedSheet.value)) selectedSheet.value = identifiers[0] ?? "";
}, { immediate: true });
watch([selectedSheet, search, pageSize], () => { page.value = 1; });
const filtered = computed(() => {
  const keyword = search.value.trim().toLowerCase();
  return (current.value?.rows ?? []).filter((row) => !keyword
    || (current.value && entryBusinessCode(current.value, row).toLowerCase().includes(keyword))
    || Object.values(row.cells).some((value) => value.toLowerCase().includes(keyword)));
});
watch(() => filtered.value.length, (length) => { page.value = Math.min(page.value, Math.max(1, Math.ceil(length / pageSize.value))); });
const visibleRows = computed(() => filtered.value.slice((page.value - 1) * pageSize.value, page.value * pageSize.value));
const entryDialog = ref(false);
const editingRow = ref<GenerationEntry | null>(null);
const entryIsNew = ref(false);
const entryReadOnly = ref(false);
const sheetDialog = ref(false);
const editingSheet = ref<GenerationSheet | null>(null);
const sheetIsNew = ref(false);
const notesDialog = ref(false);
watch(() => props.refreshToken, () => {
  entryDialog.value = false;
  sheetDialog.value = false;
  notesDialog.value = false;
  editingRow.value = null;
  editingSheet.value = null;
});

function openEntry(row?: GenerationEntry, readOnly = false): void {
  if (!current.value || props.disabled) return;
  entryIsNew.value = !row;
  entryReadOnly.value = readOnly;
  editingRow.value = row ?? {
    row_id: newGenerationRef("row"),
    cells: Object.fromEntries(current.value.columns.map((column) => [column.column_id, ""])),
    source: newUserSource(),
  };
  entryDialog.value = true;
}
function saveEntry(row: GenerationEntry): void {
  if (!current.value || props.disabled) return;
  emit("change", updateEntry(props.document, current.value.sheet_id, row));
}
async function removeEntry(row: GenerationEntry): Promise<void> {
  if (!current.value || props.disabled) return;
  const sheetId = current.value.sheet_id;
  const linked = props.document.issues.some((issue) => issue.affected_row_ids.includes(row.row_id));
  try {
    await ElMessageBox.confirm(
      `确认删除此业务条目？${linked ? '相关待明确事项会保留，并移除此条目的关联。' : ''}保存修改后生效。`,
      "删除业务条目", { type: "warning", confirmButtonText: "删除", cancelButtonText: "保留" },
    );
  } catch { return; }
  if (props.disabled) return;
  emit("change", {
    ...props.document,
    sheets: props.document.sheets.map((sheet) => ({ ...sheet, rows: sheet.rows
      .filter(item => sheet.sheet_id !== sheetId || item.row_id !== row.row_id)
      .map(item => ({ ...item, related_row_ids: item.related_row_ids?.filter(id => id !== row.row_id) })) })),
    issues: props.document.issues.map((issue) => ({ ...issue, affected_row_ids: issue.affected_row_ids.filter((id) => id !== row.row_id) })),
  });
}
function openSheet(isNew: boolean): void {
  if (props.disabled) return;
  sheetIsNew.value = isNew;
  editingSheet.value = isNew ? {
    sheet_id: newGenerationRef("sheet"), name: "", description: "", rows: [], source: newUserSource(),
    columns: [{ column_id: newGenerationRef("column"), name: "业务内容", description: "" }],
  } : current.value ? cloneValue(current.value) : null;
  sheetDialog.value = true;
}
function saveSheet(sheet: GenerationSheet): void {
  if (props.disabled) return;
  const exists = props.document.sheets.some((item) => item.sheet_id === sheet.sheet_id);
  emit("change", { ...props.document, sheets: exists
    ? props.document.sheets.map((item) => item.sheet_id === sheet.sheet_id ? sheet : item)
    : [...props.document.sheets, sheet] });
  selectedSheet.value = sheet.sheet_id;
}
async function removeSheet(): Promise<void> {
  const sheet = current.value;
  if (!sheet || props.disabled) return;
  try {
    await ElMessageBox.confirm(
      `删除「${sheet.name}」及其中的 ${sheet.rows.length} 条内容？相关待明确事项会保留，并移除本表条目的关联。保存修改后生效。`,
      "删除业务表", { type: "warning", confirmButtonText: "删除表格及内容", cancelButtonText: "保留" },
    );
  } catch { return; }
  if (props.disabled) return;
  const removed = new Set(sheet.rows.map((row) => row.row_id));
  emit("change", { ...props.document, sheets: props.document.sheets.filter((item) => item.sheet_id !== sheet.sheet_id)
      .map(item => ({ ...item, rows: item.rows.map(row => ({ ...row, related_row_ids: row.related_row_ids?.filter(id => !removed.has(id)) })) })),
    issues: props.document.issues.map((issue) => ({ ...issue, affected_row_ids: issue.affected_row_ids.filter((id) => !removed.has(id)) })) });
}
</script>

<template>
  <div class="workbook-panel">
    <div class="workbook-panel__main">
      <template v-if="current">
        <div class="workbook-panel__toolbar">
          <el-select v-model="selectedSheet" aria-label="选择业务表" class="workbook-panel__sheet-select" filterable placeholder="选择业务表">
            <el-option v-for="sheet in document.sheets" :key="sheet.sheet_id" :value="sheet.sheet_id" :label="sheet.name" />
          </el-select>
          <el-input v-model="search" class="workbook-panel__search" aria-label="搜索当前表格" placeholder="搜索当前表的条目" clearable />
          <div class="workbook-panel__toolbar-actions">
            <el-button v-if="current.description || document.summary" :icon="Info" @click="notesDialog = true">说明</el-button>
            <el-dropdown trigger="click" :disabled="disabled">
              <el-button :icon="SlidersHorizontal" :disabled="disabled">手动调整</el-button>
              <template #dropdown><el-dropdown-menu>
                <el-dropdown-item @click="openEntry()">新增条目</el-dropdown-item>
                <el-dropdown-item @click="openSheet(false)">调整表格</el-dropdown-item>
                <el-dropdown-item @click="openSheet(true)">新增业务表</el-dropdown-item>
                <el-dropdown-item divided @click="removeSheet">删除此表</el-dropdown-item>
              </el-dropdown-menu></template>
            </el-dropdown>
          </div>
        </div>
        <el-table :data="visibleRows" border stripe row-key="row_id" class="management-table" empty-text="没有匹配的业务条目">
          <el-table-column type="index" label="序号" width="64" :index="(index: number) => (page - 1) * pageSize + index + 1" />
          <el-table-column v-if="showCodeColumn" label="编号" min-width="135"><template #default="{ row }">{{ entryBusinessCode(current, row) }}</template></el-table-column>
          <el-table-column v-for="(column, index) in current.columns" :key="column.column_id" :label="column.name" :min-width="index === 0 ? 170 : 250">
            <template #default="{ row }"><div class="workbook-panel__cell" :title="entryCellValue(current, row, column.column_id)">{{ entryCellValue(current, row, column.column_id) || '—' }}</div></template>
          </el-table-column>
          <el-table-column v-if="current.rows.some(row => row.related_row_ids?.length)" label="关联条目" min-width="180">
            <template #default="{ row }"><div class="workbook-panel__cell" :title="(row.related_row_ids ?? []).map((id: string) => codeById.get(id)).join('；')">{{ (row.related_row_ids ?? []).map((id: string) => codeById.get(id)).join('；') }}</div></template>
          </el-table-column>
          <el-table-column label="原文依据" width="190" show-overflow-tooltip>
            <template #default="{ row }">{{ generationSourceText(row.source) }}</template>
          </el-table-column>
          <el-table-column label="操作" width="200" :fixed="pinActions ? 'right' : false" class-name="table-actions">
            <template #default="{ row }">
              <el-button link type="primary" :disabled="disabled" @click="openEntry(row, true)">查看</el-button>
              <el-button link type="primary" :disabled="disabled" @click="openEntry(row)">修改</el-button>
              <el-button link type="danger" :disabled="disabled" @click="removeEntry(row)">删除</el-button>
            </template>
          </el-table-column>
        </el-table>
        <el-pagination v-model:current-page="page" v-model:page-size="pageSize" class="workbook-panel__pagination" :page-sizes="[20, 50, 100]" :total="filtered.length" layout="total, sizes, prev, pager, next" />
      </template>
      <el-empty v-else description="尚无业务表格，可新增表格或查看待明确事项">
        <el-button type="primary" :disabled="disabled" @click="openSheet(true)">新增业务表</el-button>
      </el-empty>
    </div>
    <el-dialog v-model="notesDialog" title="规则库说明" width="min(640px, 94vw)" append-to-body destroy-on-close>
      <div class="workbook-panel__notes">
        <section v-if="current?.description">
          <h3>业务表说明</h3>
          <p>{{ current.description }}</p>
        </section>
        <section v-if="document.summary">
          <h3>编制说明</h3>
          <p>{{ document.summary }}</p>
        </section>
      </div>
    </el-dialog>
    <GenerationEntryEditor v-model="entryDialog" :sheet="current" :entry="editingRow" :requirements="document.requirements" :entries="allEntries" :disabled="disabled" :is-new="entryIsNew" :read-only="entryReadOnly" @save="saveEntry" />
    <GenerationSheetEditor v-model="sheetDialog" :sheet="editingSheet" :sheets="document.sheets" :disabled="disabled" :is-new="sheetIsNew" @save="saveSheet" />
  </div>
</template>

<style scoped>
.workbook-panel { min-width: 0; }
.workbook-panel__main { min-width: 0; }
.workbook-panel__toolbar { display: flex; flex-wrap: wrap; align-items: center; gap: 10px; margin-bottom: 10px; }
.workbook-panel__sheet-select { width: min(280px, 100%); }
.workbook-panel__search { flex: 1 1 170px; width: min(260px, 100%); max-width: 260px; }
.workbook-panel__toolbar-actions { display: flex; flex-wrap: wrap; align-items: center; gap: 10px; margin-inline-start: auto; }
.workbook-panel__notes { display: grid; gap: 16px; max-height: 55vh; overflow: auto; }
.workbook-panel__notes h3 { margin: 0 0 6px; color: var(--el-text-color-primary); font-size: 14px; }
.workbook-panel__notes p { margin: 0; line-height: 1.65; white-space: pre-wrap; overflow-wrap: anywhere; }
.workbook-panel__cell { display: -webkit-box; overflow: hidden; white-space: pre-wrap; overflow-wrap: anywhere; line-height: 1.65; -webkit-line-clamp: 3; -webkit-box-orient: vertical; }
.workbook-panel__pagination { justify-content: flex-end; margin-top: 14px; overflow-x: auto; }
.workbook-panel__main :deep(.el-table) { --el-table-border-color: #e8edf0; --el-table-header-bg-color: #f4f7f8; }
@media (max-width: 700px) {
  .workbook-panel__pagination { justify-content: flex-start; }
}
@media (max-width: 560px) {
  .workbook-panel__sheet-select, .workbook-panel__search { flex: 0 0 100%; width: 100%; max-width: none; }
  .workbook-panel__toolbar-actions { margin-inline-start: 0; }
}
</style>
