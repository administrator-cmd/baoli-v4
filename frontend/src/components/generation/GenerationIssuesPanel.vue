<script setup lang="ts">
import { computed, ref, watch } from "vue";
import { ElMessage } from "element-plus";
import { ListChecks } from "lucide-vue-next";
import type { GenerationDocument, GenerationEntry, GenerationIssue, GenerationIssueAnswer, GenerationSheet } from "@/api/contracts";
import { entryBusinessCode, entryLabel, findEntry, generationSourceText } from "@/utils/generationDocument";
import GenerationEntryEditor from "./GenerationEntryEditor.vue";

const props = defineProps<{
  document: GenerationDocument;
  disabled?: boolean;
  followUp?: { question: string; issueId?: string | null } | null;
  errorMessage?: string;
  submitAnswer: (message: string, issueId: string) => Promise<boolean>;
  submitBatch: (answers: GenerationIssueAnswer[]) => Promise<boolean>;
}>();
const filter = ref("OPEN");
const filterChosen = ref(false);
const issues = computed(() => props.document.issues.filter(issue => !filter.value || issue.status === filter.value));
const pendingIssues = computed(() => props.document.issues.filter(issue => issue.status === "OPEN"));
watch(() => props.document.issues, items => {
  if (!filterChosen.value) filter.value = items.length && !items.some(item => item.status === "OPEN") ? "RESOLVED" : "OPEN";
}, { immediate: true });
const dialog = ref(false);
const batchDialog = ref(false);
const selected = ref<GenerationIssue | null>(null);
const answers = ref<Record<string, string>>({});
const batchAnswers = computed(() => pendingIssues.value.flatMap(issue => {
  const message = answers.value[issue.issue_id]?.trim();
  return message ? [{ issue_id: issue.issue_id, answer: message }] : [];
}));
const answer = computed({
  get: () => selected.value ? answers.value[selected.value.issue_id] ?? selected.value.resolution : "",
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
  dialog.value = true;
}
function openBatch(): void {
  if (props.disabled || pendingIssues.value.length < 2) return;
  submitError.value = "";
  batchDialog.value = true;
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
async function submitBatchAnswers(): Promise<void> {
  if (props.disabled || submitting.value || !batchAnswers.value.length) return;
  const submitted = batchAnswers.value;
  submitting.value = true;
  submitError.value = "";
  try {
    if (await props.submitBatch(submitted)) {
      submitted.forEach(item => delete answers.value[item.issue_id]);
      batchDialog.value = false;
    } else submitError.value = props.errorMessage || "未能提交，已填写的答案已保留，请重试。";
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
    <div class="issue-panel__header">
      <div><h3>业务口径确认</h3><p>回答后，AI 会更新相关规则。</p></div>
      <div class="issue-panel__header-actions">
        <el-radio-group v-model="filter" aria-label="补充状态" class="issue-panel__toolbar" @change="filterChosen = true">
          <el-radio-button value="OPEN">待补充（{{ pendingIssues.length }}）</el-radio-button>
          <el-radio-button value="RESOLVED">已明确</el-radio-button>
          <el-radio-button value="">全部</el-radio-button>
        </el-radio-group>
        <el-button v-if="filter !== 'RESOLVED' && pendingIssues.length >= 2" :icon="ListChecks" :disabled="disabled" @click="openBatch">批量回答</el-button>
      </div>
    </div>

    <div v-if="issues.length" class="issue-panel__list">
      <article v-for="issue in issues" :key="issue.issue_id" class="issue-panel__card" :class="{ 'is-resolved': issue.status === 'RESOLVED' }">
        <div class="issue-panel__card-main">
          <div class="issue-panel__card-heading">
            <h4>{{ issue.title }}</h4>
            <el-tag :type="issue.status === 'OPEN' ? 'warning' : 'success'" size="small">{{ issue.status === 'OPEN' ? '待补充' : '已明确' }}</el-tag>
          </div>
          <p v-if="followUp?.issueId === issue.issue_id" class="issue-panel__detail">{{ followUp.question }}</p>
          <details class="issue-panel__evidence"><summary>查看依据与涉及内容</summary>
            <p v-if="issue.detail" class="issue-panel__detail">{{ issue.detail }}</p>
            <p class="issue-panel__detail">{{ generationSourceText(issue.source) }}</p>
            <div class="issue-panel__affected">
              <span>涉及内容</span>
              <template v-if="issue.affected_row_ids.length">
                <el-button v-for="reference in issue.affected_row_ids" :key="reference" class="issue-panel__link" link type="primary" @click="openEntry(reference)">{{ rowLabel(reference) }}</el-button>
              </template>
              <span v-else class="issue-panel__scope">规则库整体口径</span>
            </div>
          </details>
          <p v-if="issue.status === 'RESOLVED' && issue.resolution" class="issue-panel__resolution"><strong>已补充</strong>{{ issue.resolution }}</p>
        </div>
        <div class="issue-panel__card-action">
          <el-button v-if="issue.status === 'OPEN'" type="primary" @click="openAnswer(issue)" :disabled="disabled">回答</el-button>
          <el-button v-else @click="openAnswer(issue)" :disabled="disabled">查看 / 修改</el-button>
        </div>
      </article>
    </div>
    <el-empty v-else :image-size="64" :description="filter === 'OPEN' ? '没有待补充事项' : '当前没有符合条件的事项'" />

    <el-dialog v-model="dialog" title="补充业务口径" width="min(760px, 94vw)" append-to-body destroy-on-close :close-on-click-modal="false" :close-on-press-escape="!submitting" :show-close="!submitting">
      <template v-if="selected">
        <h3 class="issue-panel__question">{{ selected.title }}</h3>
        <p v-if="followUp?.issueId === selected.issue_id" class="issue-panel__text">{{ followUp.question }}</p>
        <el-form label-position="top" :disabled="disabled || submitting">
          <el-form-item label="你的补充" required><el-input v-model="answer" type="textarea" :autosize="{ minRows: 4, maxRows: 10 }" maxlength="6000" placeholder="填写实际的业务口径，AI 会据此修改相关内容" /></el-form-item>
        </el-form>
        <el-alert v-if="submitError" :title="submitError" type="error" :closable="false" />
        <el-collapse><el-collapse-item title="查看原文依据" name="source"><p v-if="selected.detail" class="issue-panel__text">{{ selected.detail }}</p><p class="issue-panel__text">{{ generationSourceText(selected.source) }}</p></el-collapse-item></el-collapse>
      </template>
      <template #footer><el-button :disabled="submitting" @click="dialog = false">取消</el-button><el-button type="primary" :loading="submitting" :disabled="disabled || !answer.trim()" @click="submit">提交并更新规则</el-button></template>
    </el-dialog>
    <el-dialog v-model="batchDialog" title="批量回答" width="min(800px, 94vw)" append-to-body destroy-on-close
      :close-on-click-modal="false" :close-on-press-escape="!submitting" :show-close="!submitting">
      <el-alert v-if="submitError" class="issue-panel__batch-error" :title="submitError" type="error" show-icon :closable="false" />
      <el-form v-if="pendingIssues.length" class="issue-panel__batch-form" label-position="top" :disabled="disabled || submitting">
        <el-form-item v-for="issue in pendingIssues" :key="issue.issue_id" :label="issue.title" class="issue-panel__batch-item">
          <p v-if="followUp?.issueId === issue.issue_id" class="issue-panel__batch-note">{{ followUp.question }}</p>
          <el-input v-model="answers[issue.issue_id]" type="textarea" :autosize="{ minRows: 2, maxRows: 6 }" maxlength="6000" placeholder="填写业务口径" />
          <details class="issue-panel__evidence"><summary>查看依据与涉及内容</summary>
            <p v-if="issue.detail" class="issue-panel__detail">{{ issue.detail }}</p>
            <p class="issue-panel__detail">{{ generationSourceText(issue.source) }}</p>
            <div v-if="issue.affected_row_ids.length" class="issue-panel__affected">
              <span>涉及内容</span>
              <el-button v-for="reference in issue.affected_row_ids" :key="reference" class="issue-panel__link" link type="primary" @click="openEntry(reference)">{{ rowLabel(reference) }}</el-button>
            </div>
          </details>
        </el-form-item>
      </el-form>
      <el-empty v-else :image-size="64" description="没有待补充事项" />
      <template #footer>
        <div class="issue-panel__batch-footer">
          <span class="issue-panel__batch-count" role="status">已填写 {{ batchAnswers.length }} / {{ pendingIssues.length }} 项</span>
          <div class="issue-panel__batch-buttons">
            <el-button :disabled="submitting" @click="batchDialog = false">取消</el-button>
            <el-button type="primary" :loading="submitting" :disabled="disabled || !batchAnswers.length" @click="submitBatchAnswers">提交已填答案</el-button>
          </div>
        </div>
      </template>
    </el-dialog>
    <GenerationEntryEditor v-model="entryDialog" :sheet="viewingSheet" :entry="viewingEntry" :requirements="document.requirements" :entries="allEntries" read-only />
  </div>
</template>

<style scoped>
.issue-panel { min-width: 0; }
.issue-panel__header { display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 12px 20px; margin-bottom: 14px; }
.issue-panel__header h3 { margin: 0; color: #1c2e3d; font-size: 16px; font-weight: 650; }
.issue-panel__header p { margin: 5px 0 0; color: #7c8a95; font-size: 11px; line-height: 1.5; }
.issue-panel__header-actions { display: flex; align-items: center; flex-wrap: wrap; gap: 8px; }
.issue-panel__toolbar { flex: 0 0 auto; }
.issue-panel__toolbar :deep(.el-radio-button__inner) { font-size: 11px; }
.issue-panel__evidence { margin-top: 8px; }
.issue-panel__evidence summary { width: fit-content; cursor: pointer; color: #536b78; font-size: 12px; }
.issue-panel__list { display: grid; gap: 10px; }
.issue-panel__card { display: flex; align-items: center; justify-content: space-between; gap: 18px; padding: 17px 18px; border: 1px solid #e9e6dc; border-left: 3px solid #d39a3f; border-radius: 6px; background: #fffefa; }
.issue-panel__card.is-resolved { border-color: #e6ece9; border-left-color: #68a28a; background: #fff; }
.issue-panel__card-main { min-width: 0; flex: 1; }
.issue-panel__card-heading { display: flex; align-items: center; flex-wrap: wrap; gap: 8px 10px; }
.issue-panel__card-heading h4 { margin: 0; color: #263949; font-size: 13px; font-weight: 650; line-height: 1.5; }
.issue-panel__detail { margin: 7px 0 10px; color: #687987; font-size: 11px; line-height: 1.7; white-space: pre-wrap; overflow-wrap: anywhere; }
.issue-panel__affected { display: flex; align-items: baseline; flex-wrap: wrap; gap: 4px 10px; color: #84909a; font-size: 10px; }
.issue-panel__affected > span:first-child { flex: 0 0 auto; color: #6e7d88; font-weight: 600; }
.issue-panel__link { height: auto; margin: 0; white-space: normal; text-align: left; font-size: 10px; line-height: 1.5; }
.issue-panel__scope { color: #778590; }
.issue-panel__resolution { display: flex; gap: 8px; margin: 11px 0 0; padding-top: 9px; border-top: 1px solid #edf0ee; color: #66766f; font-size: 11px; line-height: 1.6; white-space: pre-wrap; overflow-wrap: anywhere; }
.issue-panel__resolution strong { flex: 0 0 auto; color: #4c7864; font-weight: 600; }
.issue-panel__card-action { flex: 0 0 auto; }
.issue-panel__question { margin: 0 0 8px; color: #263949; font-size: 16px; }
.issue-panel__text { margin: 6px 0 12px; color: #62717d; font-size: 12px; line-height: 1.75; white-space: pre-wrap; overflow-wrap: anywhere; }
.issue-panel__batch-error { margin-bottom: 12px; }
.issue-panel__batch-form { max-height: min(55vh, 560px); overflow-y: auto; padding: 2px 8px 2px 2px; }
.issue-panel__batch-item { margin: 0; padding: 14px 0; }
.issue-panel__batch-item + .issue-panel__batch-item { border-top: 1px solid var(--el-border-color-lighter); }
.issue-panel__batch-item:first-child { padding-top: 2px; }
.issue-panel__batch-item :deep(.el-form-item__label) { height: auto; line-height: 1.5; white-space: normal; overflow-wrap: anywhere; font-weight: 600; }
.issue-panel__batch-item :deep(.el-form-item__content) { display: block; line-height: 1.5; }
.issue-panel__batch-note { margin: 0 0 8px; color: var(--el-text-color-regular); white-space: pre-wrap; overflow-wrap: anywhere; }
.issue-panel__batch-footer { display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 10px; }
.issue-panel__batch-count { color: var(--el-text-color-regular); font-size: 13px; }
.issue-panel__batch-buttons { display: flex; flex-wrap: wrap; gap: 8px; margin-inline-start: auto; }
.issue-panel__batch-buttons > .el-button { margin: 0; }
@media (max-width: 640px) { .issue-panel__header { align-items: flex-start; flex-direction: column; } .issue-panel__toolbar { max-width: 100%; } .issue-panel__toolbar :deep(.el-radio-button__inner) { padding: 8px 9px; font-size: 10px; } .issue-panel__card { align-items: stretch; flex-direction: column; gap: 12px; padding: 14px; } .issue-panel__card-action { align-self: flex-start; } }
</style>
