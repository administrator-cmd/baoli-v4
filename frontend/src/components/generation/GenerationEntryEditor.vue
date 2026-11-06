<script setup lang="ts">
import { ref, watch } from "vue";
import type { GenerationEntry, GenerationRequirement, GenerationSheet } from "@/api/contracts";
import { cloneValue, generationSourceText } from "@/utils/generationDocument";

const props = defineProps<{
  modelValue: boolean;
  sheet: GenerationSheet | null;
  entry: GenerationEntry | null;
  requirements: GenerationRequirement[];
  entries: Array<GenerationEntry & { label: string }>;
  disabled?: boolean;
  isNew?: boolean;
  readOnly?: boolean;
}>();
const emit = defineEmits<{
  "update:modelValue": [value: boolean];
  save: [entry: GenerationEntry];
}>();
const form = ref<GenerationEntry | null>(null);
watch(() => props.modelValue, (open) => {
  if (open) form.value = props.entry ? cloneValue(props.entry) : null;
});
function confirm(): void {
  if (!form.value || props.disabled || props.readOnly) return;
  emit("save", cloneValue(form.value));
  emit("update:modelValue", false);
}
</script>

<template>
  <el-dialog
    :model-value="modelValue" :title="isNew ? '新增业务条目' : readOnly ? '查看业务条目' : '修改业务条目'"
    width="min(1040px, 94vw)" append-to-body destroy-on-close
    :close-on-click-modal="false" @update:model-value="emit('update:modelValue', $event)"
  >
    <template v-if="form && sheet">
      <p class="entry-editor__context">{{ sheet.name }}<span v-if="sheet.description"> · {{ sheet.description }}</span></p>
      <el-form label-position="top" :disabled="disabled || readOnly" class="entry-editor__grid">
        <el-form-item label="内容性质">
          <el-select v-model="form.entry_kind" placeholder="其他内容">
            <el-option label="业务要求" value="RULE" /><el-option label="材料说明" value="MATERIAL" /><el-option label="字典、映射等辅助资料" value="REFERENCE" /><el-option label="其他内容" value="OTHER" />
          </el-select>
        </el-form-item>
        <el-form-item label="关联条目">
          <el-select v-model="form.related_row_ids" multiple filterable clearable placeholder="有明确关系时选择">
            <el-option v-for="item in entries.filter(item => item.row_id !== form?.row_id)" :key="item.row_id" :value="item.row_id" :label="`${item.business_code || ''} ${item.label}`" />
          </el-select>
        </el-form-item>
        <el-form-item
          v-for="column in sheet.columns" :key="column.column_id" :label="column.name"
          :class="{ 'entry-editor__wide': (form.cells[column.column_id] || '').length > 100 }"
        >
          <el-input v-model="form.cells[column.column_id]" type="textarea" :autosize="{ minRows: 2, maxRows: 10 }" />
          <span v-if="column.description" class="text-hint">{{ column.description }}</span>
        </el-form-item>
      </el-form>
      <el-collapse class="entry-editor__source">
        <el-collapse-item title="查看原文依据与编制说明" name="source">
          <p>{{ generationSourceText(form.source) }}</p>
          <p v-for="item in requirements.filter((item) => form?.source.requirement_ids.includes(item.requirement_id))" :key="item.requirement_id">
            {{ item.text }}
          </p>
        </el-collapse-item>
      </el-collapse>
    </template>
    <template #footer>
      <el-button @click="emit('update:modelValue', false)">{{ readOnly ? '关闭' : '取消' }}</el-button>
      <el-button v-if="!readOnly" type="primary" :disabled="disabled" @click="confirm">应用到规则库</el-button>
    </template>
  </el-dialog>
</template>

<style scoped>
.entry-editor__context { margin: 0 0 20px; color: var(--el-text-color-secondary); }
.entry-editor__grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 0 24px; }
.entry-editor__wide { grid-column: 1 / -1; }
.entry-editor__source p { white-space: pre-wrap; overflow-wrap: anywhere; margin: 0 0 10px; }
@media (max-width: 700px) { .entry-editor__grid { grid-template-columns: minmax(0, 1fr); } }
</style>
