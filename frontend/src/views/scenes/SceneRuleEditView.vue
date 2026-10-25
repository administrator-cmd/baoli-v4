<script setup lang="ts">
import { computed, onMounted, reactive, ref } from "vue";
import { useRoute, useRouter } from "vue-router";
import { ElMessage } from "element-plus";
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
const form = reactive({ name: "", source_ref: "", requirement: "", enabled: true,
  execution_mode: "UNKNOWN" as ExecutionMode,
  rule_role: "CHECK" as "CHECK" | "RISK", field_keys: [] as string[], output_keys: [] as string[], material_keys: [] as string[], source_rule_keys: [] as string[] });

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
  loading.value = true;
  errorMessage.value = "";
  try {
    const draft = await getSceneConfiguration(scenarioCode.value);
    fields.value = draft.fields;
    materials.value = draft.materials;
    rules.value = draft.rules;
    if (!creating.value) {
      const rule = draft.rules.find((r) => r.rule_key === ruleKey.value);
      if (!rule) throw new Error("规则不存在或已删除");
      applyRule(rule);
    }
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : "读取规则失败";
  } finally { loading.value = false; }
}

async function back() {
  await router.push({ name: "scene-detail", params: { scenarioCode: scenarioCode.value },
    query: { tab: "rules" } });
}

async function submit() {
  if (!form.name.trim()) { ElMessage.warning("请填写规则名称"); return; }
  saving.value = true;
  try {
  const parameters: Record<string, JsonValue> = { ...(existing.value?.parameters ?? {}) };
  const payload: DraftRulePayload = {
    name: form.name.trim(), source_ref: form.source_ref.trim() || null,
    node: "RULE_EXECUTION", operator: existing.value?.operator ?? "LLM_EXECUTE", execution_mode: form.execution_mode, rule_role: form.rule_role, enabled: form.enabled,
    inputs: form.field_keys.map((key) => {
      const field = fields.value.find((f) => f.field_key === key);
      if (!field) throw new Error("所选字段不存在");
      return { field_key: key, material_key: field.material_key };
    }), outputs: form.output_keys, materials: form.material_keys, parameters,
    requirement: form.requirement, applicability: existing.value?.applicability ?? { kind: "ALWAYS" },
    source_rule_key: form.source_rule_keys[0] ?? null, source_rule_keys: form.source_rule_keys,
    source_rule_codes: existing.value?.source_rule_codes ?? [], check_level: existing.value?.check_level ?? null,
    risk_condition: existing.value?.risk_condition ?? null,
  };
    if (creating.value) await createDraftRule(scenarioCode.value, payload);
    else await updateDraftRule(scenarioCode.value, ruleKey.value, payload);
    ElMessage.success("规则已保存到草稿");
    await back();
  } catch (error) { ElMessage.error(error instanceof Error ? error.message : "保存失败"); }
  finally { saving.value = false; }
}
onMounted(load);
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
