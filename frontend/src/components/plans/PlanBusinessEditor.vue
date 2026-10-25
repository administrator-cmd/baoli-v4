<script setup lang="ts">
import { computed, reactive, ref, watch } from "vue";
import type { PlanCalculationRuleItem, PlanConfig, PlanRuleItem } from "@/api/contracts";
import { executionModeLabel } from "@/constants/executionModes";
import { valueTypeOptions } from "@/constants/labels";

const props = defineProps<{
  modelValue: boolean;
  config: PlanConfig;
  kind: "rule" | "calculation";
  itemKey: string;
}>();
const emit = defineEmits<{ "update:modelValue": [value: boolean] }>();
const rule = ref<PlanRuleItem | null>(null);
const calculation = ref<PlanCalculationRuleItem | null>(null);
const executionMode = computed(() => {
  const value = rule.value?.rule_json?.execution_mode;
  return typeof value === "string" ? value : null;
});
const error = ref("");
const parametersText = ref("");
const businessText = ref("");
const applicabilityText = ref("");
const constantTexts = ref<string[]>([]);
const form = reactive({
  requirement: "", role: "CHECK", status: "CLEAR", checkLevel: "", checkOperator: "",
  riskLevel: "", riskCondition: "", fieldKeys: [] as string[], outputKeys: [] as string[],
  materialKeys: [] as string[], sourceRuleKeys: [] as string[], resultCodes: [] as string[],
});

function copy<T>(value: T): T {
  return JSON.parse(JSON.stringify(value)) as T;
}

function text(value: unknown): string {
  return value == null ? "" : String(value);
}

function strings(value: unknown): string[] {
  return Array.isArray(value) ? value.map(String) : [];
}

watch(() => [props.modelValue, props.kind, props.itemKey], () => {
  if (!props.modelValue) return;
  error.value = "";
  rule.value = null;
  calculation.value = null;
  if (props.kind === "rule") {
    const item = props.config.rules.find((entry) => entry.rule_key === props.itemKey);
    if (!item) {
      error.value = "规则不存在，请关闭后刷新方案";
      return;
    }
    rule.value = copy(item);
    const definition = item.rule_json;
    form.requirement = text(definition.requirement);
    form.role = text(definition.rule_role) || "CHECK";
    form.status = item.enabled
      ? (text(definition.config_status) || "CLEAR")
      : (text(definition.config_status) === "PENDING" ? "PENDING" : "DISABLED");
    form.checkLevel = text(definition.check_level);
    form.checkOperator = text(definition.check_operator);
    form.riskLevel = text(definition.risk_level_value);
    form.riskCondition = text(definition.risk_condition);
    form.fieldKeys = Array.isArray(definition.inputs) ? definition.inputs.map((input) => String(input.field_key)) : [];
    form.outputKeys = strings(definition.outputs);
    form.materialKeys = strings(definition.materials);
    form.sourceRuleKeys = [...new Set([...strings(definition.source_rule_keys), ...(definition.source_rule_key ? [String(definition.source_rule_key)] : [])])];
    form.resultCodes = strings(definition.source_rule_codes);
    parametersText.value = JSON.stringify(definition.parameters ?? {}, null, 2);
    applicabilityText.value = JSON.stringify(definition.applicability ?? { kind: "ALWAYS" }, null, 2);
    businessText.value = JSON.stringify(definition.business_context ?? {}, null, 2);
  } else {
    const item = props.config.calculation_rules.find((entry) => entry.rule_code === props.itemKey);
    if (!item) {
      error.value = "计算规则不存在，请关闭后刷新方案";
      return;
    }
    calculation.value = copy(item);
    if (!item.enabled && item.status === "CLEAR") calculation.value.status = "DISABLED";
    constantTexts.value = item.parameters.map((parameter) => JSON.stringify(parameter.constant_value ?? null));
    businessText.value = JSON.stringify(item.business_context, null, 2);
  }
}, { immediate: true });

function parseObject(value: string, label: string): Record<string, unknown> {
  const parsed: unknown = JSON.parse(value);
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) throw new Error(`${label}必须是 JSON 对象`);
  return parsed as Record<string, unknown>;
}

function fieldName(key: string): string {
  const field = props.config.fields.find((item) => item.field_key === key);
  if (!field) return "未找到的字段";
  const material = props.config.materials.find((item) => item.material_key === field.material_key);
  return `${material?.name ?? "未知材料"} · ${field.name}`;
}

function changeRuleStatus(): void {
  if (rule.value) rule.value.enabled = form.status === "CLEAR";
}

function changeCalculationStatus(): void {
  if (calculation.value) calculation.value.enabled = calculation.value.status === "CLEAR";
}

function addParameter(): void {
  calculation.value?.parameters.push({ parameter_code: "", source_type: "FIELD", source_code: null });
  constantTexts.value.push("null");
}

function removeParameter(index: number): void {
  calculation.value?.parameters.splice(index, 1);
  constantTexts.value.splice(index, 1);
}

function apply(): void {
  error.value = "";
  try {
    const business = parseObject(businessText.value, "业务补充");
    if (rule.value) {
      const edited = rule.value;
      if (!edited.name.trim()) throw new Error("请填写规则名称");
      const previousInputs = Array.isArray(edited.rule_json.inputs) ? edited.rule_json.inputs : [];
      const inputs = form.fieldKeys.map((key) => {
        const field = props.config.fields.find((item) => item.field_key === key);
        const previous = previousInputs.find((input) => input.field_key === key);
        if (field) return { ...previous, field_key: key, material_key: field.material_key };
        if (previous) return previous;
        throw new Error("输入字段不存在");
      });
      edited.rule_json = {
        ...edited.rule_json, requirement: form.requirement, rule_role: form.role,
        config_status: form.status, check_level: form.checkLevel || null,
        check_operator: form.checkOperator || null, risk_level_value: form.riskLevel || null,
        risk_condition: form.riskCondition || null, inputs, outputs: [...form.outputKeys],
        materials: [...form.materialKeys], source_rule_keys: [...form.sourceRuleKeys],
        source_rule_key: form.sourceRuleKeys[0] ?? null, source_rule_codes: [...form.resultCodes],
        parameters: parseObject(parametersText.value, "判定参数"),
        applicability: parseObject(applicabilityText.value, "适用条件"), business_context: business,
      };
      const target = props.config.rules.find((item) => item.rule_key === props.itemKey);
      if (!target) throw new Error("规则不存在");
      Object.assign(target, edited);
    } else if (calculation.value) {
      const edited = calculation.value;
      if (!edited.rule_name.trim()) throw new Error("请填写计算名称");
      edited.parameters.forEach((parameter, index) => {
        if (!parameter.parameter_code.trim()) throw new Error(`请填写第 ${index + 1} 个参数的名称`);
        if (parameter.source_type === "CONSTANT") {
          parameter.constant_value = JSON.parse(constantTexts.value[index]);
          parameter.source_code = null;
        }
      });
      edited.depends_on_result_codes = [...new Set(edited.parameters.filter((item) => item.source_type === "CALC_RESULT" && item.source_code).map((item) => item.source_code!))];
      edited.business_context = business as PlanCalculationRuleItem["business_context"];
      const target = props.config.calculation_rules.find((item) => item.rule_code === props.itemKey);
      if (!target) throw new Error("计算规则不存在");
      const previousResultCode = target.result_code;
      if (!edited.result_code.trim()) throw new Error("请填写结果标识");
      if (edited.result_code !== previousResultCode) {
        for (const section of props.config.sections) {
          section.data_refs = section.data_refs.map((ref) => ref === previousResultCode ? edited.result_code : ref);
        }
        for (const item of props.config.rules) {
          const definition = item.rule_json;
          if (Array.isArray(definition.source_rule_codes)) {
            definition.source_rule_codes = definition.source_rule_codes.map((code) => code === previousResultCode ? edited.result_code : code);
          }
          if (Array.isArray(definition.parameters)) {
            for (const parameter of definition.parameters) {
              if (parameter.source_type === "CALC_RESULT" && parameter.source_code === previousResultCode) {
                parameter.source_code = edited.result_code;
              }
            }
          }
        }
        for (const item of props.config.calculation_rules) {
          item.depends_on_result_codes = item.depends_on_result_codes.map((code) => code === previousResultCode ? edited.result_code : code);
          for (const parameter of item.parameters) {
            if (parameter.source_type === "CALC_RESULT" && parameter.source_code === previousResultCode) {
              parameter.source_code = edited.result_code;
            }
          }
        }
      }
      Object.assign(target, edited);
    } else {
      throw new Error("未加载可编辑的业务配置");
    }
    emit("update:modelValue", false);
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : String(cause);
  }
}
</script>

<template>
  <el-dialog :model-value="modelValue" :title="kind === 'rule' ? '编辑审核规则' : '编辑计算规则'" width="min(880px, 96vw)" :close-on-click-modal="false" @update:model-value="emit('update:modelValue', $event)">
    <el-alert v-if="error" :title="error" type="error" show-icon :closable="false" class="block-gap-sm" />
    <el-form label-width="110px">
      <template v-if="rule">
        <el-form-item label="规则名称" required><el-input v-model="rule.name" maxlength="255" /></el-form-item>
        <el-form-item label="业务编号"><el-input v-model="rule.source_ref" maxlength="80" /></el-form-item>
        <el-form-item label="规则类型"><el-input v-model="rule.operator_code" /></el-form-item>
        <el-form-item label="执行方式"><span class="text-muted">{{ executionModeLabel(executionMode) }}</span></el-form-item>
        <el-form-item label="规则用途"><el-select v-model="form.role"><el-option value="CHECK" label="普通核查" /><el-option value="RISK" label="风险结论" /></el-select></el-form-item>
        <el-form-item label="状态"><el-select v-model="form.status" @change="changeRuleStatus"><el-option value="CLEAR" label="启用" /><el-option value="PENDING" label="待确认" /><el-option value="DISABLED" label="暂停" /></el-select></el-form-item>
        <el-form-item label="审核要求"><el-input v-model="form.requirement" type="textarea" :rows="5" /></el-form-item>
        <el-form-item label="适用材料"><el-select v-model="form.materialKeys" multiple filterable><el-option v-for="item in config.materials" :key="item.material_key" :value="item.material_key" :label="item.name" /></el-select></el-form-item>
        <el-form-item label="输入字段"><el-select v-model="form.fieldKeys" multiple filterable><el-option v-for="item in config.fields" :key="item.field_key" :value="item.field_key" :label="fieldName(item.field_key)" /></el-select></el-form-item>
        <el-form-item label="产出字段"><el-select v-model="form.outputKeys" multiple filterable><el-option v-for="item in config.fields" :key="item.field_key" :value="item.field_key" :label="fieldName(item.field_key)" /></el-select></el-form-item>
        <el-form-item label="依赖规则"><el-select v-model="form.sourceRuleKeys" multiple filterable><el-option v-for="item in config.rules.filter((entry) => entry.rule_key !== itemKey)" :key="item.rule_key" :value="item.rule_key" :label="item.name" /></el-select></el-form-item>
        <el-form-item label="依赖计算"><el-select v-model="form.resultCodes" multiple filterable allow-create><el-option v-for="item in config.calculation_rules" :key="item.result_code" :value="item.result_code" :label="item.rule_name" /></el-select></el-form-item>
        <el-form-item label="检查等级"><el-input v-model="form.checkLevel" /></el-form-item>
        <el-form-item label="合格要求"><el-input v-model="form.checkOperator" type="textarea" :rows="2" /></el-form-item>
        <el-form-item label="风险等级"><el-input v-model="form.riskLevel" /></el-form-item>
        <el-form-item label="风险成立条件"><el-input v-model="form.riskCondition" type="textarea" :rows="3" /></el-form-item>
      </template>
      <template v-if="calculation">
        <el-form-item label="计算名称" required><el-input v-model="calculation.rule_name" maxlength="255" /></el-form-item>
        <el-form-item label="业务编号"><el-input v-model="calculation.source_id" maxlength="80" /></el-form-item>
        <el-form-item label="结果标识" required><el-input v-model="calculation.result_code" maxlength="80" /></el-form-item>
        <el-form-item label="状态"><el-select v-model="calculation.status" @change="changeCalculationStatus"><el-option value="CLEAR" label="启用" /><el-option value="PENDING" label="待确认" /><el-option value="DISABLED" label="暂停" /></el-select></el-form-item>
        <el-form-item label="计算方法"><el-input v-model="calculation.formula" type="textarea" :rows="4" /></el-form-item>
        <el-form-item label="业务要求"><el-input v-model="calculation.requirement" type="textarea" :rows="4" /></el-form-item>
        <el-form-item label="结果类型"><el-select v-model="calculation.result_type" filterable allow-create><el-option v-for="item in valueTypeOptions" :key="item.value" :value="item.value" :label="item.label" /></el-select></el-form-item>
        <el-form-item label="单位"><el-input v-model="calculation.unit" /></el-form-item>
        <el-form-item label="币种"><el-input v-model="calculation.currency" /></el-form-item>
        <el-form-item label="期间要求"><el-input v-model="calculation.period_policy" /></el-form-item>
        <el-form-item label="报表口径"><el-input v-model="calculation.statement_scope" /></el-form-item>
        <el-form-item label="计算参数">
          <div class="parameter-editor">
            <el-table :data="calculation.parameters" border size="small">
              <el-table-column label="参数名称" min-width="130"><template #default="{ row }"><el-input v-model="row.parameter_code" /></template></el-table-column>
              <el-table-column label="来源" width="150"><template #default="{ row }"><el-select v-model="row.source_type" @change="row.source_code = null"><el-option value="FIELD" label="抽取字段" /><el-option value="CALC_RESULT" label="计算结果" /><el-option value="CONSTANT" label="常量" /></el-select></template></el-table-column>
              <el-table-column label="取值" min-width="240"><template #default="{ row, $index }">
                <el-select v-if="row.source_type === 'FIELD'" v-model="row.source_code" filterable><el-option v-for="item in config.fields" :key="item.field_key" :value="item.field_key" :label="fieldName(item.field_key)" /></el-select>
                <el-select v-else-if="row.source_type === 'CALC_RESULT'" v-model="row.source_code" filterable><el-option v-for="item in config.calculation_rules.filter((entry) => entry.rule_code !== itemKey)" :key="item.result_code" :value="item.result_code" :label="item.rule_name" /></el-select>
                <el-input v-else v-model="constantTexts[$index]" placeholder='JSON 值，例如 0.1 或 "元"' />
              </template></el-table-column>
              <el-table-column width="65"><template #default="{ $index }"><el-button link type="danger" @click="removeParameter($index)">移除</el-button></template></el-table-column>
            </el-table>
            <el-button class="block-gap-sm" @click="addParameter">新增参数</el-button>
          </div>
        </el-form-item>
        <el-form-item label="备注"><el-input v-model="calculation.remark" type="textarea" :rows="2" /></el-form-item>
      </template>
      <el-collapse v-if="rule || calculation">
        <el-collapse-item title="补充业务属性" name="business">
          <el-form-item v-if="rule" label="判定参数"><el-input v-model="parametersText" type="textarea" :rows="5" /></el-form-item>
          <el-form-item v-if="rule" label="适用条件"><el-input v-model="applicabilityText" type="textarea" :rows="4" /></el-form-item>
          <el-form-item label="业务补充"><el-input v-model="businessText" type="textarea" :rows="5" /></el-form-item>
        </el-collapse-item>
      </el-collapse>
    </el-form>
    <template #footer><el-button @click="emit('update:modelValue', false)">取消</el-button><el-button type="primary" @click="apply">应用修改</el-button></template>
  </el-dialog>
</template>

<style scoped>
.el-select,
.parameter-editor {
  width: 100%;
}
</style>
