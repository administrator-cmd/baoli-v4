<script setup lang="ts">
import { computed, ref, watch } from "vue";

import type { JsonValue } from "@/api/contracts";
import {
  artifactTypeOptions,
  dateCompareOperatorOptions,
  decimalCompareOperatorOptions,
  nodeOptions,
  riskLevelOptions,
  riskSourceNodeOptions,
  ruleOperatorOptions,
  valueTypeOptions,
  valueTypeLabel,
} from "@/constants/labels";

/**
 * 候选原地修正弹窗。
 *
 * 只允许修改后端 RESOLVABLE_PROPERTIES 声明的属性，且全部以业务中文标签 + 输入控件呈现，
 * 不暴露 JSON 输入框；parameters 只提供后端支持的键。
 * 提交时只回传被改动的属性，由页面合并进裁决集合。
 */
type TargetType = "MATERIAL" | "FIELD" | "RULE";

interface Spec {
  key: string;
  label: string;
  kind: "text" | "textarea" | "select" | "switch" | "multiselect" | "number";
  options?: Array<{ value: string; label: string }>;
  clearable?: boolean;
  hint?: string;
}

const props = defineProps<{
  open: boolean;
  targetType: TargetType;
  candidate: Record<string, unknown> | null;
  /** 当前作业已有的材料候选，用于选择字段来源材料 / 规则适用材料。 */
  materials: Array<{ ref: string; name: string }>;
  /** 当前作业已有的规则候选，用于选择风险来源审核规则。 */
  rules: Array<{ ref: string; name: string }>;
  pending?: boolean;
}>();

const emit = defineEmits<{
  close: [];
  submit: [value: Record<string, JsonValue>];
}>();

const form = ref<Record<string, unknown>>({});

/** 把当前值合并进下拉选项：服务端返回了选项集合外的取值时也要如实显示，不能丢。 */
function withCurrent(options: Array<{ value: string; label: string }>, current: unknown): Array<{ value: string; label: string }> {
  if (typeof current !== "string" || !current || options.some((option) => option.value === current)) {
    return options;
  }
  return [...options, { value: current, label: `${current}（当前值）` }];
}

const materialRefOptions = computed(() => props.materials.map((item) => ({ value: item.ref, label: item.name })));
const ruleRefOptions = computed(() => props.rules.map((item) => ({ value: item.ref, label: item.name })));

const specs = computed<Spec[]>(() => {
  if (props.targetType === "MATERIAL") {
    return [
      { key: "name", label: "材料名称", kind: "text" },
      { key: "required", label: "是否必传", kind: "switch" },
    ];
  }
  if (props.targetType === "FIELD") {
    return [
      { key: "name", label: "字段名称", kind: "text" },
      { key: "value_type", label: "字段类型", kind: "select", options: withCurrent(valueTypeOptions, form.value.value_type) },
      { key: "unit", label: "单位", kind: "text", clearable: true, hint: "没有单位时留空，系统不会用默认值代替。" },
      {
        key: "material_ref",
        label: "来源材料",
        kind: "select",
        options: withCurrent(materialRefOptions.value, form.value.material_ref),
      },
      { key: "required", label: "是否必填", kind: "switch" },
      { key: "instruction", label: "抽取要求", kind: "textarea" },
    ];
  }
  const ruleSpecs: Spec[] = [
    { key: "name", label: "规则名称", kind: "text" },
    { key: "node", label: "所属节点", kind: "select", options: withCurrent(nodeOptions, form.value.node) },
    {
      key: "operator",
      label: "判断方式",
      kind: "select",
      options: withCurrent(ruleOperatorOptions, form.value.operator),
    },
  ];
  if (form.value.operator === "RISK_TRIGGER") {
    ruleSpecs.push({
      key: "source_node",
      label: "来源节点",
      kind: "select",
      options: withCurrent(riskSourceNodeOptions, form.value.source_node),
      clearable: true,
    }, {
      key: "source_rule_refs",
      label: "来源审核规则",
      kind: "multiselect",
      options: ruleRefOptions.value,
      hint: "可选多条来源检查；同一业务行展开的材料分支分别显示。",
    });
  }
  ruleSpecs.push({
      key: "material_refs",
      label: "适用材料",
      kind: "multiselect",
      options: materialRefOptions.value,
      hint: "适用材料应覆盖本规则引用的字段。",
  });
  return ruleSpecs;
});

/** parameters 只提供后端对该算子支持的键，避免产生会被拒绝的参数。 */
const parameterSpecs = computed<Spec[]>(() => {
  const operator = String(form.value.operator ?? "");
  if (operator === "FILE_QUALITY_CHECK") {
    return [
      {
        key: "param_artifact_type",
        label: "检查的处理产物",
        kind: "select",
        options: withCurrent(artifactTypeOptions, form.value.param_artifact_type),
      },
    ];
  }
  if (operator === "RISK_TRIGGER") {
    return [
      { key: "param_risk_level", label: "风险等级", kind: "select", options: withCurrent(riskLevelOptions, form.value.param_risk_level) },
      { key: "param_action", label: "风险处置动作", kind: "text" },
    ];
  }
  if (operator === "DATE_COMPARE") {
    return [
      { key: "param_operator", label: "日期比较方向", kind: "select", options: withCurrent(dateCompareOperatorOptions, form.value.param_operator) },
      { key: "param_threshold", label: "允许差值", kind: "text", hint: "留空表示严格比较；填写时单位必须为天。" },
      { key: "param_threshold_unit", label: "差值单位", kind: "text" },
      { key: "param_requires_manual_review", label: "需人工复核", kind: "switch" },
    ];
  }
  if (operator === "DECIMAL_COMPARE" || operator === "DECIMAL_SUM_COMPARE") {
    return [
      {
        key: "param_operator",
        label: operator === "DECIMAL_SUM_COMPARE" ? "加总比较方向" : "金额比较方向",
        kind: "select",
        options: withCurrent(decimalCompareOperatorOptions, form.value.param_operator),
        hint: operator === "DECIMAL_SUM_COMPARE" ? "加总比较必须填写允许差值。" : undefined,
      },
      { key: "param_threshold", label: "允许差值", kind: "text" },
      { key: "param_threshold_unit", label: "差值单位", kind: "text" },
      { key: "param_requires_manual_review", label: "需人工复核", kind: "switch" },
    ];
  }
  if (operator === "DECIMAL_CN_EQUAL") {
    return [
      { key: "param_threshold", label: "允许差值", kind: "text", hint: "只能填写 0 或留空。" },
      { key: "param_requires_manual_review", label: "需人工复核", kind: "switch" },
    ];
  }
  if (operator === "LLM_EXTRACT") {
    return [{ key: "param_instruction", label: "抽取要求", kind: "textarea" }];
  }
  if (operator === "LLM_VALIDATE") {
    return [
      { key: "param_instruction", label: "审核要求", kind: "textarea" },
      { key: "param_requires_manual_review", label: "需人工复核", kind: "switch" },
    ];
  }
  if (["FIELD_EQUAL", "FIELD_NOT_EQUAL", "FIELD_NOT_EMPTY", "CLAUSE_ABSENT"].includes(operator)) {
    return [{ key: "param_requires_manual_review", label: "需人工复核", kind: "switch" }];
  }
  return [];
});

function resetForm(): void {
  const candidate = props.candidate ?? {};
  const parameters = (candidate.parameters ?? {}) as Record<string, unknown>;
  form.value = {
    name: candidate.name ?? "",
    required: Boolean(candidate.required),
    value_type: candidate.value_type ?? "STRING",
    unit: candidate.unit ?? "",
    material_ref: candidate.material_ref ?? candidate.material_name ?? "",
    instruction: candidate.instruction ?? "",
    node: candidate.node ?? "",
    operator: candidate.operator ?? "",
    source_node: candidate.source_node ?? "",
    source_rule_refs: Array.isArray(candidate.source_rule_refs) ? [...candidate.source_rule_refs] : [],
    material_refs: Array.isArray(candidate.material_refs) ? [...(candidate.material_refs as string[])] : [],
    param_artifact_type: parameters.artifact_type ?? "",
    param_risk_level: parameters.risk_level ?? "",
    param_action: parameters.action ?? "",
    param_operator: parameters.operator ?? "",
    param_threshold: parameters.threshold == null ? "" : String(parameters.threshold),
    param_threshold_unit: parameters.threshold_unit ?? "",
    param_instruction: parameters.instruction ?? "",
    param_requires_manual_review: Boolean(parameters.requires_manual_review),
  };
}

function updateSpecValue(key: string, value: unknown): void {
  if (key === "operator" && value !== form.value.operator) {
    for (const parameter of ["artifact_type", "risk_level", "action", "operator", "threshold", "threshold_unit", "instruction"]) {
      form.value[`param_${parameter}`] = "";
    }
    form.value.param_requires_manual_review = false;
    form.value.source_node = "";
    form.value.source_rule_refs = [];
  }
  form.value[key] = value;
}

watch(
  () => [props.open, props.candidate] as const,
  () => {
    if (props.open) {
      resetForm();
    }
  },
  { immediate: true },
);

function normalize(value: unknown): unknown {
  if (value === "" || value === undefined) {
    return null;
  }
  return value;
}

function buildParameters(): Record<string, JsonValue> {
  const original = (props.candidate?.parameters ?? {}) as Record<string, JsonValue>;
  const operatorChanged = form.value.operator !== props.candidate?.operator;
  const parameters: Record<string, JsonValue> = operatorChanged ? {} : { ...original };
  for (const spec of parameterSpecs.value) {
    const key = spec.key.slice("param_".length);
    const current = form.value[spec.key];
    const previous = key === "threshold"
      ? (original.threshold == null ? "" : String(original.threshold))
      : key === "requires_manual_review"
        ? Boolean(original.requires_manual_review)
        : (original[key] ?? "");
    if (!operatorChanged && equalValue(current, previous)) {
      continue;
    }
    if (key === "requires_manual_review") {
      if (current) {
        parameters[key] = true;
      } else {
        delete parameters[key];
      }
    } else {
      const value = normalize(current);
      if (value === null) {
        delete parameters[key];
      } else {
        parameters[key] = value as JsonValue;
      }
    }
  }
  if (form.value.operator === "DATE_COMPARE" && !("threshold" in parameters)) {
    delete parameters.threshold_unit;
  }
  return parameters;
}

function equalValue(left: unknown, right: unknown): boolean {
  return JSON.stringify(left ?? null) === JSON.stringify(right ?? null);
}

function submit(): void {
  const candidate = props.candidate ?? {};
  const value: Record<string, JsonValue> = {};
  if (props.targetType === "MATERIAL") {
    for (const key of ["name", "required"]) {
      if (!equalValue(form.value[key], candidate[key])) {
        value[key] = form.value[key] as JsonValue;
      }
    }
  } else if (props.targetType === "FIELD") {
    for (const key of ["name", "value_type", "unit", "material_ref", "required", "instruction"]) {
      if (!equalValue(form.value[key], candidate[key])) {
        value[key] = normalize(form.value[key]) as JsonValue;
      }
    }
  } else {
    for (const key of ["name", "node", "operator", "source_node"]) {
      const current = normalize(form.value[key]);
      if (!equalValue(current, candidate[key])) {
        value[key] = current as JsonValue;
      }
    }
    const sourceRuleRefs = (form.value.source_rule_refs as string[]) ?? [];
    if (!equalValue(sourceRuleRefs, candidate.source_rule_refs ?? [])) {
      value.source_rule_refs = sourceRuleRefs;
      value.source_rule_ref = null;
    }
    const materialRefs = (form.value.material_refs as string[]) ?? [];
    if (!equalValue(materialRefs, candidate.material_refs ?? [])) {
      value.material_refs = materialRefs;
    }
    const parameters = buildParameters();
    if (!equalValue(parameters, candidate.parameters ?? {})) {
      value.parameters = parameters;
    }
  }
  emit("submit", value);
}
</script>

<template>
  <el-dialog :model-value="open" title="修改候选内容" width="620px" append-to-body @update:model-value="emit('close')">
    <p class="text-muted">
      只显示本类型允许修改的业务属性；提交后立即按服务端校验重新编译，未发布的草稿可继续修正。
    </p>

    <el-form label-width="160px" label-position="right">
      <el-form-item v-for="spec in specs" :key="spec.key" :label="spec.label">
        <el-switch v-if="spec.kind === 'switch'" :model-value="Boolean(form[spec.key])" @update:model-value="form[spec.key] = $event" />
        <el-input
          v-else-if="spec.kind === 'text'"
          :model-value="(form[spec.key] as string) ?? ''"
          @update:model-value="form[spec.key] = $event"
        />
        <el-input
          v-else-if="spec.kind === 'textarea'"
          :model-value="(form[spec.key] as string) ?? ''"
          type="textarea"
          :rows="2"
          maxlength="500"
          show-word-limit
          @update:model-value="form[spec.key] = $event"
        />
        <el-select
          v-else-if="spec.kind === 'select'"
          :model-value="(form[spec.key] as string) ?? ''"
          :clearable="spec.clearable"
          style="width: 100%"
          @update:model-value="updateSpecValue(spec.key, $event ?? '')"
        >
          <el-option v-for="option in spec.options" :key="option.value" :label="option.label" :value="option.value" />
        </el-select>
        <el-select
          v-else-if="spec.kind === 'multiselect'"
          :model-value="(form[spec.key] as string[]) ?? []"
          multiple
          style="width: 100%"
          @update:model-value="form[spec.key] = $event"
        >
          <el-option v-for="option in spec.options" :key="option.value" :label="option.label" :value="option.value" />
        </el-select>
        <el-input-number
          v-else
          :model-value="(form[spec.key] as number) ?? undefined"
          controls-position="right"
          @update:model-value="form[spec.key] = $event"
        />
        <div v-if="spec.hint" class="text-hint">{{ spec.hint }}</div>
      </el-form-item>

      <el-divider v-if="parameterSpecs.length" content-position="left">判断参数</el-divider>
      <el-form-item v-for="spec in parameterSpecs" :key="spec.key" :label="spec.label">
        <el-switch v-if="spec.kind === 'switch'" :model-value="Boolean(form[spec.key])" @update:model-value="form[spec.key] = $event" />
        <el-input
          v-else-if="spec.kind === 'text'"
          :model-value="(form[spec.key] as string) ?? ''"
          @update:model-value="form[spec.key] = $event"
        />
        <el-input
          v-else-if="spec.kind === 'textarea'"
          :model-value="(form[spec.key] as string) ?? ''"
          type="textarea"
          :rows="2"
          maxlength="500"
          show-word-limit
          @update:model-value="form[spec.key] = $event"
        />
        <el-select
          v-else-if="spec.kind === 'select'"
          :model-value="(form[spec.key] as string) ?? ''"
          :clearable="spec.clearable"
          style="width: 100%"
          @update:model-value="form[spec.key] = $event ?? ''"
        >
          <el-option v-for="option in spec.options" :key="option.value" :label="option.label" :value="option.value" />
        </el-select>
        <el-input-number
          v-else
          :model-value="(form[spec.key] as number) ?? undefined"
          controls-position="right"
          @update:model-value="form[spec.key] = $event"
        />
        <div v-if="spec.hint" class="text-hint">{{ spec.hint }}</div>
      </el-form-item>
    </el-form>

    <p class="text-hint">
      当前字段类型：{{ valueTypeLabel((form.value_type as string) ?? null) }}。修改后请核对判断方向、阈值与单位。
    </p>

    <template #footer>
      <el-button @click="emit('close')">取消</el-button>
      <el-button type="primary" :loading="pending" @click="submit">保存修改</el-button>
    </template>
  </el-dialog>
</template>

<style scoped>
:deep(.el-form-item__content) {
  flex-direction: column;
  align-items: stretch;
}
</style>
