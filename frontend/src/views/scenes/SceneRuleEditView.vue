<script setup lang="ts">
import { computed, onMounted, onUnmounted, reactive, ref, watch } from "vue";
import { onBeforeRouteLeave, onBeforeRouteUpdate, useRoute, useRouter } from "vue-router";
import { ElMessage, ElMessageBox } from "element-plus";
import type { DraftFieldItem, DraftMaterialItem, DraftRuleItem, JsonValue } from "@/api/contracts";
import { createDraftRule, getSceneConfiguration, updateDraftRule, type DraftRulePayload } from "@/api/scenes";
import BackButton from "@/components/BackButton.vue";
import { EXECUTION_MODE_OPTIONS, executionModeLabel, type ExecutionMode } from "@/constants/executionModes";

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
const form = reactive({ name: "", source_ref: "", requirement: "", enabled: true,
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
  form.enabled = true;
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
  form.enabled = rule.enabled;
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
    node: "RULE_EXECUTION", operator: existing.value?.operator ?? "LLM_EXECUTE", execution_mode: form.execution_mode, rule_role: form.rule_role, enabled: form.enabled,
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
  <div class="page-shell">
    <BackButton @click="back()" />
    <h2>{{ creating ? '新增规则' : '编辑规则' }}</h2>
    <el-alert v-if="errorMessage" type="error" :title="errorMessage" :closable="false" />
    <el-form v-else v-loading="loading" label-width="110px">
      <el-form-item label="名称"><el-input v-model="form.name" /></el-form-item>
      <el-form-item label="业务编号"><el-input v-model="form.source_ref" placeholder="原规则库未提供编号时可留空" /></el-form-item>
      <el-form-item label="启用状态"><el-switch v-model="form.enabled" inline-prompt active-text="启用" inactive-text="停用" /></el-form-item>
      <el-form-item label="执行方式" required>
        <el-select v-model="form.execution_mode" style="width: 260px">
          <el-option v-for="item in EXECUTION_MODE_OPTIONS" :key="item.value" :value="item.value" :label="item.label" />
        </el-select>
        <div v-if="form.execution_mode === 'UNKNOWN'" class="text-hint execution-mode-warning">
          未判定执行方式时不能发布；请按原规则行选择材料检查、字段比较、跨范围比较、组合汇总或风险推导。
        </div>
        <div v-else class="text-hint">{{ executionModeLabel(form.execution_mode) }}</div>
      </el-form-item>
      <el-form-item label="规则角色"><el-select v-model="form.rule_role"><el-option value="CHECK" label="普通核查" /><el-option value="RISK" label="风险结论" /></el-select></el-form-item>
      <el-form-item label="执行要求"><el-input v-model="form.requirement" type="textarea" :rows="8" placeholder="填写完整业务规则、方法、公式、条件和输出要求" /></el-form-item>
      <el-form-item label="适用材料"><el-select v-model="form.material_keys" multiple style="width:100%">
        <el-option v-for="material in materials" :key="material.material_key" :value="material.material_key" :label="material.name" />
      </el-select></el-form-item>
      <el-form-item label="输入字段"><el-select v-model="form.field_keys" multiple style="width:100%">
        <el-option v-for="field in fields" :key="field.field_key" :value="field.field_key" :label="field.name" />
      </el-select></el-form-item>
      <el-form-item label="输出字段"><el-select v-model="form.output_keys" multiple style="width:100%">
        <el-option v-for="field in fields" :key="field.field_key" :value="field.field_key" :label="field.name" />
      </el-select></el-form-item>
      <el-form-item label="依赖规则"><el-select v-model="form.source_rule_keys" multiple style="width:100%">
        <el-option v-for="rule in rules.filter((r) => r.rule_key !== ruleKey)" :key="rule.rule_key" :value="rule.rule_key" :label="rule.name" />
      </el-select></el-form-item>
      <el-form-item><el-button type="primary" :loading="saving" :disabled="loading" @click="submit">保存到草稿</el-button></el-form-item>
    </el-form>
  </div>
</template>

<style scoped>
.execution-mode-warning {
  margin-top: 4px;
  color: var(--el-color-warning);
}
</style>
