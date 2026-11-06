<script setup lang="ts">
import { ref, watch } from "vue";
import { ElMessage, ElMessageBox } from "element-plus";
import type { GenerationSheet } from "@/api/contracts";
import { cloneValue, newGenerationRef } from "@/utils/generationDocument";

const props = defineProps<{
  modelValue: boolean;
  sheet: GenerationSheet | null;
  sheets: GenerationSheet[];
  disabled?: boolean;
  isNew?: boolean;
}>();
const emit = defineEmits<{
  "update:modelValue": [value: boolean];
  save: [sheet: GenerationSheet];
}>();
const form = ref<GenerationSheet | null>(null);
watch(() => props.modelValue, (open) => {
  if (open) form.value = props.sheet ? cloneValue(props.sheet) : null;
});
function addColumn(): void {
  if (!form.value || props.disabled) return;
  const columnId = newGenerationRef("column");
  form.value.columns.push({ column_id: columnId, name: "", description: "" });
  form.value.rows.forEach((row) => { row.cells[columnId] = ""; });
}
function moveColumn(index: number, direction: number): void {
  if (!form.value || props.disabled) return;
  const target = index + direction;
  const columns = form.value.columns;
  if (target < 0 || target >= columns.length) return;
  [columns[index], columns[target]] = [columns[target], columns[index]];
}
async function removeColumn(index: number): Promise<void> {
  const current = form.value;
  if (!current || props.disabled) return;
  if (current.columns.length === 1) {
    ElMessage.warning("业务表至少保留一个栏目");
    return;
  }
  const column = current.columns[index];
  const hasContent = current.rows.some((row) => row.cells[column.column_id].trim());
  if (hasContent) {
    try {
      await ElMessageBox.confirm(
        `删除「${column.name || '未命名栏目'}」会同时删除本表该栏目中的内容。应用表格修改后生效。`,
        "删除栏目", { type: "warning", confirmButtonText: "删除栏目及内容", cancelButtonText: "保留" },
      );
    } catch { return; }
  }
  if (form.value !== current || props.disabled) return;
  current.columns.splice(index, 1);
  current.rows.forEach((row) => { delete row.cells[column.column_id]; });
}
function confirm(): void {
  if (!form.value || props.disabled) return;
  const name = form.value.name.trim();
  if (!name) { ElMessage.error("请填写业务表名称"); return; }
  if (name.length > 31 || ["\\", "/", "*", "?", ":", "[", "]"].some((char) => name.includes(char))
    || name.startsWith("'") || name.endsWith("'")) {
    ElMessage.error("表名需符合 Excel 命名规则：不超过31字，不包含 \\ / * ? : [ ]，不以单引号开头或结尾");
    return;
  }
  if (props.sheets.some((sheet) => sheet.sheet_id !== form.value?.sheet_id && sheet.name.toLowerCase() === name.toLowerCase())) {
    ElMessage.error("已有同名业务表，请换一个名称"); return;
  }
  const names = form.value.columns.map((column) => column.name.trim());
  if (names.some((column) => !column) || new Set(names).size !== names.length) {
    ElMessage.error("请填写每个栏目名称，同一表内的栏目名称不能重复"); return;
  }
  form.value.name = name;
  form.value.columns.forEach((column, index) => { column.name = names[index]; });
  emit("save", cloneValue(form.value));
  emit("update:modelValue", false);
}
</script>

<template>
  <el-dialog
    :model-value="modelValue" :title="isNew ? '新增业务表' : '调整业务表'"
    width="min(900px, 94vw)" append-to-body destroy-on-close :close-on-click-modal="false"
    @update:model-value="emit('update:modelValue', $event)"
  >
    <el-form v-if="form" label-position="top" :disabled="disabled">
      <el-form-item label="业务表名称" required><el-input v-model="form.name" maxlength="31" show-word-limit /></el-form-item>
      <el-form-item label="阅读说明"><el-input v-model="form.description" type="textarea" :rows="2" placeholder="说明这张表的业务范围或阅读方法" /></el-form-item>
      <el-table :data="form.columns" border>
        <el-table-column label="栏目名称" min-width="160">
          <template #default="{ row }"><el-input v-model="row.name" placeholder="例如：适用范围、判断标准" /></template>
        </el-table-column>
        <el-table-column label="填写说明（可选）" min-width="210">
          <template #default="{ row }"><el-input v-model="row.description" /></template>
        </el-table-column>
        <el-table-column label="顺序与操作" width="190">
          <template #default="{ $index }">
            <el-button link :disabled="$index === 0 || disabled" @click="moveColumn($index, -1)">前移</el-button>
            <el-button link :disabled="$index === form.columns.length - 1 || disabled" @click="moveColumn($index, 1)">后移</el-button>
            <el-button link type="danger" :disabled="disabled" @click="removeColumn($index)">删除</el-button>
          </template>
        </el-table-column>
      </el-table>
      <el-button class="block-gap-sm" :disabled="disabled" @click="addColumn">新增栏目</el-button>
    </el-form>
    <template #footer>
      <el-button @click="emit('update:modelValue', false)">取消</el-button>
      <el-button type="primary" :disabled="disabled" @click="confirm">应用表格修改</el-button>
    </template>
  </el-dialog>
</template>
