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
      // 材料类别来自模板单元格的自由文本（例如「合同」「发票」），不做固定枚举猜测。
      { key: "material_type", label: "材料类别", kind: "text" },
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
  return [
    { key: "name", label: "规则名称", kind: "text" },
    { key: "node", label: "所属节点", kind: "select", options: withCurrent(nodeOptions, form.value.node) },
    {
      key: "operator",
      label: "判断方式",
      kind: "select",
      options: withCurrent(ruleOperatorOptions, form.value.operator),
    },
    {
      key: "source_node",
      label: "来源节点（风险疑点）",
      kind: "select",
      options: withCurrent(riskSourceNodeOptions, form.value.source_node),
      clearable: true,
      hint: "仅 06_风险疑点 规则使用；其它规则留空。",
    },
    {
      key: "source_rule_ref",
      label: "来源审核规则（风险疑点）",
      kind: "select",
      options: withCurrent(ruleRefOptions.value, form.value.source_rule_ref),
      clearable: true,
    },
    {
      key: "material_refs",
      label: "适用材料",
      kind: "multiselect",
      options: materialRefOptions.value,
      hint: "一条规则只比较左右各一个字段；多字段关系请拆成多条规则。",
    },
  ];
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
      { key: "param_threshold", label: "允许差值", kind: "number", hint: "仅在「差额不超过阈值」时需要填写。" },
      { key: "param_requires_manual_review", label: "需人工复核", kind: "switch" },
    ];
  }
  if (operator === "LLM_EXTRACT" || operator === "LLM_VALIDATE") {
    return [
      { key: "param_instruction", label: "审核要求", kind: "textarea" },
      { key: "param_requires_manual_review", label: "需人工复核", kind: "switch" },
    ];
  }
  if (operator === "FIELD_EQUAL" || operator === "FIELD_NOT_EQUAL") {
    return [{ key: "param_requires_manual_review", label: "需人工复核", kind: "switch" }];
  }
  return [];
});

function resetForm(): void {
  const candidate = props.candidate ?? {};
  const parameters = (candidate.parameters ?? {}) as Record<string, unknown>;
  form.value = {
    name: candidate.name ?? "",
    material_type: candidate.material_type ?? "",
    required: Boolean(candidate.required),
    value_type: candidate.value_type ?? "STRING",
    unit: candidate.unit ?? "",
    material_ref: candidate.material_ref ?? candidate.material_name ?? "",
    instruction: candidate.instruction ?? "",
    node: candidate.node ?? "",
    operator: candidate.operator ?? "",
    source_node: candidate.source_node ?? "",
    source_rule_ref: candidate.source_rule_ref ?? "",
    material_refs: Array.isArray(candidate.material_refs) ? [...(candidate.material_refs as string[])] : [],
    param_artifact_type: parameters.artifact_type ?? "",
    param_risk_level: parameters.risk_level ?? "",
    param_action: parameters.action ?? "",
    param_operator: parameters.operator ?? "",
    param_threshold: parameters.threshold ?? null,
    param_instruction: parameters.instruction ?? "",
    param_requires_manual_review: Boolean(parameters.requires_manual_review),
  };
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

/**
 * 组装后端 parameters。
 *
 * 以**候选现有的 parameters 为基础**，只覆盖本弹窗真正呈现的键；未呈现的键原样保留。
 * 此前这里从空对象开始拼：`DECIMAL_SUM_COMPARE`（加总比较）、`FIELD_NOT_EMPTY`、
 * `CLAUSE_ABSENT`、`DECIMAL_CN_EQUAL` 都不在下面的分支里，于是编辑这些规则会把
 * `operator` / `threshold` 等参数**整体清空**后再提交——一次「改个名字」的操作
 * 就能把这条规则的比较方向与允许差值抹掉，而且页面上看不出发生了什么。
 */
function buildParameters(): Record<string, JsonValue> {
  const operator = String(form.value.operator ?? "");
  const parameters: Record<string, JsonValue> = {
    ...((props.candidate?.parameters as Record<string, JsonValue>) ?? {}),
  };
  // 先清掉本弹窗负责的键，再由下面的分支按当前表单值重新写入；
  // 不清掉会让「换成别的方向」后残留旧方向的阈值。
  for (const key of ["operator", "threshold", "artifact_type", "risk_level", "action", "instruction"]) {
    delete parameters[key];
  }
  const put = (key: string, value: unknown) => {
    const normalized = normalize(value);
    if (normalized !== null) {
      parameters[key] = normalized as JsonValue;
    }
  };
  if (operator === "FILE_QUALITY_CHECK") {
    put("artifact_type", form.value.param_artifact_type);
  } else if (operator === "RISK_TRIGGER") {
    put("risk_level", form.value.param_risk_level);
    put("action", form.value.param_action);
  } else if (operator === "DATE_COMPARE") {
    put("operator", form.value.param_operator);
    put("threshold", form.value.param_threshold);
  } else if (operator === "DECIMAL_COMPARE") {
    put("operator", form.value.param_operator);
    if (form.value.param_operator === "ABS_DIFF_LTE") {
      put("threshold", form.value.param_threshold);
    }
  } else if (operator === "DECIMAL_SUM_COMPARE") {
    // 加总比较同样需要方向与允许差值；缺 threshold 时后端会以「缺少必填参数」拒绝，
    // 那不是静默失败，但页面本来就该把这两个键呈现出来。
    put("operator", form.value.param_operator);
    put("threshold", form.value.param_threshold);
  } else if (operator === "LLM_EXTRACT" || operator === "LLM_VALIDATE") {
    put("instruction", form.value.param_instruction);
  }
  if (form.value.param_requires_manual_review) {
    parameters.requires_manual_review = true;
  } else {
    delete parameters.requires_manual_review;
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
    for (const key of ["name", "material_type", "required"]) {
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
    for (const key of ["name", "node", "operator", "source_node", "source_rule_ref"]) {
      if (!equalValue(form.value[key], candidate[key])) {
        value[key] = normalize(form.value[key]) as JsonValue;
      }
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
          @update:model-value="form[spec.key] = $event ?? ''"
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
