<script setup lang="ts">
import { computed, ref, watch } from "vue";
import { ElMessage } from "element-plus";

import type { DraftCalculationItem, DraftFieldItem, JsonValue } from "@/api/contracts";
import { createDraftCalculation, updateDraftCalculation, type DraftCalculationPayload } from "@/api/scenes";
import { valueTypeOptions } from "@/constants/labels";

const props = defineProps<{
  modelValue: boolean;
  scenarioCode: string;
  item: DraftCalculationItem | null;
  items: DraftCalculationItem[];
  fields: DraftFieldItem[];
}>();
const emit = defineEmits<{ "update:modelValue": [value: boolean]; saved: [] }>();

function emptyForm(): DraftCalculationPayload {
  return {
    rule_code: "", rule_name: "", result_code: "", source_id: null,
    formula: "", requirement: "", result_type: "DECIMAL", unit: null, currency: null,
    period_policy: null, statement_scope: null, status: "CLEAR", parameters: [],
    depends_on_result_codes: [], business_context: {}, remark: "",
  };
}

const form = ref<DraftCalculationPayload>(emptyForm());
const saving = ref(false);
const error = ref("");
const businessText = ref("{}");
const constantTexts = ref<string[]>([]);
const otherCalculations = computed(() => props.items.filter((item) => item.rule_code !== props.item?.rule_code));
const fieldOptions = computed(() => props.fields.map((field) => ({
  value: field.field_key, label: `${field.material_name} · ${field.name}`,
})));

watch(() => props.modelValue, (open) => {
  if (!open) return;
  error.value = "";
  const raw = props.item ? JSON.parse(JSON.stringify(props.item.definition)) as Partial<DraftCalculationPayload> : {};
  form.value = { ...emptyForm(), ...raw };
  if (props.item) {
    form.value.rule_code = props.item.rule_code ?? "";
    form.value.rule_name = raw.rule_name ?? props.item.name ?? "";
    const status = props.item.definition.config_status ?? raw.status;
    form.value.status = status === "PENDING" ? "PENDING"
      : status === "DISABLED" || props.item.definition.enabled === false ? "DISABLED" : "CLEAR";
  }
  businessText.value = JSON.stringify(form.value.business_context, null, 2);
  constantTexts.value = form.value.parameters.map((parameter) => JSON.stringify(parameter.constant_value ?? null));
});

function addParameter(): void {
  form.value.parameters.push({ parameter_code: "", source_type: "FIELD", source_code: null });
  constantTexts.value.push("null");
}

function removeParameter(index: number): void {
  form.value.parameters.splice(index, 1);
  constantTexts.value.splice(index, 1);
}

async function save(): Promise<void> {
  error.value = "";
  saving.value = true;
  try {
    const payload = JSON.parse(JSON.stringify(form.value)) as DraftCalculationPayload;
    if (!payload.rule_code.trim() || !payload.rule_name.trim() || !payload.result_code.trim()) {
      throw new Error("请填写规则编号、计算名称和结果标识");
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
    const business: unknown = JSON.parse(businessText.value);
    if (!business || typeof business !== "object" || Array.isArray(business)) throw new Error("业务补充必须是 JSON 对象");
    payload.business_context = business as Record<string, JsonValue>;
    if (props.item) {
      if (!props.item.rule_code) throw new Error("计算规则缺少编号，无法保存");
      await updateDraftCalculation(props.scenarioCode, props.item.rule_code, payload);
    } else {
      await createDraftCalculation(props.scenarioCode, payload);
    }
    ElMessage.success("计算规则已保存到草稿");
    emit("update:modelValue", false);
    emit("saved");
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : String(cause);
  } finally {
    saving.value = false;
  }
}
</script>

<template>
  <el-dialog :model-value="modelValue" :title="item ? '编辑计算规则' : '新增计算规则'"
    width="min(880px, 96vw)" :close-on-click-modal="false" :close-on-press-escape="!saving" :show-close="!saving"
    @update:model-value="emit('update:modelValue', $event)">
    <el-alert v-if="error" :title="error" type="error" :closable="false" show-icon class="block-gap-sm" />
    <el-form label-width="110px" :disabled="saving">
      <el-form-item label="规则编号" required><el-input v-model="form.rule_code" :disabled="Boolean(item)" maxlength="80" /></el-form-item>
      <el-form-item label="计算名称" required><el-input v-model="form.rule_name" maxlength="255" /></el-form-item>
      <el-form-item label="业务编号"><el-input v-model="form.source_id" /></el-form-item>
      <el-form-item label="结果标识" required><el-input v-model="form.result_code" maxlength="80" /></el-form-item>
      <el-form-item label="启用状态"><el-select v-model="form.status"><el-option value="CLEAR" label="启用" /><el-option value="PENDING" label="待确认" /><el-option value="DISABLED" label="停用" /></el-select></el-form-item>
      <el-form-item label="计算方法"><el-input v-model="form.formula" type="textarea" :rows="4" /></el-form-item>
      <el-form-item label="业务要求"><el-input v-model="form.requirement" type="textarea" :rows="4" /></el-form-item>
      <el-form-item label="结果类型"><el-select v-model="form.result_type" filterable allow-create><el-option v-for="option in valueTypeOptions" :key="option.value" :value="option.value" :label="option.label" /></el-select></el-form-item>
      <el-form-item label="单位"><el-input v-model="form.unit" /></el-form-item>
      <el-form-item label="币种"><el-input v-model="form.currency" /></el-form-item>
      <el-form-item label="期间要求"><el-input v-model="form.period_policy" /></el-form-item>
      <el-form-item label="报表口径"><el-input v-model="form.statement_scope" /></el-form-item>
      <el-form-item label="依赖计算"><el-select v-model="form.depends_on_result_codes" multiple filterable allow-create class="full-width"><el-option v-for="entry in otherCalculations" :key="entry.rule_code ?? entry.result_code ?? ''" :value="entry.result_code ?? ''" :label="entry.name ?? entry.result_code ?? ''" /></el-select></el-form-item>
      <el-form-item label="计算参数">
        <div class="full-width">
          <el-table :data="form.parameters" border>
            <el-table-column label="参数名称" min-width="130"><template #default="{ row }"><el-input v-model="row.parameter_code" /></template></el-table-column>
            <el-table-column label="来源" width="140"><template #default="{ row }"><el-select v-model="row.source_type" @change="row.source_code = null"><el-option value="FIELD" label="字段" /><el-option value="CALC_RESULT" label="计算结果" /><el-option value="CONSTANT" label="常量" /></el-select></template></el-table-column>
            <el-table-column label="取值" min-width="230"><template #default="{ row, $index }">
              <el-input v-if="row.source_type === 'CONSTANT'" v-model="constantTexts[$index]" placeholder="JSON 值，如 100 或 true" />
              <el-select v-else-if="row.source_type === 'FIELD'" v-model="row.source_code" filterable allow-create class="full-width"><el-option v-for="option in fieldOptions" :key="option.value" :value="option.value" :label="option.label" /></el-select>
              <el-select v-else-if="row.source_type === 'CALC_RESULT'" v-model="row.source_code" filterable allow-create class="full-width"><el-option v-for="entry in otherCalculations" :key="entry.rule_code ?? entry.result_code ?? ''" :value="entry.result_code ?? ''" :label="entry.name ?? entry.result_code ?? ''" /></el-select>
              <el-input v-else v-model="row.source_code" />
            </template></el-table-column>
            <el-table-column label="操作" width="65"><template #default="{ $index }"><el-button link type="danger" @click="removeParameter($index)">移除</el-button></template></el-table-column>
          </el-table>
          <el-button class="parameter-add" @click="addParameter">添加参数</el-button>
        </div>
      </el-form-item>
      <el-form-item label="备注"><el-input v-model="form.remark" type="textarea" :rows="2" /></el-form-item>
      <el-collapse>
        <el-collapse-item title="业务补充" name="business"><el-input v-model="businessText" type="textarea" :rows="6" /></el-collapse-item>
      </el-collapse>
    </el-form>
    <template #footer>
      <el-button :disabled="saving" @click="emit('update:modelValue', false)">取消</el-button>
      <el-button type="primary" :loading="saving" @click="save">保存到草稿</el-button>
    </template>
  </el-dialog>
</template>

<style scoped>
.full-width { width: 100%; }
.parameter-add { margin-top: 8px; }
</style>
