<script setup lang="ts">
import { ref, watch } from "vue";
import type { PlanConfig, PlanFieldItem, PlanMaterialItem } from "@/api/contracts";
import { fieldDataType, fieldDataTypeOptions, fieldTypePayload, STRUCTURED_FIELD_TYPE } from "@/utils/fieldTypeConfig";
import {
  artifactSourceOptions,
  periodScopeOptions,
  statementScopeOptions,
} from "@/constants/labels";

const props = defineProps<{
  modelValue: boolean;
  config: PlanConfig;
  kind: "material" | "field";
  itemKey: string;
}>();
const emit = defineEmits<{ "update:modelValue": [value: boolean] }>();
const material = ref<PlanMaterialItem | null>(null);
const field = ref<PlanFieldItem | null>(null);
const dataType = ref("STRING");
const schemaText = ref("");
const error = ref("");

watch(() => [props.modelValue, props.kind, props.itemKey], () => {
  if (!props.modelValue) return;
  material.value = null;
  field.value = null;
  error.value = "";
  schemaText.value = "";
  const item = props.kind === "material"
    ? props.config.materials.find((entry) => entry.material_key === props.itemKey)
    : props.config.fields.find((entry) => entry.field_key === props.itemKey);
  if (!item) {
    error.value = "配置项不存在，请关闭后重新加载方案";
    return;
  }
  if (props.kind === "material") {
    material.value = JSON.parse(JSON.stringify(item)) as PlanMaterialItem;
  } else {
    field.value = JSON.parse(JSON.stringify(item)) as PlanFieldItem;
    dataType.value = fieldDataType(field.value);
    schemaText.value = field.value.structure_schema ? JSON.stringify(field.value.structure_schema, null, 2) : "";
  }
});

function changeFieldStatus(): void {
  if (field.value) field.value.enabled = field.value.config_status === "CLEAR";
}

function apply(): void {
  error.value = "";
  try {
    if (material.value) {
      const edited = material.value;
      if (!edited.name.trim()) throw new Error("请填写材料名称");
      const target = props.config.materials.find((item) => item.material_key === props.itemKey);
      if (!target) throw new Error("材料已不存在，请重新加载方案");
      Object.assign(target, { ...edited, name: edited.name.trim() });
    } else if (field.value) {
      const edited = field.value;
      if (!edited.name.trim() || !edited.material_key) throw new Error("请填写字段名称并选择所属材料");
      let schema: Record<string, unknown> | null = null;
      if (dataType.value === STRUCTURED_FIELD_TYPE) {
        if (!schemaText.value.trim()) throw new Error("结构化对象必须填写结构定义");
        let parsed: unknown;
        try {
          parsed = JSON.parse(schemaText.value);
        } catch {
          throw new Error("结构定义不是有效的 JSON");
        }
        if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
          throw new Error("结构定义的根节点必须是 JSON 对象");
        }
        schema = parsed as Record<string, unknown>;
      }
      const target = props.config.fields.find((item) => item.field_key === props.itemKey);
      if (!target) throw new Error("字段已不存在，请重新加载方案");
      Object.assign(target, {
        ...edited,
        ...fieldTypePayload(dataType.value, edited),
        name: edited.name.trim(),
        artifact_source: edited.artifact_source || null,
        structure_schema: schema,
      });
    } else {
      throw new Error("未加载可编辑的配置项");
    }
    emit("update:modelValue", false);
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : String(cause);
  }
}
</script>

<template>
  <el-dialog :model-value="modelValue" :title="kind === 'material' ? '编辑材料' : '编辑字段'" width="min(880px, 96vw)" :close-on-click-modal="false" @update:model-value="emit('update:modelValue', $event)">
    <el-alert v-if="error" :title="error" type="error" show-icon :closable="false" class="block-gap-sm" />
    <el-form label-width="100px" class="extraction-form">
      <template v-if="material">
        <el-form-item label="材料名称" required><el-input v-model="material.name" maxlength="255" /></el-form-item>
        <el-form-item label="是否必传"><el-switch v-model="material.required" aria-label="是否必传" /></el-form-item>
        <el-form-item label="配置状态"><el-select v-model="material.config_status" placeholder="未声明"><el-option value="CLEAR" label="启用" /><el-option value="PENDING" label="待确认" /><el-option value="DISABLED" label="停用" /></el-select></el-form-item>
        <el-form-item label="业务编号"><el-input v-model="material.source_ref" /></el-form-item>
        <el-form-item label="业务说明" class="extraction-form__wide"><el-input v-model="material.description" type="textarea" :rows="4" /></el-form-item>
      </template>
      <template v-if="field">
        <el-form-item label="字段名称" required><el-input v-model="field.name" maxlength="255" /></el-form-item>
        <el-form-item label="所属材料" required><el-select v-model="field.material_key" filterable><el-option v-for="item in config.materials" :key="item.material_key" :value="item.material_key" :label="item.name" /></el-select></el-form-item>
        <el-form-item label="数据类型"><el-select v-model="dataType"><el-option v-for="item in fieldDataTypeOptions" :key="item.value" :value="item.value" :label="item.label" /></el-select></el-form-item>
        <el-form-item label="是否必抽"><el-switch v-model="field.required" aria-label="是否必抽" /></el-form-item>
        <el-form-item label="配置状态"><el-select v-model="field.config_status" placeholder="未声明" @change="changeFieldStatus"><el-option value="CLEAR" label="启用" /><el-option value="PENDING" label="待确认" /><el-option value="DISABLED" label="停用" /></el-select></el-form-item>
        <el-form-item label="单位"><el-input v-model="field.unit" /></el-form-item>
        <el-form-item label="币种"><el-input v-model="field.currency" /></el-form-item>
        <el-form-item label="期间口径"><el-select v-model="field.period_scope" filterable allow-create><el-option v-for="item in periodScopeOptions" :key="item.value" :value="item.value" :label="item.label" /></el-select></el-form-item>
        <el-form-item label="报表口径"><el-select v-model="field.statement_scope" filterable allow-create><el-option v-for="item in statementScopeOptions" :key="item.value" :value="item.value" :label="item.label" /></el-select></el-form-item>
        <el-form-item label="产物来源"><el-select v-model="field.artifact_source" clearable placeholder="常规抽取"><el-option v-for="item in artifactSourceOptions" :key="item.value" :value="item.value" :label="item.label" /></el-select></el-form-item>
        <el-form-item label="业务编号"><el-input v-model="field.source_ref" /></el-form-item>
        <el-form-item label="提取要求" class="extraction-form__wide"><el-input v-model="field.instruction" type="textarea" :rows="4" /></el-form-item>
        <el-form-item v-if="dataType === STRUCTURED_FIELD_TYPE" label="结构定义" class="extraction-form__wide" required><el-input v-model="schemaText" type="textarea" :rows="10" placeholder="填写 JSON 对象形式的结构定义" /></el-form-item>
      </template>
    </el-form>
    <template #footer><el-button @click="emit('update:modelValue', false)">取消</el-button><el-button type="primary" @click="apply">应用修改</el-button></template>
  </el-dialog>
</template>

<style scoped>
.extraction-form {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  column-gap: 20px;
}

.extraction-form__wide {
  grid-column: 1 / -1;
}

.el-select {
  width: 100%;
}

@media (max-width: 640px) {
  .extraction-form {
    grid-template-columns: minmax(0, 1fr);
  }
}
</style>
