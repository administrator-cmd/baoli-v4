<script setup lang="ts">
import { computed, onMounted, onUnmounted, reactive, ref, watch } from "vue";
import { onBeforeRouteLeave, onBeforeRouteUpdate, useRoute, useRouter } from "vue-router";
import { ElMessage, ElMessageBox } from "element-plus";
import type { DraftFieldItem, DraftMaterialItem, DraftRuleItem, JsonValue } from "@/api/contracts";
import { createDraftRule, getSceneConfiguration, updateDraftRule, type DraftRulePayload } from "@/api/scenes";
import BackButton from "@/components/BackButton.vue";
import PageHeader from "@/components/PageHeader.vue";
import { EXECUTION_MODE_OPTIONS, type ExecutionMode } from "@/constants/executionModes";

const route = useRoute();
const router = useRouter();
const scenarioCode = computed(() => String(route.params.scenarioCode ?? ""));
const ruleKey = computed(() => String(route.params.ruleKey ?? "new"));
const creating = computed(() => ruleKey.value === "new");
const fields = ref<DraftFieldItem[]>([]);
const materials = ref<DraftMaterialItem[]>([]);
const rules = ref<DraftRuleItem[]>([]);
const existing = ref<DraftRuleItem | null>(null);
const loading = ref(false);
const saving = ref(false);
const errorMessage = ref("");
/** 当前表单内容对应的对象身份（场景码:规则键）；提交时用它确认写的是表单所显示的规则。 */
const formIdentity = ref("");
/** 最近一次从服务端载入的表单快照；与表单不同就是有未保存输入。 */
const loadedFormJson = ref("");
let loadSequence = 0;
const form = reactive({ name: "", source_ref: "", requirement: "", config_status: "CLEAR" as "CLEAR" | "PENDING" | "DISABLED",
  execution_mode: "UNKNOWN" as ExecutionMode,
  rule_role: "CHECK" as "CHECK" | "RISK", field_keys: [] as string[], output_keys: [] as string[], material_keys: [] as string[], source_rule_keys: [] as string[] });

function identityOf(sceneCode: string, key: string): string {
  return `${sceneCode}:${key}`;
}

/** 清空上一条规则的内容：路由复用同一组件时，不能把上一条规则的字段留在表单里。 */
function resetForm(): void {
  existing.value = null;
  formIdentity.value = "";
  form.name = "";
  form.source_ref = "";
  form.requirement = "";
  form.config_status = "CLEAR";
  form.execution_mode = "UNKNOWN";
  form.rule_role = "CHECK";
  form.field_keys = [];
  form.output_keys = [];
  form.material_keys = [];
  form.source_rule_keys = [];
}

function applyRule(rule: DraftRuleItem) {
  existing.value = rule;
  form.name = rule.name;
  form.source_ref = rule.source_ref ?? "";
  form.requirement = rule.requirement;
  form.config_status = rule.config_status === "PENDING" ? "PENDING" : rule.enabled ? "CLEAR" : "DISABLED";
  form.execution_mode = (rule.execution_mode || "UNKNOWN") as ExecutionMode;
  form.rule_role = rule.rule_role ?? "CHECK";
  form.field_keys = rule.inputs.flatMap((i) => i.field_key ? [i.field_key] : []);
  form.output_keys = rule.outputs.flatMap((i) => i.field_key ? [i.field_key] : []);
  form.material_keys = rule.materials.map((i) => i.material_key);
  form.source_rule_keys = rule.source_rule_keys ?? (rule.source_rule_key ? [rule.source_rule_key] : []);
}

async function load() {
  const sequence = ++loadSequence;
  const sceneCode = scenarioCode.value;
  const key = ruleKey.value;
  const isCreating = key === "new";
  loading.value = true;
  errorMessage.value = "";
  formIdentity.value = "";
  try {
    const draft = await getSceneConfiguration(sceneCode);
    // 读取期间路由又切到别的对象时，旧响应不得写回表单。
    if (sequence !== loadSequence) return;
    fields.value = draft.fields;
    materials.value = draft.materials;
    rules.value = draft.rules;
    if (!isCreating) {
      const rule = draft.rules.find((r) => r.rule_key === key);
      if (!rule) throw new Error("规则不存在或已删除");
      applyRule(rule);
    }
    formIdentity.value = identityOf(sceneCode, key);
    loadedFormJson.value = JSON.stringify(form);
  } catch (error) {
    if (sequence !== loadSequence) return;
    errorMessage.value = error instanceof Error ? error.message : "读取规则失败";
  } finally {
    if (sequence === loadSequence) loading.value = false;
  }
}

async function back() {
  await router.push({ name: "scene-detail", params: { scenarioCode: scenarioCode.value },
    query: { tab: "rules" } });
}

async function submit() {
  if (!form.name.trim()) { ElMessage.warning("请填写规则名称"); return; }
  const sceneCode = scenarioCode.value;
  const key = ruleKey.value;
  const creatingRule = key === "new";
  // 表单内容必须对应现在要写入的对象：路由已切到别的规则时拒绝保存，而不是把旧内容写进新规则。
  if (formIdentity.value !== identityOf(sceneCode, key)) {
    ElMessage.warning("页面正在切换规则，请等待当前规则加载完成后再保存");
    return;
  }
  saving.value = true;
  try {
  const parameters: Record<string, JsonValue> = { ...(existing.value?.parameters ?? {}) };
  const payload: DraftRulePayload = {
    name: form.name.trim(), source_ref: form.source_ref.trim() || null,
    node: "RULE_EXECUTION", operator: existing.value?.operator ?? "LLM_EXECUTE", execution_mode: form.execution_mode, rule_role: form.rule_role,
    enabled: form.config_status === "CLEAR", config_status: form.config_status,
    inputs: form.field_keys.map((fieldKey) => {
      const field = fields.value.find((f) => f.field_key === fieldKey);
      if (!field) throw new Error("所选字段不存在");
      return { field_key: fieldKey, material_key: field.material_key };
    }), outputs: form.output_keys, materials: form.material_keys, parameters,
    requirement: form.requirement, applicability: existing.value?.applicability ?? { kind: "ALWAYS" },
    source_rule_key: form.source_rule_keys[0] ?? null, source_rule_keys: form.source_rule_keys,
    source_rule_codes: existing.value?.source_rule_codes ?? [], check_level: existing.value?.check_level ?? null,
    risk_condition: existing.value?.risk_condition ?? null,
  };
    if (creatingRule) await createDraftRule(sceneCode, payload);
    else await updateDraftRule(sceneCode, key, payload);
    loadedFormJson.value = JSON.stringify(form);
    ElMessage.success("规则已保存到草稿");
    await back();
  } catch (error) { ElMessage.error(error instanceof Error ? error.message : "保存失败"); }
  finally { saving.value = false; }
}

// 场景码与规则键来自 URL：路由复用同一组件时必须重新加载，不能沿用上一条规则的表单。
watch([scenarioCode, ruleKey], (_current, previous) => {
  if (previous !== undefined) resetForm();
  void load();
}, { immediate: true });

/** 有未保存输入时才提示离开；同一对象的查询参数切换不拦。 */
const hasUnsavedInput = computed(() => (
  formIdentity.value !== "" && JSON.stringify(form) !== loadedFormJson.value
));

async function confirmLeave(): Promise<boolean> {
  if (!hasUnsavedInput.value) return true;
  try {
    await ElMessageBox.confirm("当前规则有未保存的修改，离开后将丢失。", "未保存的修改", {
      type: "warning", confirmButtonText: "仍要离开", cancelButtonText: "留在本页",
    });
    return true;
  } catch {
    return false;
  }
}

function warnBeforeUnload(event: BeforeUnloadEvent): void {
  if (!hasUnsavedInput.value) return;
  event.preventDefault();
  event.returnValue = "";
}

onMounted(() => window.addEventListener("beforeunload", warnBeforeUnload));
onUnmounted(() => window.removeEventListener("beforeunload", warnBeforeUnload));
onBeforeRouteLeave(() => confirmLeave());
onBeforeRouteUpdate((to, from) => (
  to.params.scenarioCode === from.params.scenarioCode && to.params.ruleKey === from.params.ruleKey
    ? true
    : confirmLeave()
));
</script>

<template>
  <div class="rule-editor">
    <PageHeader :title="creating ? '新增规则' : '编辑规则'" :show-refresh="false" class="rule-editor__header">
      <template #leading><BackButton @click="back()" /></template>
      <template #actions>
        <span v-if="hasUnsavedInput" class="rule-editor__unsaved" role="status">有未保存修改</span>
        <el-button type="primary" :loading="saving" :disabled="loading || !!errorMessage" @click="submit">保存到草稿</el-button>
      </template>
    </PageHeader>
    <el-alert v-if="errorMessage" type="error" :title="errorMessage" :closable="false" show-icon />
    <el-form v-else v-loading="loading" label-position="top" :disabled="loading || saving" class="rule-form" @submit.prevent>
      <section class="rule-section" aria-labelledby="rule-content-title">
        <h2 id="rule-content-title" class="rule-section__title">规则内容</h2>
        <div class="rule-identity">
          <el-form-item label="规则名称" required>
            <el-input v-model="form.name" placeholder="请输入规则名称" />
          </el-form-item>
          <el-form-item label="业务编号">
            <el-input v-model="form.source_ref" placeholder="原文未提供时可留空" />
          </el-form-item>
        </div>
        <div class="rule-settings">
          <el-form-item label="配置状态">
            <el-select v-model="form.config_status">
              <el-option value="CLEAR" label="启用" />
              <el-option value="PENDING" label="待确认" />
              <el-option value="DISABLED" label="停用" />
            </el-select>
          </el-form-item>
          <el-form-item label="规则用途">
            <el-select v-model="form.rule_role">
              <el-option value="CHECK" label="普通核查" />
              <el-option value="RISK" label="风险结论" />
            </el-select>
          </el-form-item>
          <el-form-item label="执行方式" required>
            <el-select v-model="form.execution_mode" :aria-describedby="form.execution_mode === 'UNKNOWN' ? 'execution-mode-hint' : undefined">
              <el-option v-for="item in EXECUTION_MODE_OPTIONS" :key="item.value" :value="item.value" :label="item.label" />
            </el-select>
          </el-form-item>
        </div>
        <el-alert v-if="form.execution_mode === 'UNKNOWN'" id="execution-mode-hint" class="execution-mode-warning" type="warning" show-icon :closable="false" title="执行方式未判定，可保存草稿，发布前需明确。" />
        <el-form-item label="执行要求" class="rule-requirement">
          <el-input v-model="form.requirement" type="textarea" :rows="8" resize="vertical" placeholder="填写业务规则、判断条件、方法或公式，以及输出要求" />
        </el-form-item>
      </section>

      <section class="rule-section" aria-labelledby="rule-relations-title">
        <h2 id="rule-relations-title" class="rule-section__title">关联配置</h2>
        <div class="rule-relations">
          <el-form-item label="适用材料">
            <el-select v-model="form.material_keys" multiple filterable placeholder="搜索并选择材料">
              <el-option v-for="material in materials" :key="material.material_key" :value="material.material_key" :label="material.name" />
            </el-select>
          </el-form-item>
          <el-form-item label="输入字段">
            <el-select v-model="form.field_keys" multiple filterable placeholder="搜索材料或字段名称">
              <el-option v-for="field in fields" :key="field.field_key" :value="field.field_key" :label="`${field.material_name ?? '未知材料'} · ${field.name}`" />
            </el-select>
          </el-form-item>
          <el-form-item label="输出字段">
            <el-select v-model="form.output_keys" multiple filterable placeholder="搜索材料或字段名称">
              <el-option v-for="field in fields" :key="field.field_key" :value="field.field_key" :label="`${field.material_name ?? '未知材料'} · ${field.name}`" />
            </el-select>
          </el-form-item>
          <el-form-item label="依赖规则">
            <el-select v-model="form.source_rule_keys" multiple filterable placeholder="搜索并选择规则">
              <el-option v-for="rule in rules.filter((r) => r.rule_key !== ruleKey)" :key="rule.rule_key" :value="rule.rule_key" :label="rule.name" />
            </el-select>
          </el-form-item>
        </div>
      </section>
    </el-form>
  </div>
</template>

<style scoped>
.rule-editor {
  width: 100%;
  max-width: 1440px;
  margin: 0 auto;
  container-type: inline-size;
}
.rule-editor__header {
  position: sticky;
  top: 0;
  z-index: 10;
  padding: 4px 0 12px;
  background: var(--app-page-bg);
}
.rule-editor__unsaved { color: var(--el-text-color-regular); font-size: 13px; }
.rule-form {
  display: grid;
  grid-template-columns: minmax(0, 1.6fr) minmax(320px, 1fr);
  align-items: start;
  gap: 20px;
}
.rule-section {
  min-width: 0;
  padding: 20px;
  background: var(--el-bg-color);
  border: 1px solid var(--el-border-color-light);
  border-radius: var(--el-border-radius-base);
}
.rule-section__title {
  margin: 0 0 20px;
  font-size: 16px;
  font-weight: 600;
  line-height: 24px;
}
.rule-identity, .rule-settings, .rule-relations { display: grid; gap: 16px; }
.rule-identity { grid-template-columns: minmax(0, 1.6fr) minmax(0, 1fr); }
.rule-settings { grid-template-columns: repeat(3, minmax(0, 1fr)); margin-top: 20px; }
.rule-form :deep(.el-form-item) { min-width: 0; margin-bottom: 0; }
.rule-form :deep(.el-form-item__label) { margin-bottom: 8px; line-height: 20px; }
.rule-form :deep(.el-select) { width: 100%; min-width: 0; }
.rule-form :deep(.el-textarea__inner) { line-height: 1.75; }
.rule-form .rule-requirement { margin-top: 20px; }
.execution-mode-warning { margin-top: 16px; }
@container (max-width: 1040px) {
  .rule-form { grid-template-columns: minmax(0, 1fr); }
  .rule-relations { grid-template-columns: repeat(2, minmax(0, 1fr)); }
}
@container (max-width: 640px) {
  .rule-identity, .rule-settings, .rule-relations { grid-template-columns: minmax(0, 1fr); }
  .rule-section { padding: 16px; }
}
</style>
