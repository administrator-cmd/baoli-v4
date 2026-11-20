<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from "vue";
import { onBeforeRouteLeave, onBeforeRouteUpdate, useRoute, useRouter } from "vue-router";
import { ElMessage, ElMessageBox } from "element-plus";

import type { DraftCalculationItem, DraftFieldItem, JsonValue } from "@/api/contracts";
import { createDraftCalculation, getSceneConfiguration, updateDraftCalculation, type DraftCalculationPayload } from "@/api/scenes";
import BackButton from "@/components/BackButton.vue";
import PageHeader from "@/components/PageHeader.vue";
import { valueTypeOptions } from "@/constants/labels";

const route = useRoute();
const router = useRouter();
const scenarioCode = computed(() => String(route.params.scenarioCode));
const ruleCode = computed(() => String(route.params.ruleCode ?? ""));
const creating = computed(() => route.name === "scene-calculation-new");
const identity = computed(() => JSON.stringify([scenarioCode.value, ruleCode.value, creating.value]));
const items = ref<DraftCalculationItem[]>([]);
const fields = ref<DraftFieldItem[]>([]);

function emptyForm(): DraftCalculationPayload {
  return {
    rule_code: "", rule_name: "", result_code: "", source_id: null,
    formula: "", requirement: "", result_type: "DECIMAL", unit: null, currency: null,
    period_policy: null, statement_scope: null, status: "CLEAR", parameters: [],
    depends_on_result_codes: [], business_context: {}, remark: "",
  };
}

const form = ref<DraftCalculationPayload>(emptyForm());
const loading = ref(false);
const saving = ref(false);
const loadError = ref("");
const error = ref("");
const constantTexts = ref<string[]>([]);
const formIdentity = ref("");
const loadedFormJson = ref("");
const formJson = computed(() => JSON.stringify([form.value, constantTexts.value]));
const hasUnsavedInput = computed(() => formIdentity.value !== "" && formJson.value !== loadedFormJson.value);
let loadSequence = 0;
const otherCalculations = computed(() => items.value.filter((item) => creating.value || item.rule_code !== ruleCode.value));
const fieldOptions = computed(() => fields.value.map((field) => ({
  value: field.field_key, label: `${field.material_name} · ${field.name}`,
})));

async function load(): Promise<void> {
  const sequence = ++loadSequence;
  const currentIdentity = identity.value;
  const code = ruleCode.value;
  const isCreating = creating.value;
  loading.value = true;
  loadError.value = "";
  error.value = "";
  formIdentity.value = "";
  form.value = emptyForm();
  constantTexts.value = [];
  items.value = [];
  fields.value = [];
  try {
    const draft = await getSceneConfiguration(scenarioCode.value);
    if (sequence !== loadSequence) return;
    items.value = draft.calculations;
    fields.value = draft.fields;
    if (!isCreating) {
      const item = draft.calculations.find((entry) => entry.rule_code === code);
      if (!item) throw new Error("计算规则不存在或已删除");
      const raw = JSON.parse(JSON.stringify(item.definition)) as Partial<DraftCalculationPayload>;
      form.value = { ...emptyForm(), ...raw };
      form.value.rule_code = code;
      form.value.rule_name = raw.rule_name ?? item.name ?? "";
      const status = item.definition.config_status ?? raw.status;
      form.value.status = status === "PENDING" ? "PENDING"
        : status === "DISABLED" || item.definition.enabled === false ? "DISABLED" : "CLEAR";
    }
    constantTexts.value = form.value.parameters.map((parameter) => JSON.stringify(parameter.constant_value ?? null));
    formIdentity.value = currentIdentity;
    loadedFormJson.value = formJson.value;
  } catch (cause) {
    if (sequence !== loadSequence) return;
    loadError.value = cause instanceof Error ? cause.message : "读取计算规则失败";
  } finally {
    if (sequence === loadSequence) loading.value = false;
  }
}

watch(identity, () => void load(), { immediate: true });

async function back(): Promise<void> {
  await router.push({ name: "scene-detail", params: { scenarioCode: scenarioCode.value }, query: { tab: "calculations" } });
}

async function confirmLeave(): Promise<boolean> {
  if (!hasUnsavedInput.value) return true;
  try {
    await ElMessageBox.confirm("当前计算规则有未保存的修改，离开后将丢失。", "未保存的修改", {
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
  to.name === from.name && to.params.scenarioCode === from.params.scenarioCode && to.params.ruleCode === from.params.ruleCode
    ? true : confirmLeave()
));

function addParameter(): void {
  form.value.parameters.push({ parameter_code: "", source_type: "FIELD", source_code: null });
  constantTexts.value.push("null");
}

function removeParameter(index: number): void {
  form.value.parameters.splice(index, 1);
  constantTexts.value.splice(index, 1);
}

async function save(): Promise<void> {
  if (formIdentity.value !== identity.value) {
    ElMessage.warning("计算规则正在加载，请加载完成后再保存");
    return;
  }
  const currentIdentity = formIdentity.value;
  const sceneCode = scenarioCode.value;
  const code = ruleCode.value;
  const isCreating = creating.value;
  const savedFormJson = formJson.value;
  error.value = "";
  saving.value = true;
  try {
    const payload = JSON.parse(JSON.stringify(form.value)) as DraftCalculationPayload;
    if (!payload.rule_name.trim() || !payload.result_code.trim()) {
      throw new Error("请填写计算名称和结果标识");
    }
    payload.parameters.forEach((parameter, index) => {
      if (!parameter.parameter_code.trim()) throw new Error(`请填写第 ${index + 1} 个参数的名称`);
      if (parameter.source_type === "CONSTANT") {
        try { parameter.constant_value = JSON.parse(constantTexts.value[index]) as JsonValue; }
        catch { throw new Error(`第 ${index + 1} 个参数的常量必须是合法 JSON 值`); }
        parameter.source_code = null;
      } else if (!parameter.source_code) {
        throw new Error(`请选择第 ${index + 1} 个参数的来源`);
      }
    });
    if (isCreating) await createDraftCalculation(sceneCode, payload);
    else await updateDraftCalculation(sceneCode, code, payload);
    if (currentIdentity !== identity.value) return;
    loadedFormJson.value = savedFormJson;
    ElMessage.success("计算规则已保存到草稿");
    await back();
  } catch (cause) {
    if (currentIdentity !== identity.value) return;
    error.value = cause instanceof Error ? cause.message : String(cause);
  } finally {
    saving.value = false;
  }
}
</script>

<template>
  <div class="calculation-editor">
    <PageHeader :title="creating ? '新增计算规则' : '编辑计算规则'" :show-refresh="false" class="calculation-editor__header">
      <template #leading><BackButton @click="back()" /></template>
      <template #actions>
        <span v-if="hasUnsavedInput" class="calculation-editor__unsaved" role="status">有未保存修改</span>
        <el-button type="primary" :loading="saving" :disabled="loading || !!loadError" @click="save">保存到草稿</el-button>
      </template>
    </PageHeader>
    <el-alert v-if="loadError" :title="loadError" type="error" :closable="false" show-icon />
    <template v-else>
      <el-alert v-if="error" :title="error" type="error" :closable="false" show-icon class="calculation-error" />
      <el-form v-loading="loading" label-position="top" :disabled="loading || saving" class="calculation-form" @submit.prevent>
        <div class="calculation-overview">
          <section class="calculation-section" aria-labelledby="calculation-content-title">
            <h3 id="calculation-content-title" class="calculation-section__title">计算内容</h3>
            <div class="calculation-name-row">
              <el-form-item label="计算名称" required>
                <el-input v-model="form.rule_name" maxlength="255" placeholder="请输入计算名称" />
              </el-form-item>
              <el-form-item label="配置状态">
                <el-select v-model="form.status">
                  <el-option value="CLEAR" label="启用" />
                  <el-option value="PENDING" label="待确认" />
                  <el-option value="DISABLED" label="停用" />
                </el-select>
              </el-form-item>
            </div>
            <div class="calculation-grid">
              <el-form-item label="规则编号">
                <el-input :model-value="creating ? '保存时自动生成' : form.rule_code" readonly />
              </el-form-item>
              <el-form-item label="业务编号">
                <el-input v-model="form.source_id" placeholder="原文未提供时可留空" />
              </el-form-item>
            </div>
            <el-form-item label="计算方法">
              <el-input v-model="form.formula" type="textarea" :rows="3" resize="vertical" placeholder="填写计算公式或计算步骤" />
            </el-form-item>
            <el-form-item label="业务要求">
              <el-input v-model="form.requirement" type="textarea" :rows="3" resize="vertical" placeholder="填写计算条件、业务口径与结果要求" />
            </el-form-item>
          </section>

          <section class="calculation-section calculation-results" aria-labelledby="calculation-results-title">
            <h3 id="calculation-results-title" class="calculation-section__title">结果设置</h3>
            <div class="calculation-grid">
              <el-form-item label="结果标识" required>
                <el-input v-model="form.result_code" maxlength="80" placeholder="供其他规则引用" />
              </el-form-item>
              <el-form-item label="结果类型">
                <el-select v-model="form.result_type" filterable allow-create>
                  <el-option v-for="option in valueTypeOptions" :key="option.value" :value="option.value" :label="option.label" />
                </el-select>
              </el-form-item>
              <el-form-item label="单位"><el-input v-model="form.unit" /></el-form-item>
              <el-form-item label="币种"><el-input v-model="form.currency" /></el-form-item>
              <el-form-item label="期间要求"><el-input v-model="form.period_policy" /></el-form-item>
              <el-form-item label="报表口径"><el-input v-model="form.statement_scope" /></el-form-item>
            </div>
            <el-form-item label="依赖计算">
              <el-select v-model="form.depends_on_result_codes" multiple filterable allow-create placeholder="搜索并选择依赖的计算结果">
                <el-option v-for="entry in otherCalculations" :key="entry.rule_code ?? entry.result_code ?? ''" :value="entry.result_code ?? ''" :label="entry.name ?? entry.result_code ?? ''" />
              </el-select>
            </el-form-item>
          </section>
        </div>

        <section class="calculation-section calculation-parameters" aria-labelledby="calculation-parameters-title">
          <div class="calculation-section__header">
            <h3 id="calculation-parameters-title" class="calculation-section__title">计算参数</h3>
            <el-button size="small" type="primary" plain @click="addParameter">添加参数</el-button>
          </div>
          <div class="calculation-parameters__scroll">
            <el-table :data="form.parameters" border class="calculation-parameters__table" empty-text="暂无计算参数">
              <el-table-column label="参数名称" min-width="180">
                <template #default="{ row, $index }"><el-input v-model="row.parameter_code" :aria-label="`第 ${$index + 1} 个参数名称`" placeholder="参数名称" /></template>
              </el-table-column>
              <el-table-column label="来源" width="150">
                <template #default="{ row, $index }">
                  <el-select v-model="row.source_type" :aria-label="`第 ${$index + 1} 个参数来源`" @change="row.source_code = null">
                    <el-option value="FIELD" label="字段" /><el-option value="CALC_RESULT" label="计算结果" /><el-option value="CONSTANT" label="常量" />
                  </el-select>
                </template>
              </el-table-column>
              <el-table-column label="取值" min-width="310">
                <template #default="{ row, $index }">
                  <el-input v-if="row.source_type === 'CONSTANT'" v-model="constantTexts[$index]" :aria-label="`第 ${$index + 1} 个参数常量`" placeholder="JSON 值，如 100 或 true" />
                  <el-select v-else-if="row.source_type === 'FIELD'" v-model="row.source_code" filterable allow-create :aria-label="`第 ${$index + 1} 个参数字段`" placeholder="搜索材料或字段名称">
                    <el-option v-for="option in fieldOptions" :key="option.value" :value="option.value" :label="option.label" />
                  </el-select>
                  <el-select v-else-if="row.source_type === 'CALC_RESULT'" v-model="row.source_code" filterable allow-create :aria-label="`第 ${$index + 1} 个参数计算结果`" placeholder="搜索计算结果">
                    <el-option v-for="entry in otherCalculations" :key="entry.rule_code ?? entry.result_code ?? ''" :value="entry.result_code ?? ''" :label="entry.name ?? entry.result_code ?? ''" />
                  </el-select>
                  <el-input v-else v-model="row.source_code" :aria-label="`第 ${$index + 1} 个参数取值`" />
                </template>
              </el-table-column>
              <el-table-column label="操作" width="76">
                <template #default="{ $index }"><el-button link type="danger" :aria-label="`移除第 ${$index + 1} 个参数`" @click="removeParameter($index)">移除</el-button></template>
              </el-table-column>
            </el-table>
          </div>
        </section>

        <el-form-item label="补充说明" class="calculation-section calculation-remark">
          <el-input v-model="form.remark" type="textarea" :rows="2" resize="vertical" placeholder="填写计算方法与业务要求之外的说明（选填）" />
        </el-form-item>
      </el-form>
    </template>
  </div>
</template>

<style scoped>
.calculation-error { margin-bottom: 16px; }
.calculation-editor {
  width: 100%;
  max-width: 1440px;
  margin: 0 auto;
  container-type: inline-size;
}
.calculation-editor__header {
  position: sticky;
  top: 0;
  z-index: 10;
  padding: 4px 0 12px;
  background: var(--app-page-bg);
}
.calculation-editor__unsaved { color: var(--el-text-color-regular); font-size: 13px; }
.calculation-form { display: grid; gap: 20px; }
.calculation-overview {
  display: grid;
  grid-template-columns: minmax(0, 1.6fr) minmax(320px, 1fr);
  align-items: start;
  gap: 20px;
}
.calculation-section {
  display: grid;
  align-content: start;
  min-width: 0;
  gap: 16px;
  padding: 20px;
  background: var(--el-bg-color);
  border: 1px solid var(--el-border-color-light);
  border-radius: var(--el-border-radius-base);
}
.calculation-section__title { margin: 0; font-size: 16px; font-weight: 600; line-height: 24px; color: var(--el-text-color-primary); }
.calculation-name-row, .calculation-grid { display: grid; gap: 16px; }
.calculation-name-row { grid-template-columns: minmax(0, 1fr) 180px; }
.calculation-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
.calculation-form :deep(.el-form-item) { min-width: 0; margin-bottom: 0; }
.calculation-form :deep(.el-form-item__label) { margin-bottom: 8px; line-height: 20px; }
.calculation-form :deep(.el-select) { width: 100%; min-width: 0; }
.calculation-form :deep(.el-textarea__inner) { line-height: 1.75; }
.calculation-section__header { display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between; gap: 12px; }
.calculation-parameters__scroll { overflow-x: auto; }
.calculation-parameters__table { min-width: 716px; }
.calculation-form .calculation-remark { display: block; }
@container (max-width: 1040px) {
  .calculation-overview { grid-template-columns: minmax(0, 1fr); }
}
@container (max-width: 640px) {
  .calculation-name-row, .calculation-grid { grid-template-columns: minmax(0, 1fr); }
  .calculation-section { padding: 16px; }
}
</style>
