<script setup lang="ts">
import { computed, reactive, ref, watch } from "vue";
import { ElMessage, ElMessageBox } from "element-plus";

import type {
  DraftFieldItem,
  DraftMaterialItem,
  DraftSectionItem,
  ScenarioConfigurationView,
} from "@/api/contracts";
import {
  createDraftSection,
  deleteDraftSection,
  getSceneConfiguration,
  updateDraftSection,
} from "@/api/scenes";
import { reportContentTypeOptions, reportFormatOptions, reportResultSortOptions } from "@/constants/labels";

/** 场景章节控制报告正文，材料范围与排序由配置确定。 */
const props = defineProps<{
  scenarioCode: string | null;
  /** 由父页面触发的外部刷新（例如发布后）。 */
  refreshToken?: number;
}>();

const emit = defineEmits<{
  changed: [];
}>();

const sections = ref<DraftSectionItem[]>([]);
const fields = ref<DraftFieldItem[]>([]);
const materials = ref<DraftMaterialItem[]>([]);
const draft = ref<ScenarioConfigurationView | null>(null);
const loading = ref(false);
const loadError = ref<string | null>(null);
const saving = ref(false);
let loadSequence = 0;

const canEdit = computed(() => Boolean(props.scenarioCode));

const dialogVisible = ref(false);
const editingItem = ref<DraftSectionItem | null>(null);
const form = reactive({
  title: "",
  content_instruction: "",
  data_refs: [] as string[],
  // 报告分组属性；未声明编号时由规则库内部身份组织章节。
  report_code: "",
  report_name: "",
  material_keys: [] as string[],
  formats: [] as string[],
  section_order: null as number | null,
  content_type: "",
  result_sort: "",
  risk_level_order_text: "",
  config_status: "CLEAR",
});

const dataOptions = computed(() => {
  const options = [
    ...fields.value.map((item) => ({ value: item.field_key, label: `字段：${item.material_name ?? "未知材料"}·${item.name}` })),
    ...(draft.value?.calculations ?? []).filter((item) => item.result_code).map((item) => ({
      value: item.result_code!, label: `计算结果：${item.name ?? item.result_code}`,
    })),
    ...(draft.value?.rules ?? []).filter((item) => !item.system).map((item) => ({
      value: item.rule_key, label: `规则结论：${item.name}`,
    })),
  ];
  const known = new Set(options.map((item) => item.value));
  for (const ref of form.data_refs) {
    if (!known.has(ref)) options.push({ value: ref, label: `未明确的引用：${ref}` });
  }
  return options;
});

const materialOptions = computed(() =>
  materials.value.map((item) => ({ value: item.material_key, label: item.name })),
);

async function load(): Promise<void> {
  const sequence = ++loadSequence;
  if (!props.scenarioCode) {
    sections.value = [];
    fields.value = [];
    return;
  }
  loading.value = true;
  loadError.value = null;
  try {
    const payload = await getSceneConfiguration(props.scenarioCode);
    if (sequence !== loadSequence) return;
    draft.value = payload;
    sections.value = payload.sections;
    fields.value = payload.fields;
    materials.value = payload.materials;
  } catch (error) {
    if (sequence !== loadSequence) return;
    sections.value = [];
    loadError.value = error instanceof Error ? error.message : "报告配置读取失败";
  } finally {
    if (sequence === loadSequence) loading.value = false;
  }
}

watch(() => [props.scenarioCode, props.refreshToken], () => void load(), { immediate: true });

function openCreate(): void {
  editingItem.value = null;
  form.title = "";
  form.content_instruction = "";
  form.data_refs = [];
  form.report_code = "";
  form.report_name = "";
  form.material_keys = [];
  form.formats = [];
  form.section_order = null;
  form.content_type = "";
  form.result_sort = "";
  form.risk_level_order_text = "";
  form.config_status = "CLEAR";
  dialogVisible.value = true;
}

function openEdit(item: DraftSectionItem): void {
  editingItem.value = item;
  form.title = item.title;
  form.content_instruction = item.content_instruction;
  form.data_refs = [...item.data_refs];
  form.report_code = item.report_code ?? "";
  form.report_name = item.report_name ?? "";
  form.material_keys = [...(item.material_keys ?? [])];
  form.formats = [...(item.formats ?? [])];
  form.section_order = item.section_order ?? null;
  form.content_type = item.content_type ?? "";
  form.result_sort = item.result_sort ?? "";
  form.risk_level_order_text = (item.risk_level_order ?? []).join("\n");
  form.config_status = item.config_status ?? "CLEAR";
  dialogVisible.value = true;
}

async function submitForm(): Promise<void> {
  if (!props.scenarioCode || !form.title.trim()) {
    ElMessage.warning("请填写章节名称");
    return;
  }
  saving.value = true;
  try {
    const payload = {
      title: form.title.trim(),
      content_instruction: form.content_instruction,
      data_refs: form.data_refs,
      // 未声明的报告属性如实提交为空，
      // 不替业务补一个编号，也不沿用上一份报告的分组属性。
      report_code: form.report_code.trim() || null,
      report_name: form.report_name.trim() || null,
      material_keys: form.material_keys,
      formats: form.formats,
      section_order: form.section_order,
      content_type: form.content_type || null,
      result_sort: form.result_sort || null,
      risk_level_order: form.risk_level_order_text.split(/\r?\n/).map((value) => value.trim()).filter(Boolean),
      config_status: form.config_status,
    };
    if (editingItem.value) {
      await updateDraftSection(props.scenarioCode, editingItem.value.section_key, payload);
      ElMessage.success("报告章节已保存");
    } else {
      await createDraftSection(props.scenarioCode, payload);
      ElMessage.success("报告章节已新增");
    }
    dialogVisible.value = false;
    await load();
    emit("changed");
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : "操作失败");
  } finally {
    saving.value = false;
  }
}

async function removeItem(item: DraftSectionItem): Promise<void> {
  if (!props.scenarioCode) {
    return;
  }
  try {
    await ElMessageBox.confirm(`确认删除报告章节「${item.title}」？`, "删除报告章节", {
      type: "warning",
      confirmButtonText: "删除",
      cancelButtonText: "取消",
    });
  } catch {
    return;
  }
  saving.value = true;
  try {
    await deleteDraftSection(props.scenarioCode, item.section_key);
    ElMessage.success("报告章节已删除");
    await load();
    emit("changed");
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : "操作失败");
  } finally {
    saving.value = false;
  }
}
</script>

<template>
  <div>
    <el-alert v-if="!scenarioCode" type="info" show-icon :closable="false" title="请先选择场景" />

    <template v-else>
      <div class="app-toolbar">
        <div class="app-toolbar__left">
          <el-button v-if="canEdit" type="primary" @click="openCreate">新增章节</el-button>
        </div>
        <div class="app-toolbar__right">
          <el-button :loading="loading" @click="load">刷新</el-button>
        </div>
      </div>

      <el-alert v-if="loadError" type="error" show-icon :closable="false" :title="`读取失败：${loadError}`">
        <el-button size="small" @click="load">重试</el-button>
      </el-alert>

      <el-table v-else :data="sections" border stripe v-loading="loading" empty-text="暂无报告章节（系统执行摘要仍会生成）">
        <el-table-column label="报告编号" width="120">
          <template #default="{ row }">{{ row.report_code || "—" }}</template>
        </el-table-column>
        <el-table-column prop="title" label="章节名称" min-width="160" show-overflow-tooltip />
        <el-table-column label="章节顺序" width="90">
          <template #default="{ row }">{{ row.section_order ?? "—" }}</template>
        </el-table-column>
        <el-table-column label="材料范围" min-width="150" show-overflow-tooltip>
          <template #default="{ row }">
            <span v-if="(row.material_names || []).length">{{ (row.material_names || []).join("、") }}</span>
            <span v-else class="text-hint">—</span>
          </template>
        </el-table-column>
        <el-table-column label="业务数据引用" min-width="260">
          <template #default="{ row }">
            <span v-if="row.data_names.length">{{ row.data_names.join("、") }}</span>
            <span v-else class="text-hint">按章节要求选择业务数据</span>
          </template>
        </el-table-column>
        <el-table-column prop="content_instruction" label="内容要求" min-width="220" show-overflow-tooltip>
          <template #default="{ row }">{{ row.content_instruction || "—" }}</template>
        </el-table-column>
        <el-table-column label="状态" width="95">
          <template #default="{ row }">
            <el-tag size="small" :type="row.config_status === 'PENDING' ? 'warning' : row.config_status === 'DISABLED' ? 'info' : 'success'">
              {{ row.config_status === 'PENDING' ? '待确认' : row.config_status === 'DISABLED' ? '暂停' : '启用' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column v-if="canEdit" label="操作" width="140" fixed="right">
          <template #default="{ row }">
            <el-button link type="primary" size="small" @click="openEdit(row)">编辑</el-button>
            <el-button link type="danger" size="small" @click="removeItem(row)">删除</el-button>
          </template>
        </el-table-column>
      </el-table>

      <el-dialog v-model="dialogVisible" :title="editingItem ? '编辑报告章节' : '新增报告章节'" width="600px" :close-on-click-modal="false">
        <el-form label-width="100px">
          <el-form-item label="章节名称" required>
            <el-input v-model="form.title" maxlength="255" placeholder="例如：审核概况" />
          </el-form-item>
          <el-form-item label="业务数据">
            <el-select v-model="form.data_refs" multiple filterable allow-create placeholder="选择字段、计算结果或规则结论，也可填写引用" style="width: 100%">
              <el-option v-for="item in dataOptions" :key="item.value" :value="item.value" :label="item.label" />
            </el-select>
          </el-form-item>
          <el-form-item label="内容要求">
            <el-input
              v-model="form.content_instruction"
              type="textarea"
              :rows="3"
              maxlength="4000"
              placeholder="填写本章使用哪些业务数据，以及需要生成的内容"
            />
          </el-form-item>

          <h4 class="app-section-title">报告分组</h4>
          <el-form-item label="报告编号">
            <el-input v-model="form.report_code" maxlength="80" style="width: 220px" placeholder="例如：RPT-001" />
          </el-form-item>
          <el-form-item label="报告名称">
            <el-input v-model="form.report_name" maxlength="255" placeholder="例如：合同审核报告" />
          </el-form-item>
          <el-form-item label="材料范围">
            <el-select v-model="form.material_keys" multiple filterable style="width: 100%" placeholder="这份报告覆盖哪些材料（空白不表示全部）">
              <el-option v-for="item in materialOptions" :key="item.value" :value="item.value" :label="item.label" />
            </el-select>
          </el-form-item>
          <el-form-item label="输出格式">
            <el-select v-model="form.formats" multiple style="width: 100%" placeholder="选择输出格式">
              <el-option v-for="item in reportFormatOptions" :key="item.value" :value="item.value" :label="item.label" />
            </el-select>
          </el-form-item>
          <el-form-item label="章节顺序">
            <el-input-number v-model="form.section_order" :min="1" :step="1" style="width: 160px" />
          </el-form-item>
          <el-form-item label="内容类型">
            <el-select v-model="form.content_type" clearable style="width: 220px" placeholder="留空表示未声明">
              <el-option v-for="item in reportContentTypeOptions" :key="item.value" :value="item.value" :label="item.label" />
            </el-select>
          </el-form-item>
          <el-form-item label="结果排序">
            <el-select v-model="form.result_sort" clearable style="width: 260px" placeholder="留空表示未声明">
              <el-option v-for="item in reportResultSortOptions" :key="item.value" :value="item.value" :label="item.label" />
            </el-select>
          </el-form-item>
          <el-form-item label="风险等级顺序">
            <el-input v-model="form.risk_level_order_text" type="textarea" :rows="3" placeholder="按风险从高到低，每行填写一个场景等级；不排序时可留空" />
          </el-form-item>
          <el-form-item label="状态">
            <el-select v-model="form.config_status">
              <el-option value="CLEAR" label="启用" />
              <el-option value="PENDING" label="待确认（不生成）" />
              <el-option value="DISABLED" label="暂停" />
            </el-select>
          </el-form-item>
        </el-form>
        <template #footer>
          <el-button @click="dialogVisible = false">取消</el-button>
          <el-button type="primary" :loading="saving" @click="submitForm">确定</el-button>
        </template>
      </el-dialog>
    </template>
  </div>
</template>
