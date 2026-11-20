<script setup lang="ts">
import { computed, ref } from "vue";
import { ElMessage } from "element-plus";
import type { DependencyQuestion, ImportJob, ProposalRule } from "@/api/contracts/imports";
import { updateImportProposal } from "@/api/imports";

const props = defineProps<{ job: ImportJob; rule: ProposalRule; questions: DependencyQuestion[]; disabled: boolean }>();
const emit = defineEmits<{ saved: [job: ImportJob]; busy: [value: boolean]; failed: [error: unknown] }>();
const answers = ref<string[]>([]);
const saving = ref(false);
const complete = computed(() => props.questions.every((q, i) => q.options.some(o => o.ref === answers.value[i])));
const labels = { fields: "字段", rules: "规则", calculations: "计算指标" };

async function save() {
  if (!complete.value || saving.value || props.disabled) return;
  saving.value = true;
  emit("busy", true);
  try {
    const updated = await updateImportProposal(props.job.job_no, "rules", props.rule.candidate_ref,
      props.job.draft_revision, { dependency_answers: answers.value });
    emit("saved", updated);
    ElMessage.success("已保存规则引用");
  } catch (error) {
    emit("failed", error);
  } finally {
    saving.value = false;
    emit("busy", false);
  }
}
</script>

<template>
  <section class="dependency-review">
    <strong>{{ rule.name }}</strong>
    <details v-if="rule.origin?.rows?.length">
      <summary>查看 Excel 原文 · {{ rule.origin.sheet_name }} 第 {{ rule.origin.row_no }} 行</summary>
      <p v-for="row in rule.origin.rows" :key="JSON.stringify([row.sheet_name, row.row_no])">
        {{ row.sheet_name }} · 第 {{ row.row_no }} 行 · {{ Object.values(row.cells).filter(v => v != null && v !== '').join(' ｜ ') }}
      </p>
    </details>
    <label v-for="(q, i) in questions" :key="i">
      “{{ q.term }}”指哪一项？
      <el-select v-model="answers[i]" :aria-label="`${q.term}的引用对象`" :disabled="disabled || saving" placeholder="选择引用对象">
        <el-option v-for="o in q.options" :key="o.ref" :value="o.ref"
          :label="`${o.name} · ${labels[o.group]} · ${o.sheet_name} 第 ${o.row_no} 行（${o.ref}）`" />
      </el-select>
    </label>
    <el-button type="primary" plain :disabled="!complete || disabled" :loading="saving" @click="save">确认引用</el-button>
  </section>
</template>

<style scoped>
.dependency-review { display: grid; justify-items: start; gap: 10px; margin: 12px 0; padding: 14px; border-left: 3px solid var(--el-color-warning); background: var(--el-fill-color-lighter); }
.dependency-review label { display: grid; gap: 5px; width: min(100%, 38rem); }
.dependency-review details { color: var(--el-text-color-secondary); font-size: 13px; overflow-wrap: anywhere; }
.dependency-review summary { cursor: pointer; }
</style>
