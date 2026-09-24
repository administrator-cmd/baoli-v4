<script setup lang="ts">
import { computed, reactive, ref, watch } from "vue";
import { ElMessage, ElMessageBox } from "element-plus";

import type {
  DraftFieldItem,
  DraftMaterialItem,
  DraftSectionItem,
  ScenarioDraftView,
} from "@/api/contracts";
import {
  createDraftSection,
  deleteDraftSection,
  getSceneDraft,
  updateDraftSection,
} from "@/api/scenes";
import { reportContentTypeOptions, reportFormatOptions, reportResultSortOptions } from "@/constants/labels";

/**
 * 报告配置面板。
 *
 * 两种语义并存，由章节自带的报告编号区分：
 *   - 节点版（contract_review_v3）：一行一个章节，同一报告编号组成一份报告，
 *     带报告名称、材料范围、输出格式、章节顺序、内容类型、结果排序与配置状态；
 *   - 旧格式：没有报告编号，保持原有的「附加展示章节」语义。
 * 固定事实报告由系统保证，不在这里配置；内容要求只作为展示说明，
 * 不代表系统会生成自由正文。
 */
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
const draft = ref<ScenarioDraftView | null>(null);
const loading = ref(false);
const loadError = ref<string | null>(null);
const saving = ref(false);

const canEdit = computed(() => Boolean(props.scenarioCode));

const dialogVisible = ref(false);
const editingItem = ref<DraftSectionItem | null>(null);
const form = reactive({
  title: "",
  content_instruction: "",
  field_keys: [] as string[],
  // 节点版的报告分组属性。没有报告编号时这些键全部留空，章节按旧语义处理。
  report_code: "",
  report_name: "",
  material_keys: [] as string[],
  formats: [] as string[],
  section_order: null as number | null,
  content_type: "",
  result_sort: "",
  // 是否生效：与 Excel 的「是否生效」同一口径。业务只表达「这一行参不参与输出」，
  // 不表达「系统配没配好」——后者是系统内部状态。
  enabled: true,
});

const fieldOptions = computed(() =>
  fields.value.map((item) => ({
    value: item.field_key,
    label: `${item.material_name ?? "未知材料"}·${item.name}`,
  })),
);

const materialOptions = computed(() =>
  materials.value.map((item) => ({ value: item.material_key, label: item.name })),
);

async function load(): Promise<void> {
  if (!props.scenarioCode) {
    sections.value = [];
    fields.value = [];
    return;
  }
  loading.value = true;
  loadError.value = null;
  try {
    const payload = await getSceneDraft(props.scenarioCode);
    draft.value = payload;
    sections.value = payload.sections;
    fields.value = payload.fields;
    materials.value = payload.materials;
  } catch (error) {
    sections.value = [];
    loadError.value = error instanceof Error ? error.message : "报告配置读取失败";
  } finally {
    loading.value = false;
  }
}

watch(() => [props.scenarioCode, props.refreshToken], () => void load(), { immediate: true });

function openCreate(): void {
  editingItem.value = null;
  form.title = "";
  form.content_instruction = "";
  form.field_keys = [];
  form.report_code = "";
  form.report_name = "";
  form.material_keys = [];
  form.formats = [];
  form.section_order = null;
  form.content_type = "";
  form.result_sort = "";
  form.enabled = true;
  dialogVisible.value = true;
}

function openEdit(item: DraftSectionItem): void {
  editingItem.value = item;
  form.title = item.title;
  form.content_instruction = item.content_instruction;
  form.field_keys = [...item.field_keys];
  form.report_code = item.report_code ?? "";
  form.report_name = item.report_name ?? "";
  form.material_keys = [...(item.material_keys ?? [])];
  form.formats = [...(item.formats ?? [])];
  form.section_order = item.section_order ?? null;
  form.content_type = item.content_type ?? "";
  form.result_sort = item.result_sort ?? "";
  form.enabled = item.config_status !== "DISABLED";
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
      field_keys: form.field_keys,
      // 报告编号留空即视为旧格式的附加章节：这部分键如实提交为空，
      // 不替业务补一个编号，也不沿用上一份报告的分组属性。
      report_code: form.report_code.trim() || null,
      report_name: form.report_name.trim() || null,
      material_keys: form.material_keys,
      formats: form.formats,
      section_order: form.section_order,
      content_type: form.content_type || null,
      result_sort: form.result_sort || null,
      // 内部配置状态由「是否生效」映射：不生效 → DISABLED，生效 → CLEAR。
      config_status: form.enabled ? "CLEAR" : "DISABLED",
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
          <span class="text-hint">固定事实报告由系统始终生成；这里配置的是附加展示章节。</span>
        </div>
        <div class="app-toolbar__right">
          <el-button :loading="loading" @click="load">刷新</el-button>
        </div>
      </div>

      <el-alert v-if="loadError" type="error" show-icon :closable="false" :title="`读取失败：${loadError}`">
        <el-button size="small" @click="load">重试</el-button>
      </el-alert>

      <el-table v-else :data="sections" border stripe v-loading="loading" empty-text="暂无附加章节（固定事实报告仍会生成）">
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
        <el-table-column label="绑定字段" min-width="260">
          <template #default="{ row }">
            <span v-if="row.field_names.length">{{ row.field_names.join("、") }}</span>
            <span v-else class="text-hint">未绑定字段（本章节不生成额外正文）</span>
          </template>
        </el-table-column>
        <el-table-column prop="content_instruction" label="内容要求" min-width="220" show-overflow-tooltip>
          <template #default="{ row }">{{ row.content_instruction || "—" }}</template>
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
          <el-form-item label="绑定字段">
            <el-select v-model="form.field_keys" multiple filterable placeholder="选择要展示的字段（可留空）" style="width: 100%">
              <el-option v-for="item in fieldOptions" :key="item.value" :value="item.value" :label="item.label" />
            </el-select>
          </el-form-item>
          <el-form-item label="内容要求">
            <el-input
              v-model="form.content_instruction"
              type="textarea"
              :rows="3"
              maxlength="4000"
              placeholder="展示说明（报告只做字段的确定性展示，不代表会生成自由正文）"
            />
          </el-form-item>

          <h4 class="app-section-title">报告分组（节点版格式；留空表示旧格式的附加章节）</h4>
          <el-form-item label="报告编号">
            <el-input v-model="form.report_code" maxlength="80" style="width: 220px" placeholder="例如：RPT-001" />
            <span class="text-hint" style="margin-left: 8px">同一编号的章节组成一份报告</span>
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
            <span class="text-hint" style="margin-left: 8px">同一报告内唯一的正整数</span>
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
          <el-form-item label="是否生效">
            <el-switch v-model="form.enabled" active-text="生效" inactive-text="不生效" />
            <span class="text-hint" style="margin-left: 8px">不生效的章节仍然保留，只是不进入报告输出</span>
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
