<script setup lang="ts">
import { computed, reactive, ref, watch } from "vue";
import { ElMessage, ElMessageBox } from "element-plus";

import type { DraftFieldItem, ScenarioConfigurationView } from "@/api/contracts";
import { ApiError } from "@/api/http";
import { useLocalPagination } from "@/composables/useLocalPagination";
import { PAGE_SIZE_OPTIONS } from "@/constants/pagination";
import { TABLE_COLUMN_WIDTHS } from "@/constants/tableColumns";
import { compareNaturalLabels } from "@/utils/naturalSort";
import { fieldDataType, fieldDataTypeLabel, fieldDataTypeOptions, fieldTypePayload, STRUCTURED_FIELD_TYPE } from "@/utils/fieldTypeConfig";
import {
  createDraftField,
  deleteDraftField,
  updateDraftField,
} from "@/api/scenes";
import {
  artifactSourceLabel,
  artifactSourceOptions,
  periodScopeLabel,
  periodScopeOptions,
  statementScopeLabel,
  statementScopeOptions,
} from "@/constants/labels";

/**
 * 字段配置面板：以场景 + 版本为作用域。
 * 字段属于某个材料位；抽取要求完整可编辑（表格只展示一行，详情在弹窗里）。
 * 稳定标识在内部维护，名称不是主键：改名不影响已发布版本与任务。
 */
const props = defineProps<{
  scenarioCode: string | null;
  /** 父级持有的同一份场景配置；面板不再各自请求整份配置。 */
  config: ScenarioConfigurationView | null;
  configError?: string | null;
  configLoading?: boolean;
}>();

const emit = defineEmits<{
  changed: [];
  refresh: [];
}>();

/** 数据来自父级唯一的配置请求。 */
const draft = computed(() => props.config);
const fields = computed(() => props.config?.fields ?? []);
const materials = computed(() => props.config?.materials ?? []);
const loading = computed(() => props.configLoading === true);
const loadError = computed(() => props.configError ?? null);
const saving = ref(false);

const canEdit = computed(() => Boolean(props.scenarioCode));
const query = reactive({ material_key: "", name: "" });
const filteredFields = computed(() =>
  fields.value.filter(
    (item) =>
      (!query.material_key || item.material_key === query.material_key) &&
      (!query.name.trim() || `${item.name}\n${item.source_ref ?? ""}`.toLocaleLowerCase().includes(query.name.trim().toLocaleLowerCase())),
  ).sort((left, right) => compareNaturalLabels(left.source_ref, right.source_ref)),
);
const { page, pageSize, pageItems: pagedFields } = useLocalPagination(() => filteredFields.value);

watch([() => query.material_key, () => query.name], () => {
  page.value = 1;
});

/**
 * 被规则引用的字段键。
 *
 * 引用关系来自同一份草稿/版本里的规则定义（inputs 与 outputs），不是后端单独提供的字段；
 * 删除被引用的字段会被服务端拒绝，页面据此提前标出，避免用户误删。
 */
const referencedFieldKeys = computed(() => {
  const rules = draft.value?.rules ?? [];
  const keys = new Set<string>();
  for (const rule of rules) {
    for (const input of rule.inputs ?? []) {
      if (input.field_key) keys.add(input.field_key);
    }
    for (const output of rule.outputs ?? []) {
      if (output.field_key) keys.add(output.field_key);
    }
  }
  return keys;
});

const dialogVisible = ref(false);
const editingItem = ref<DraftFieldItem | null>(null);
const form = reactive({
  material_key: "",
  name: "",
  data_type: "STRING",
  structure_schema_text: "",
  unit: "",
  currency: "",
  artifact_source: "",
  // 取数期间与报表口径：计算参数按这两项定位字段事实。
  // 表单初始选择「不适用」，涉及期间或报表口径时按真实业务配置选择。
  period_scope: "NOT_APPLICABLE",
  statement_scope: "NOT_APPLICABLE",
  required: false,
  instruction: "",
  // 真实业务编号可留空；自由规则库用稳定字段身份关联。
  source_ref: "",
});

const dependencyIssue = ref<{ title: string; entries: string[] } | null>(null);

function openCreate(): void {
  editingItem.value = null;
  form.material_key = materials.value[0]?.material_key ?? "";
  form.name = "";
  form.data_type = "STRING";
  form.structure_schema_text = "";
  form.unit = "";
  form.currency = "";
  form.artifact_source = "";
  form.period_scope = "NOT_APPLICABLE";
  form.statement_scope = "NOT_APPLICABLE";
  form.required = false;
  form.instruction = "";
  form.source_ref = "";
  dialogVisible.value = true;
}

function openEdit(item: DraftFieldItem): void {
  editingItem.value = item;
  form.material_key = item.material_key;
  form.name = item.name;
  form.data_type = fieldDataType(item);
  form.structure_schema_text = item.structure_schema ? JSON.stringify(item.structure_schema, null, 2) : "";
  form.unit = item.unit ?? "";
  form.currency = item.currency ?? "";
  form.artifact_source = item.artifact_source ?? "";
  form.period_scope = item.period_scope ?? "NOT_APPLICABLE";
  form.statement_scope = item.statement_scope ?? "NOT_APPLICABLE";
  form.required = item.required;
  form.instruction = item.instruction;
  form.source_ref = item.source_ref ?? "";
  dialogVisible.value = true;
}

defineExpose({ openEdit });

function describeError(error: unknown): string {
  return error instanceof Error ? error.message : "操作失败";
}

function parsedStructureSchema(): Record<string, unknown> | null {
  if (form.data_type !== STRUCTURED_FIELD_TYPE) return null;
  const text = form.structure_schema_text.trim();
  if (!text) {
    throw new Error("结构化对象必须填写结构定义");
  }
  let parsed: unknown;
  try {
    parsed = JSON.parse(text);
  } catch {
    throw new Error("结构定义不是有效的 JSON");
  }
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
    throw new Error("结构定义的根节点必须是 JSON 对象");
  }
  return parsed as Record<string, unknown>;
}

async function submitForm(): Promise<void> {
  if (!props.scenarioCode || !form.name.trim() || !form.material_key) {
    ElMessage.warning("请选择所属材料并填写字段名称");
    return;
  }
  let structureSchema: Record<string, unknown> | null;
  try {
    structureSchema = parsedStructureSchema();
  } catch (error) {
    ElMessage.warning(describeError(error));
    return;
  }
  saving.value = true;
  try {
    const payload = {
      material_key: form.material_key,
      name: form.name.trim(),
      ...fieldTypePayload(form.data_type, editingItem.value),
      structure_schema: structureSchema,
      unit: form.unit.trim() ? form.unit.trim() : null,
      // 币种与产物来源：留空表示未声明 / 常规字段抽取，不表示“随便选一个”。
      currency: form.currency.trim() ? form.currency.trim() : null,
      artifact_source: form.artifact_source ? form.artifact_source : null,
      // 仅在规则实际涉及期间或报表口径时选择；留空由服务端归一为“不适用”。
      period_scope: form.period_scope,
      statement_scope: form.statement_scope,
      required: form.required,
      instruction: form.instruction,
      source_ref: form.source_ref.trim() ? form.source_ref.trim() : null,
    };
    if (editingItem.value) {
      await updateDraftField(props.scenarioCode, editingItem.value.field_key, payload);
      ElMessage.success("字段配置已保存");
    } else {
      await createDraftField(props.scenarioCode, payload);
      ElMessage.success("字段配置已新增");
    }
    dialogVisible.value = false;
    emit("changed");
  } catch (error) {
    ElMessage.error(describeError(error));
  } finally {
    saving.value = false;
  }
}

async function removeItem(item: DraftFieldItem): Promise<void> {
  if (!props.scenarioCode) {
    return;
  }
  try {
    await ElMessageBox.confirm(
      `确认删除字段「${item.name}」？删除前会检查规则、计算与报告章节引用；历史版本不受影响。`,
      "删除字段配置",
      { type: "warning", confirmButtonText: "删除", cancelButtonText: "取消" },
    );
  } catch {
    return;
  }
  saving.value = true;
  try {
    await deleteDraftField(props.scenarioCode, item.field_key);
    ElMessage.success("字段配置已删除");
    emit("changed");
  } catch (error) {
    if (error instanceof ApiError && error.code === "FIELD_REFERENCED") {
      const details = error.details as Record<string, unknown>;
      const rules = Array.isArray(details.rule_keys) ? (details.rule_keys as string[]) : [];
      const calculations = Array.isArray(details.calculation_rules) ? (details.calculation_rules as string[]) : [];
      const sections = Array.isArray(details.section_names) ? (details.section_names as string[]) : [];
      dependencyIssue.value = {
        title: error.message,
        entries: [...rules.map((item) => `规则：${item}`), ...calculations.map((item) => `计算：${item}`),
          ...sections.map((item) => `报告章节：${item}`)],
      };
    } else {
      ElMessage.error(describeError(error));
    }
  } finally {
    saving.value = false;
  }
}
</script>

<template>
  <div>
    <el-alert v-if="!scenarioCode" type="info" show-icon :closable="false" title="请先选择场景" />

    <template v-else>
      <el-form inline class="app-query block-gap-sm">
        <el-form-item label="所属材料">
          <el-select v-model="query.material_key" clearable placeholder="全部材料" style="width: 200px">
            <el-option v-for="item in materials" :key="item.material_key" :value="item.material_key" :label="item.name" />
          </el-select>
        </el-form-item>
        <el-form-item label="字段搜索">
          <el-input v-model="query.name" clearable placeholder="字段名称或编号" style="width: 200px" />
        </el-form-item>
      </el-form>

      <div class="app-toolbar">
        <div class="app-toolbar__left">
          <el-button v-if="canEdit" type="primary" :disabled="!materials.length" @click="openCreate">新增字段</el-button>
          <span v-if="canEdit && !materials.length" class="text-hint">先在材料配置中建立材料位，再添加字段。</span>
        </div>
        <div class="app-toolbar__right">
          <el-button :loading="loading" @click="$emit('refresh')">刷新</el-button>
        </div>
      </div>

      <el-alert v-if="loadError" type="error" show-icon :closable="false" :title="`读取失败：${loadError}`">
        <el-button size="small" @click="$emit('refresh')">重试</el-button>
      </el-alert>

      <el-table v-else :data="pagedFields" border stripe class="management-table" v-loading="loading" :empty-text="fields.length ? '未找到匹配的字段' : '暂无字段配置'">
        <el-table-column prop="source_ref" label="字段编号" width="140" align="center">
          <template #default="{ row }">
            <span v-if="row.source_ref" class="mono">{{ row.source_ref }}</span>
            <span v-else class="text-hint">未填写</span>
          </template>
        </el-table-column>
        <el-table-column prop="name" label="字段名称" min-width="180" show-overflow-tooltip />
        <el-table-column prop="material_name" label="来源材料" width="160" show-overflow-tooltip />
        <el-table-column label="数据类型" width="112" align="center">
          <template #default="{ row }">{{ fieldDataTypeLabel(row) }}</template>
        </el-table-column>
        <el-table-column label="单位" width="64" align="center">
          <template #default="{ row }">{{ row.unit || "—" }}</template>
        </el-table-column>
        <el-table-column label="币种" width="64" align="center">
          <template #default="{ row }">{{ row.currency || "—" }}</template>
        </el-table-column>
        <el-table-column label="取数期间" width="120" align="center">
          <template #default="{ row }">{{ periodScopeLabel(row.period_scope) }}</template>
        </el-table-column>
        <el-table-column label="报表口径" width="120" align="center">
          <template #default="{ row }">{{ statementScopeLabel(row.statement_scope) }}</template>
        </el-table-column>
        <el-table-column label="产物来源" :width="TABLE_COLUMN_WIDTHS.artifactSource" align="center">
          <template #default="{ row }">{{ artifactSourceLabel(row.artifact_source) }}</template>
        </el-table-column>
        <el-table-column label="是否必填" width="88" align="center">
          <template #default="{ row }">
            <el-tag :type="row.required ? 'warning' : 'info'" size="small" effect="plain" disable-transitions>
              {{ row.required ? "是" : "否" }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="抽取要求" min-width="280" show-overflow-tooltip>
          <template #default="{ row }">{{ row.instruction || "—" }}</template>
        </el-table-column>
        <el-table-column label="规则引用" width="130" align="center">
          <template #default="{ row }">
            <el-tag v-if="referencedFieldKeys.has(row.field_key)" type="info" size="small" effect="plain" disable-transitions>
              已被引用
            </el-tag>
            <span v-else class="text-hint">未引用</span>
          </template>
        </el-table-column>
        <el-table-column v-if="canEdit" label="操作" width="124" class-name="table-actions" fixed="right" align="center">
          <template #default="{ row }">
            <el-button link type="primary" size="small" @click="openEdit(row)">编辑</el-button>
            <el-button link type="danger" size="small" @click="removeItem(row)">删除</el-button>
          </template>
        </el-table-column>
      </el-table>

      <div v-if="!loadError && filteredFields.length" class="app-pagination">
        <el-pagination
          v-model:current-page="page"
          v-model:page-size="pageSize"
          :total="filteredFields.length"
          :page-sizes="PAGE_SIZE_OPTIONS"
          layout="total, sizes, prev, pager, next, jumper"
          background
        />
      </div>

      <el-dialog v-model="dialogVisible" :title="editingItem ? '编辑字段' : '新增字段'" width="min(680px, 96vw)" :close-on-click-modal="false">
        <el-form label-width="100px">
          <el-form-item label="所属材料" required>
            <el-select v-model="form.material_key" placeholder="请选择材料位" style="width: 100%">
              <el-option v-for="item in materials" :key="item.material_key" :value="item.material_key" :label="item.name" />
            </el-select>
          </el-form-item>
          <el-form-item label="字段编号">
            <el-input v-model="form.source_ref" maxlength="80" placeholder="例如：FD-001" />
          </el-form-item>
          <el-form-item label="字段名称" required>
            <el-input v-model="form.name" maxlength="255" placeholder="例如：应收账款金额" />
          </el-form-item>
          <el-form-item label="数据类型" required>
            <el-select v-model="form.data_type" style="width: 100%">
              <el-option v-for="item in fieldDataTypeOptions" :key="item.value" :value="item.value" :label="item.label" />
            </el-select>
          </el-form-item>
          <el-form-item v-if="form.data_type === STRUCTURED_FIELD_TYPE" label="结构定义" required>
            <el-input
              v-model="form.structure_schema_text"
              type="textarea"
              :rows="10"
              placeholder='例如：{"type":"object","properties":{"付款期限":{"type":"string"}},"required":["付款期限"],"additionalProperties":false}'
            />
          </el-form-item>
          <el-form-item label="单位">
            <el-input v-model="form.unit" maxlength="40" placeholder="按规则库填写（如：件、%、元）；不填写表示未声明" />
          </el-form-item>
          <el-form-item label="币种">
            <el-input v-model="form.currency" maxlength="40" placeholder="金额类字段建议填写（如：人民币）；不填写表示未声明，系统不默认币种" />
          </el-form-item>
          <el-form-item label="取数期间">
            <el-select v-model="form.period_scope" filterable allow-create clearable style="width: 100%" placeholder="不涉及时留空">
              <el-option v-for="item in periodScopeOptions" :key="item.value" :value="item.value" :label="item.label" />
            </el-select>
          </el-form-item>
          <el-form-item label="报表口径">
            <el-select v-model="form.statement_scope" filterable allow-create clearable style="width: 100%" placeholder="不涉及时留空">
              <el-option v-for="item in statementScopeOptions" :key="item.value" :value="item.value" :label="item.label" />
            </el-select>
          </el-form-item>
          <el-form-item label="产物来源">
            <el-select v-model="form.artifact_source" clearable style="width: 100%" placeholder="留空表示由常规字段抽取提供">
              <el-option
                v-for="item in artifactSourceOptions"
                :key="item.value"
                :value="item.value"
                :label="item.label"
              />
            </el-select>
          </el-form-item>
          <el-form-item label="是否必需">
            <el-radio-group v-model="form.required">
              <el-radio :value="true">必需</el-radio>
              <el-radio :value="false">可选</el-radio>
            </el-radio-group>
          </el-form-item>
          <el-form-item label="抽取要求">
            <el-input
              v-model="form.instruction"
              type="textarea"
              :rows="3"
              maxlength="4000"
              placeholder="描述从这个材料里怎么取这个字段，发布时会成为字段抽取规则"
            />
          </el-form-item>
        </el-form>
        <template #footer>
          <el-button @click="dialogVisible = false">取消</el-button>
          <el-button type="primary" :loading="saving" @click="submitForm">确定</el-button>
        </template>
      </el-dialog>

      <el-dialog :model-value="Boolean(dependencyIssue)" title="字段仍被引用，不能删除" width="600px" @close="dependencyIssue = null">
        <p>{{ dependencyIssue?.title }}</p>
        <ul class="text-muted">
          <li v-for="entry in dependencyIssue?.entries ?? []" :key="entry">{{ entry }}</li>
        </ul>
        <p class="text-hint">请先在规则配置 / 报告配置中移除这些引用，再删除该字段。</p>
        <template #footer>
          <el-button type="primary" @click="dependencyIssue = null">知道了</el-button>
        </template>
      </el-dialog>
    </template>
  </div>
</template>
