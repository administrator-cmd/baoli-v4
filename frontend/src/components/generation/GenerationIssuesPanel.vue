<script setup lang="ts">
import { computed, ref, watch } from "vue";
import { ElMessage } from "element-plus";
import { useMediaQuery } from "@/composables/useMediaQuery";
import type { GenerationDocument, GenerationEntry, GenerationIssue, GenerationSheet } from "@/api/contracts";
import { entryBusinessCode, entryLabel, findEntry, generationSourceText } from "@/utils/generationDocument";
import GenerationEntryEditor from "./GenerationEntryEditor.vue";

const props = defineProps<{
  document: GenerationDocument;
  disabled?: boolean;
  errorMessage?: string;
  submitAnswer: (message: string, issueId: string) => Promise<boolean>;
}>();
const filter = ref("OPEN");
const filterChosen = ref(false);
const pinActions = useMediaQuery("(min-width: 993px)");
const issues = computed(() => props.document.issues.filter(issue => !filter.value || issue.status === filter.value));
watch(() => props.document.issues, items => {
  if (!filterChosen.value) filter.value = items.length && !items.some(item => item.status === "OPEN") ? "RESOLVED" : "OPEN";
}, { immediate: true });
const dialog = ref(false);
const selected = ref<GenerationIssue | null>(null);
const answers = ref<Record<string, string>>({});
const answer = computed({
  get: () => selected.value ? answers.value[selected.value.issue_id] ?? "" : "",
  set: (value: string) => { if (selected.value) answers.value[selected.value.issue_id] = value; },
});
const submitting = ref(false);
const submitError = ref("");
const entryDialog = ref(false);
const viewingEntry = ref<GenerationEntry | null>(null);
const viewingSheet = ref<GenerationSheet | null>(null);
const allEntries = computed(() => props.document.sheets.flatMap(sheet => sheet.rows.map(row => ({ ...row, business_code: entryBusinessCode(sheet, row), label: entryLabel(sheet, row) }))));

function rowLabel(reference: string): string {
  const found = findEntry(props.document, reference);
  return found ? `${found.sheet.name} · ${entryLabel(found.sheet, found.row)}` : "相关条目已不存在";
}
function openAnswer(issue: GenerationIssue): void {
  if (props.disabled) return;
  selected.value = issue;
  submitError.value = "";
  if (!(issue.issue_id in answers.value)) answers.value[issue.issue_id] = issue.resolution;
  dialog.value = true;
}
async function submit(): Promise<void> {
  if (!selected.value || props.disabled || submitting.value) return;
  const message = answer.value.trim();
  if (!message) { ElMessage.error("请填写你的补充"); return; }
  const issueId = selected.value.issue_id;
  submitting.value = true;
  submitError.value = "";
  try {
    if (await props.submitAnswer(message, issueId)) {
      delete answers.value[issueId];
      dialog.value = false;
    } else submitError.value = props.errorMessage || "未能提交，补充内容已保留，请重试。";
  } finally {
    submitting.value = false;
  }
}
function openEntry(reference: string): void {
  const found = findEntry(props.document, reference);
  if (!found) { ElMessage.error("相关业务条目已不存在"); return; }
  viewingEntry.value = found.row;
  viewingSheet.value = found.sheet;
  entryDialog.value = true;
}
</script>

<template>
  <div class="issue-panel">
    <el-radio-group v-model="filter" aria-label="补充状态" class="issue-panel__toolbar" @change="filterChosen = true">
      <el-radio-button value="OPEN">待补充（{{ document.issues.filter(item => item.status === 'OPEN').length }}）</el-radio-button>
      <el-radio-button value="RESOLVED">已明确</el-radio-button>
      <el-radio-button value="">全部</el-radio-button>
    </el-radio-group>
    <el-table :data="issues" border stripe class="management-table" empty-text="当前没有符合条件的补充事项">
      <el-table-column label="需要补充的内容" min-width="300">
        <template #default="{ row }"><strong>{{ row.title }}</strong><p class="issue-panel__preview" :title="row.detail">{{ row.detail }}</p></template>
      </el-table-column>
      <el-table-column label="涉及内容" min-width="240">
        <template #default="{ row }">
          <el-button v-for="reference in row.affected_row_ids" :key="reference" class="issue-panel__link" link type="primary" @click="openEntry(reference)">{{ rowLabel(reference) }}</el-button>
          <span v-if="!row.affected_row_ids.length">规则库整体口径</span>
        </template>
      </el-table-column>
      <el-table-column label="已补充内容" min-width="220"><template #default="{ row }"><div class="issue-panel__preview" :title="row.resolution">{{ row.resolution || '—' }}</div></template></el-table-column>
      <el-table-column label="状态" width="120"><template #default="{ row }"><el-tag :type="row.status === 'OPEN' ? 'warning' : 'info'">{{ row.status === 'OPEN' ? '待补充' : '已明确' }}</el-tag></template></el-table-column>
      <el-table-column label="操作" width="150" :fixed="pinActions ? 'right' : false" class-name="table-actions">
        <template #default="{ row }"><el-button link type="primary" :disabled="disabled" @click="openAnswer(row)">{{ row.status === 'OPEN' ? '补充答案' : '修改补充' }}</el-button></template>
      </el-table-column>
    </el-table>
    <el-dialog v-model="dialog" title="补充业务口径" width="min(760px, 94vw)" append-to-body destroy-on-close :close-on-click-modal="false" :close-on-press-escape="!submitting" :show-close="!submitting">
      <template v-if="selected">
        <h3 class="issue-panel__question">{{ selected.title }}</h3>
        <p class="issue-panel__text">{{ selected.detail }}</p>
        <el-form label-position="top" :disabled="disabled || submitting">
          <el-form-item label="你的补充" required><el-input v-model="answer" type="textarea" :autosize="{ minRows: 4, maxRows: 10 }" maxlength="6000" placeholder="填写实际的业务口径，AI 会据此修改相关内容" /></el-form-item>
        </el-form>
        <el-alert v-if="submitError" :title="submitError" type="error" :closable="false" />
        <el-collapse><el-collapse-item title="查看原文依据" name="source"><p class="issue-panel__text">{{ generationSourceText(selected.source) }}</p></el-collapse-item></el-collapse>
      </template>
      <template #footer><el-button :disabled="submitting" @click="dialog = false">取消</el-button><el-button type="primary" :loading="submitting" :disabled="disabled || !answer.trim()" @click="submit">交给 AI 修改</el-button></template>
    </el-dialog>
    <GenerationEntryEditor v-model="entryDialog" :sheet="viewingSheet" :entry="viewingEntry" :requirements="document.requirements" :entries="allEntries" read-only />
  </div>
</template>

<style scoped>
.issue-panel__toolbar { margin-bottom: 16px; }
.issue-panel__text, .issue-panel__preview { margin: 6px 0 12px; white-space: pre-wrap; overflow-wrap: anywhere; line-height: 1.7; }
.issue-panel__preview { display: -webkit-box; -webkit-line-clamp: 3; -webkit-box-orient: vertical; overflow: hidden; }
.issue-panel__link { display: block; margin: 0 0 8px; text-align: left; white-space: normal; height: auto; line-height: 1.6; }
.issue-panel__question { margin: 0 0 8px; font-size: 16px; }
</style>
