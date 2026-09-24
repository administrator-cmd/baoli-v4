<script setup lang="ts">
import { computed, reactive, ref, watch } from "vue";
import { ElMessage, ElMessageBox } from "element-plus";

import type { DraftFieldItem, DraftMaterialItem, ScenarioDraftView } from "@/api/contracts";
import { ApiError } from "@/api/http";
import {
  createDraftField,
  deleteDraftField,
  getSceneDraft,
  updateDraftField,
} from "@/api/scenes";
import {
  artifactSourceLabel,
  artifactSourceOptions,
  periodScopeLabel,
  periodScopeOptions,
  statementScopeLabel,
  statementScopeOptions,
  valueTypeLabel,
  valueTypeOptions,
} from "@/constants/labels";

/**
 * 字段配置面板：以场景 + 版本为作用域。
 * 字段属于某个材料位；抽取要求完整可编辑（表格只展示一行，详情在弹窗里）。
 * 稳定标识在内部维护，名称不是主键：改名不影响已发布版本与任务。
 */
const props = defineProps<{
  scenarioCode: string | null;
  /** 由父页面触发的外部刷新（例如发布后）。 */
  refreshToken?: number;
}>();

const emit = defineEmits<{
  changed: [];
}>();

const fields = ref<DraftFieldItem[]>([]);
const materials = ref<DraftMaterialItem[]>([]);
const draft = ref<ScenarioDraftView | null>(null);
const loading = ref(false);
const loadError = ref<string | null>(null);
const saving = ref(false);

const canEdit = computed(() => Boolean(props.scenarioCode));

const query = reactive({ material_key: "", name: "" });
const filteredFields = computed(() =>
  fields.value.filter(
    (item) =>
      (!query.material_key || item.material_key === query.material_key) &&
      (!query.name.trim() || item.name.includes(query.name.trim())),
  ),
);

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
  value_type: "STRING",
  unit: "",
  currency: "",
  artifact_source: "",
  // 取数期间与报表口径：计算参数按这两项定位字段事实。
  // 默认「不适用」——合同场景的字段本来就没有期间与报表口径的概念；
  // 财报场景要按实际口径改选，被计算引用的字段声明成「不适用」时发布期会明确拦下。
  period_scope: "NOT_APPLICABLE",
  statement_scope: "NOT_APPLICABLE",
  required: false,
  instruction: "",
  // 业务编号（Excel 的「字段编号」列）：导出规则库时写回该列，导入时必填。
  source_ref: "",
});

const dependencyIssue = ref<{ title: string; entries: string[] } | null>(null);

async function load(): Promise<void> {
  if (!props.scenarioCode) {
    fields.value = [];
    materials.value = [];
    draft.value = null;
    return;
  }
  loading.value = true;
  loadError.value = null;
  try {
    const payload = await getSceneDraft(props.scenarioCode);
    draft.value = payload;
    fields.value = payload.fields;
    materials.value = payload.materials;
  } catch (error) {
    fields.value = [];
    materials.value = [];
    loadError.value = error instanceof Error ? error.message : "字段配置读取失败";
  } finally {
    loading.value = false;
  }
}

watch(() => [props.scenarioCode, props.refreshToken], () => void load(), { immediate: true });

function openCreate(): void {
  editingItem.value = null;
  form.material_key = materials.value[0]?.material_key ?? "";
  form.name = "";
  form.value_type = "STRING";
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
  form.value_type = item.value_type;
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

function describeError(error: unknown): string {
  return error instanceof Error ? error.message : "操作失败";
}

async function submitForm(): Promise<void> {
  if (!props.scenarioCode || !form.name.trim() || !form.material_key) {
    ElMessage.warning("请选择所属材料并填写字段名称");
    return;
  }
  saving.value = true;
  try {
    const payload = {
      material_key: form.material_key,
      name: form.name.trim(),
      value_type: form.value_type,
      unit: form.unit.trim() ? form.unit.trim() : null,
      // 币种与产物来源：留空表示未声明 / 常规字段抽取，不表示“随便选一个”。
      currency: form.currency.trim() ? form.currency.trim() : null,
      artifact_source: form.artifact_source ? form.artifact_source : null,
      // 取数期间与报表口径必须显式提交：服务端不代填默认值。
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
    await load();
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
      `确认删除字段「${item.name}」？删除前会检查规则与报告章节引用；历史版本不受影响。`,
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
    await load();
    emit("changed");
  } catch (error) {
    if (error instanceof ApiError && error.code === "FIELD_REFERENCED") {
      const details = error.details as Record<string, unknown>;
      const rules = Array.isArray(details.rule_keys) ? (details.rule_keys as string[]) : [];
      const sections = Array.isArray(details.section_names) ? (details.section_names as string[]) : [];
      dependencyIssue.value = {
        title: error.message,
        entries: [...rules.map((item) => `规则：${item}`), ...sections.map((item) => `报告章节：${item}`)],
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
        <el-form-item label="字段名称">
          <el-input v-model="query.name" clearable placeholder="请输入字段名称" style="width: 200px" />
        </el-form-item>
      </el-form>

      <div class="app-toolbar">
        <div class="app-toolbar__left">
          <el-button v-if="canEdit" type="primary" :disabled="!materials.length" @click="openCreate">新增字段</el-button>
          <span v-if="canEdit && !materials.length" class="text-hint">先在材料配置中建立材料位，再添加字段。</span>
        </div>
        <div class="app-toolbar__right">
          <el-button :loading="loading" @click="load">刷新</el-button>
        </div>
      </div>

      <el-alert v-if="loadError" type="error" show-icon :closable="false" :title="`读取失败：${loadError}`">
        <el-button size="small" @click="load">重试</el-button>
      </el-alert>

      <el-table v-else :data="filteredFields" border stripe v-loading="loading" empty-text="暂无字段配置">
        <el-table-column type="index" label="序号" width="60" />
        <el-table-column label="字段 key" width="190">
          <template #default="{ row }"><span class="mono">{{ row.field_key }}</span></template>
        </el-table-column>
        <el-table-column prop="source_ref" label="字段编号" width="120">
          <template #default="{ row }">
            <span v-if="row.source_ref" class="mono">{{ row.source_ref }}</span>
            <span v-else class="text-hint">未填写</span>
          </template>
        </el-table-column>
        <el-table-column prop="name" label="字段名称" min-width="170" show-overflow-tooltip />
        <el-table-column prop="material_name" label="来源材料" width="170" show-overflow-tooltip />
        <el-table-column label="数据类型" width="100">
          <template #default="{ row }">{{ valueTypeLabel(row.value_type) }}</template>
        </el-table-column>
        <el-table-column label="单位" width="80">
          <template #default="{ row }">{{ row.unit || "—" }}</template>
        </el-table-column>
        <el-table-column label="币种" width="80">
          <template #default="{ row }">{{ row.currency || "—" }}</template>
        </el-table-column>
        <el-table-column label="取数期间" width="120">
          <template #default="{ row }">{{ periodScopeLabel(row.period_scope) }}</template>
        </el-table-column>
        <el-table-column label="报表口径" width="120">
          <template #default="{ row }">{{ statementScopeLabel(row.statement_scope) }}</template>
        </el-table-column>
        <el-table-column label="产物来源" width="110">
          <template #default="{ row }">{{ artifactSourceLabel(row.artifact_source) }}</template>
        </el-table-column>
        <el-table-column label="是否必填" width="100">
          <template #default="{ row }">{{ row.required ? "是" : "否" }}</template>
        </el-table-column>
        <el-table-column label="抽取方式" min-width="200" show-overflow-tooltip>
          <template #default="{ row }">{{ row.instruction || "—" }}</template>
        </el-table-column>
        <el-table-column label="是否被规则引用" width="140">
          <template #default="{ row }">
            <el-tag v-if="referencedFieldKeys.has(row.field_key)" type="info" size="small" effect="plain" disable-transitions>
              已被引用
            </el-tag>
            <span v-else class="text-hint">未引用</span>
          </template>
        </el-table-column>
        <el-table-column v-if="canEdit" label="操作" width="140" fixed="right">
          <template #default="{ row }">
            <el-button link type="primary" size="small" @click="openEdit(row)">编辑</el-button>
            <el-button link type="danger" size="small" @click="removeItem(row)">删除</el-button>
          </template>
        </el-table-column>
      </el-table>

      <el-dialog v-model="dialogVisible" :title="editingItem ? '编辑字段' : '新增字段'" width="600px" :close-on-click-modal="false">
        <el-form label-width="100px">
          <el-form-item label="所属材料" required>
            <el-select v-model="form.material_key" placeholder="请选择材料位" style="width: 100%">
              <el-option v-for="item in materials" :key="item.material_key" :value="item.material_key" :label="item.name" />
            </el-select>
          </el-form-item>
          <el-form-item label="字段编号">
            <el-input v-model="form.source_ref" maxlength="80" placeholder="例如：FD-001；导出规则库时写回「字段编号」列" />
            <div class="text-hint">从 Excel 导入的字段会带回原编号；页面新建的请补一个，否则导出会因缺少必填编号被拒绝。</div>
          </el-form-item>
          <el-form-item label="字段名称" required>
            <el-input v-model="form.name" maxlength="255" placeholder="例如：应收账款金额" />
          </el-form-item>
          <el-form-item label="字段类型" required>
            <el-select v-model="form.value_type" style="width: 100%">
              <el-option v-for="item in valueTypeOptions" :key="item.value" :value="item.value" :label="item.label" />
            </el-select>
          </el-form-item>
          <el-form-item label="单位">
            <el-input v-model="form.unit" maxlength="40" placeholder="金额类字段建议填写（如：元）；不填写表示未声明" />
          </el-form-item>
          <el-form-item label="币种">
            <el-input v-model="form.currency" maxlength="40" placeholder="金额类字段建议填写（如：人民币）；不填写表示未声明，系统不默认币种" />
          </el-form-item>
          <el-form-item label="取数期间">
            <el-select v-model="form.period_scope" style="width: 100%">
              <el-option v-for="item in periodScopeOptions" :key="item.value" :value="item.value" :label="item.label" />
            </el-select>
          </el-form-item>
          <el-form-item label="报表口径">
            <el-select v-model="form.statement_scope" style="width: 100%">
              <el-option v-for="item in statementScopeOptions" :key="item.value" :value="item.value" :label="item.label" />
            </el-select>
            <div class="text-hint">
              合同场景的字段没有期间与报表口径，选「不适用」即可。被计算公式引用的字段不能是「不适用」，
              发布时会明确拦下——计算参数按这两项定位字段事实。
            </div>
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
            <div class="text-hint">选择「印章识别」表示该字段取自印章识别产物；主体核对按这条声明确定待核对主体，不靠字段名猜。</div>
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
