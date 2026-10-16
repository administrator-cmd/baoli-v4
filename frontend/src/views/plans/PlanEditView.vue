<script setup lang="ts">
import { computed, onMounted, reactive, ref, watch } from "vue";
import { useRoute, useRouter } from "vue-router";
import { ElMessage, ElMessageBox } from "element-plus";

import type { PlanAdjustments, PlanConfig, PlanRuleItem, PlanSectionItem, PlanSelection } from "@/api/contracts";
import { createPlan, getPlan, getPlanAdjustments, getPlanConfig, updatePlan, updatePlanConfig } from "@/api/plans";
import { listSceneOptions, type ScenarioOption } from "@/api/scenes";
import BackButton from "@/components/BackButton.vue";
import PageHeader from "@/components/PageHeader.vue";
import PlanBusinessEditor from "@/components/plans/PlanBusinessEditor.vue";
import PlanExtractionEditor from "@/components/plans/PlanExtractionEditor.vue";
import {
  extractionStrategyLabel,
  reportContentTypeOptions,
  reportFormatOptions,
  reportResultSortOptions,
  valueTypeLabel,
} from "@/constants/labels";

/**
 * 方案编辑：以已发布场景版本为基准的个性化配置。
 *
 * 新建 = 选一个已发布且启用的场景版本，服务端把那一版的配置（材料 / 字段 / 规则 /
 * 报告章节）整份复制过来并立即编译一次；新建时不再挑规则，规则在方案里逐条勾。
 *
 * 编辑 = 调整方案自己的规则、材料、字段和报告章节。
 */
const route = useRoute();
const router = useRouter();

const planCode = computed(() => (typeof route.params.planCode === "string" ? route.params.planCode : null));
const isCreate = computed(() => planCode.value === null);
const previousCode = computed(() => isCreate.value && typeof route.query.previous === "string" ? route.query.previous : null);
const requestedSceneCode = computed(() => isCreate.value && typeof route.query.scenario === "string" ? route.query.scenario : null);
const PLAN_TAB_NAMES = ["basic", "materials", "fields", "rules", "calculations", "sections"];
const activeTab = computed({
  get: () => {
    const requested = route.query.tab;
    return typeof requested === "string" && PLAN_TAB_NAMES.includes(requested) ? requested : "basic";
  },
  set: (value: string) => {
    void router.replace({ query: { ...route.query, tab: value } });
  },
});

const scenes = ref<ScenarioOption[]>([]);
const scenesError = ref<string | null>(null);
const scenesLoading = ref(false);
const plan = ref<PlanSelection | null>(null);
const config = ref<PlanConfig | null>(null);
const businessEditorVisible = ref(false);
const businessEditorKind = ref<"rule" | "calculation">("rule");
const businessEditorKey = ref("");
const extractionEditorVisible = ref(false);
const extractionEditorKind = ref<"material" | "field">("material");
const extractionEditorKey = ref("");

function editBusiness(kind: "rule" | "calculation", key: string): void {
  businessEditorKind.value = kind;
  businessEditorKey.value = key;
  businessEditorVisible.value = true;
}
function editExtraction(kind: "material" | "field", key: string): void {
  extractionEditorKind.value = kind;
  extractionEditorKey.value = key;
  extractionEditorVisible.value = true;
}

function statusLabel(status: unknown, enabled?: boolean): string {
  if (status === "PENDING") return "待确认";
  if (status === "DISABLED" || enabled === false) return "暂停";
  if (status === "CLEAR" || enabled === true) return "启用";
  return typeof status === "string" && status ? status : "未声明";
}

function statusTone(status: unknown, enabled?: boolean): "success" | "warning" | "info" {
  const label = statusLabel(status, enabled);
  return label === "启用" ? "success" : label === "待确认" ? "warning" : "info";
}
const savedConfigJson = ref("");
const loading = ref(false);
const loadError = ref<string | null>(null);
const planStatusError = ref<string | null>(null);
const saving = ref(false);
const reference = ref<PlanAdjustments | null>(null);
const referenceLoading = ref(false);
const referenceError = ref<string | null>(null);
let loadRequest = 0;
let referenceRequest = 0;

const basicForm = reactive({ plan_name: "", scenario_code: "", description: "", enabled: true });
const hasUnsavedBasic = computed(() => plan.value !== null && (
  basicForm.plan_name.trim() !== plan.value.plan_name
  || (basicForm.description.trim() || null) !== plan.value.description
  || basicForm.enabled !== plan.value.enabled
));
const hasUnsavedConfig = computed(() => config.value !== null && JSON.stringify(config.value) !== savedConfigJson.value);
const canCreateTask = computed(() => Boolean(
  plan.value?.enabled && plan.value.effective && !plan.value.deleted
  && !hasUnsavedBasic.value && !hasUnsavedConfig.value && !planStatusError.value && !saving.value,
));
const sectionDialogVisible = ref(false);
const editingSectionKey = ref<string | null>(null);
const sectionForm = reactive({
  title: "",
  content_instruction: "",
  data_refs: [] as string[],
  report_code: "",
  report_name: "",
  material_keys: [] as string[],
  formats: [] as string[],
  section_order: null as number | null,
  content_type: "",
  result_sort: "",
  risk_level_order_text: "",
  config_status: null as string | null,
});
const dataOptions = computed(() => {
  const options = [
    ...(config.value?.fields ?? []).map((field) => ({
      value: field.field_key, label: `字段：${config.value?.materials.find((material) => material.material_key === field.material_key)?.name ?? "未知材料"} · ${field.name}`,
    })),
    ...(config.value?.calculation_rules ?? []).map((item) => ({
      value: item.result_code, label: `计算结果：${item.rule_name}`,
    })),
    ...(config.value?.rules ?? []).filter((item) => !item.is_system && !item.rule_json.calculation_ref).map((item) => ({
      value: item.rule_key, label: `规则结论：${item.name}`,
    })),
  ];
  const known = new Set(options.map((item) => item.value));
  const refs = [...sectionForm.data_refs, ...(config.value?.sections ?? []).flatMap((item) => item.data_refs)];
  for (const ref of refs) {
    if (!known.has(ref)) {
      options.push({ value: ref, label: `未明确的引用：${ref}` });
      known.add(ref);
    }
  }
  return options;
});

const selectedScene = computed(() => scenes.value.find((item) => item.scenario_code === basicForm.scenario_code) ?? null);

const scenesUsable = computed(() => scenes.value.filter((item) => item.available !== false));

async function loadScenes(): Promise<void> {
  scenesLoading.value = true;
  scenesError.value = null;
  try {
    const payload = await listSceneOptions();
    scenes.value = payload.items;
  } catch (error) {
    scenes.value = [];
    scenesError.value = describeError(error);
  } finally {
    scenesLoading.value = false;
  }
}

async function load(): Promise<void> {
  const code = planCode.value;
  const request = ++loadRequest;
  if (!code) {
    plan.value = null;
    config.value = null;
    savedConfigJson.value = "";
    loading.value = false;
    loadError.value = null;
    planStatusError.value = null;
    basicForm.plan_name = "";
    basicForm.scenario_code = requestedSceneCode.value ?? "";
    basicForm.description = "";
    basicForm.enabled = true;
    return;
  }
  loading.value = true;
  loadError.value = null;
  planStatusError.value = null;
  plan.value = null;
  config.value = null;
  savedConfigJson.value = "";
  basicForm.plan_name = "";
  basicForm.scenario_code = "";
  basicForm.description = "";
  basicForm.enabled = true;
  try {
    const [detail, planConfig] = await Promise.all([getPlan(code), getPlanConfig(code)]);
    if (request !== loadRequest || planCode.value !== code) return;
    plan.value = detail;
    config.value = planConfig;
    savedConfigJson.value = JSON.stringify(planConfig);
    basicForm.plan_name = detail.plan_name;
    basicForm.scenario_code = detail.scenario_code;
    basicForm.description = detail.description ?? "";
    basicForm.enabled = detail.enabled;
  } catch (error) {
    if (request !== loadRequest || planCode.value !== code) return;
    plan.value = null;
    config.value = null;
    savedConfigJson.value = "";
    loadError.value = error instanceof Error ? error.message : "方案读取失败";
  } finally {
    if (request === loadRequest) loading.value = false;
  }
}

watch(() => planCode.value, () => void load(), { immediate: true });
watch(previousCode, () => void loadReference(), { immediate: true });
watch([previousCode, requestedSceneCode], ([previous, code]) => {
  if (!previous) basicForm.scenario_code = code ?? "";
}, { immediate: true });
onMounted(() => void loadScenes());

async function loadReference(): Promise<void> {
  const code = previousCode.value;
  const request = ++referenceRequest;
  reference.value = null;
  referenceError.value = null;
  if (!code) {
    referenceLoading.value = false;
    return;
  }
  basicForm.scenario_code = "";
  referenceLoading.value = true;
  try {
    const result = await getPlanAdjustments(code);
    if (request !== referenceRequest || previousCode.value !== code) {
      return;
    }
    reference.value = result;
    basicForm.scenario_code = result.scenario_code;
  } catch (error) {
    if (request === referenceRequest && previousCode.value === code) {
      referenceError.value = describeError(error);
    }
  } finally {
    if (request === referenceRequest) {
      referenceLoading.value = false;
    }
  }
}

function describeError(error: unknown): string {
  return error instanceof Error ? error.message : "操作失败";
}

function openCreateTask(): void {
  if (!plan.value || !canCreateTask.value) return;
  void router.push({ name: "audit-tasks", query: { createPlan: plan.value.plan_code } });
}

async function submitCreate(): Promise<void> {
  const createRoute = route.fullPath;
  const scene = selectedScene.value;
  if (previousCode.value && (!reference.value || referenceLoading.value || referenceError.value)) {
    ElMessage.warning("旧方案调整尚未读取成功");
    return;
  }
  if (!basicForm.plan_name.trim() || !scene || scene.available === false) {
    // 下拉里只列「已启用且已有已发布版本」的场景，选不到就是还不具备建方案的条件。
    ElMessage.warning("请填写方案名称，并选择一个已发布且已启用的场景");
    return;
  }
  saving.value = true;
  try {
    const created = await createPlan({
      plan_name: basicForm.plan_name.trim(),
      scenario_code: scene.scenario_code,
      scenario_version_no: scene.latest_version_no,
      description: basicForm.description.trim() || null,
      enabled: basicForm.enabled,
    });
    if (route.fullPath !== createRoute) return;
    ElMessage.success("方案已创建，配置已从场景版本复制");
    await router.replace({ name: "plan-edit", params: { planCode: created.plan_code } });
  } catch (error) {
    if (route.fullPath === createRoute) ElMessage.error(describeError(error));
  } finally {
    saving.value = false;
  }
}

async function saveBasic(): Promise<boolean> {
  const code = planCode.value;
  const request = loadRequest;
  if (!code || loading.value || !plan.value) {
    return false;
  }
  saving.value = true;
  try {
    const updated = await updatePlan(code, {
      plan_name: basicForm.plan_name.trim(),
      description: basicForm.description.trim() || null,
      enabled: basicForm.enabled,
    });
    if (request !== loadRequest || planCode.value !== code) return false;
    plan.value = updated;
    planStatusError.value = null;
    ElMessage.success("基本资料已保存");
    return true;
  } catch (error) {
    if (request === loadRequest && planCode.value === code) ElMessage.error(describeError(error));
    return false;
  } finally {
    saving.value = false;
  }
}

async function refreshPlanStatus(): Promise<PlanSelection | null> {
  const code = planCode.value;
  const request = loadRequest;
  if (!code) return null;
  try {
    const detail = await getPlan(code);
    if (request !== loadRequest || planCode.value !== code) return null;
    plan.value = detail;
    planStatusError.value = null;
    return detail;
  } catch (error) {
    if (request === loadRequest && planCode.value === code) {
      planStatusError.value = describeError(error);
    }
    return null;
  }
}

async function saveConfig(): Promise<void> {
  const code = planCode.value;
  const request = loadRequest;
  const currentConfig = config.value;
  if (!code || !currentConfig || saving.value) {
    return;
  }
  const submittedConfigJson = JSON.stringify(currentConfig);
  saving.value = true;
  try {
    // 整体提交完整业务配置，计算定义与启停一起保存。
    const updated = await updatePlanConfig(code, {
      materials: currentConfig.materials,
      fields: currentConfig.fields,
      rules: currentConfig.rules,
      sections: currentConfig.sections,
      calculation_rules: currentConfig.calculation_rules,
    });
    if (request !== loadRequest || planCode.value !== code) return;
    const editedDuringSave = JSON.stringify(currentConfig) !== submittedConfigJson;
    savedConfigJson.value = JSON.stringify(updated);
    config.value = editedDuringSave ? currentConfig : updated;
    const refreshedPlan = await refreshPlanStatus();
    if (request !== loadRequest || planCode.value !== code) return;
    if (!refreshedPlan) return;
    if (editedDuringSave) {
      ElMessage.warning("本次配置已保存，保存期间的新修改仍待保存");
    } else if (refreshedPlan.effective) {
      ElMessage.success("方案配置已保存并生效");
    } else {
      ElMessage.warning(`方案配置已保存，尚未生效：${refreshedPlan.sync_error?.message ?? "请核对配置"}`);
    }
  } catch (error) {
    if (request === loadRequest && planCode.value === code) ElMessage.error(describeError(error));
  } finally {
    saving.value = false;
  }
}

async function saveAll(): Promise<void> {
  if (hasUnsavedBasic.value) {
    const saved = await saveBasic();
    if (!saved) return;
  }
  if (hasUnsavedConfig.value) {
    await saveConfig();
  }
}

// 抽取、计算和报告在各自页签编辑业务定义，派生执行项不重复展示。
const businessRules = computed(() => (config.value?.rules ?? []).filter((item) => !item.is_system && !item.rule_json.calculation_ref));
const enabledRules = computed(() => businessRules.value.filter((item) => item.enabled));
const disabledRules = computed(() => businessRules.value.filter((item) => !item.enabled));

function openSectionEditor(item?: PlanSectionItem): void {
  editingSectionKey.value = item?.section_key ?? null;
  sectionForm.title = item?.title ?? "";
  sectionForm.content_instruction = item?.content_instruction ?? "";
  sectionForm.data_refs = [...(item?.data_refs ?? [])];
  sectionForm.report_code = item?.report_code ?? "";
  sectionForm.report_name = item?.report_name ?? "";
  sectionForm.material_keys = [...(item?.material_keys ?? [])];
  sectionForm.formats = [...(item?.formats ?? [])];
  sectionForm.section_order = item?.section_order ?? null;
  sectionForm.content_type = item?.content_type ?? "";
  sectionForm.result_sort = item?.result_sort ?? "";
  sectionForm.risk_level_order_text = (item?.risk_level_order ?? []).join("\n");
  sectionForm.config_status = item ? item.config_status ?? null : "PENDING";
  sectionDialogVisible.value = true;
}

function applySection(): void {
  if (!config.value || !sectionForm.title.trim()) {
    ElMessage.warning("请填写章节名称");
    return;
  }
  const existing = config.value.sections.find((item) => item.section_key === editingSectionKey.value);
  const section: PlanSectionItem = {
    section_key: existing?.section_key ?? crypto.randomUUID(),
    title: sectionForm.title.trim(),
    content_instruction: sectionForm.content_instruction,
    field_keys: sectionForm.data_refs.filter((ref) => config.value!.fields.some((field) => field.field_key === ref)),
    data_refs: [...sectionForm.data_refs],
    report_code: sectionForm.report_code.trim() || null,
    report_name: sectionForm.report_name.trim() || null,
    material_keys: [...sectionForm.material_keys],
    formats: [...sectionForm.formats],
    section_order: sectionForm.section_order,
    content_type: sectionForm.content_type || null,
    result_sort: sectionForm.result_sort || null,
    risk_level_order: sectionForm.risk_level_order_text.split(/\r?\n/).map((value) => value.trim()).filter(Boolean),
    config_status: sectionForm.config_status,
    sort_no: existing?.sort_no ?? config.value.sections.length + 1,
  };
  if (existing) {
    Object.assign(existing, section);
  } else {
    config.value.sections.push(section);
  }
  sectionDialogVisible.value = false;
}

function dataNames(refs: string[]): string {
  return refs.map((ref) => dataOptions.value.find((item) => item.value === ref)?.label ?? `未明确的引用：${ref}`).join("、") || "按章节要求选择业务数据";
}

function materialNames(keys: string[] | null | undefined): string {
  return (keys ?? []).map((key) => config.value?.materials.find((material) => material.material_key === key)?.name ?? key).join("、") || "未限定";
}

function ruleMaterialNames(rule: PlanRuleItem): string {
  const keys = rule.rule_json.materials;
  return materialNames(Array.isArray(keys) ? keys.filter((key): key is string => typeof key === "string") : []);
}

async function removeSection(item: PlanSectionItem): Promise<void> {
  if (!config.value) return;
  try {
    await ElMessageBox.confirm(`确认移除报告章节「${item.title}」？`, "移除章节", {
      type: "warning", confirmButtonText: "移除", cancelButtonText: "取消",
    });
  } catch {
    return;
  }
  config.value.sections = config.value.sections.filter((section) => section.section_key !== item.section_key);
}

function adjustmentValue(value: unknown): string {
  if (value === null || value === undefined || value === "") return "未填写";
  if (typeof value === "boolean") return value ? "是" : "否";
  if (Array.isArray(value)) return value.map(adjustmentValue).join("、") || "空列表";
  if (typeof value === "object") return JSON.stringify(value) ?? "未填写";
  return String(value);
}
</script>

<template>
  <div class="app-panel" :class="{ 'plan-editor': !isCreate }">
    <PageHeader
      eyebrow="方案管理"
      :title="isCreate ? '新增方案' : `方案编辑：${basicForm.plan_name || planCode}`"
      :show-refresh="false"
    >
      <template #actions>
        <BackButton label="返回方案列表" @click="router.push({ name: 'plans' })" />
        <el-button
          v-if="planCode"
          type="primary"
          :loading="saving"
          :disabled="loading || (!hasUnsavedBasic && !hasUnsavedConfig)"
          @click="saveAll"
        >保存修改</el-button>
        <el-button
          v-if="planCode"
          type="primary"
          :disabled="!canCreateTask"
          @click="openCreateTask"
        >创建任务</el-button>
        <el-button v-if="planCode" @click="router.push({ name: 'plan-new', query: { previous: planCode } })">基于当前场景重新生成</el-button>
      </template>
    </PageHeader>

    <el-alert v-if="loadError" type="error" show-icon :closable="false" :title="`读取失败：${loadError}`">
      <el-button size="small" @click="load">重试</el-button>
    </el-alert>
    <el-alert v-if="isCreate && scenesError" type="error" show-icon :closable="false" :title="`场景列表读取失败：${scenesError}`">
      <el-button size="small" @click="loadScenes">重试</el-button>
    </el-alert>
    <el-alert v-if="planStatusError" type="error" show-icon :closable="false" :title="`配置已保存，但方案状态读取失败：${planStatusError}`">
      <el-button size="small" @click="refreshPlanStatus">重试</el-button>
    </el-alert>
    <el-alert
      v-if="plan && (hasUnsavedBasic || hasUnsavedConfig)"
      type="warning"
      show-icon
      :closable="false"
      title="方案有未保存修改，保存后才能创建任务"
      class="block-gap-sm"
    />

    <!-- 基本信息 -->
      <el-card v-if="isCreate" shadow="never" v-loading="loading" class="block-gap-sm">
        <template #header>基本信息</template>
        <el-form label-width="100px">
          <el-form-item label="方案名称" required>
            <el-input v-model="basicForm.plan_name" maxlength="255" placeholder="例如：采购合同全流程审核方案" />
          </el-form-item>
          <el-form-item label="所属场景" required>
            <el-select
              v-model="basicForm.scenario_code"
              :disabled="!isCreate || !!previousCode"
              placeholder="选择审核场景"
              style="width: 100%"
            >
              <el-option
                v-for="item in scenesUsable"
                :key="item.scenario_code"
                :value="item.scenario_code"
                :label="`${item.scenario_name}（第 ${item.latest_version_no} 版）`"
              />
            </el-select>
          </el-form-item>
          <el-alert
            v-if="isCreate && previousCode && reference && !scenesLoading && (!selectedScene || selectedScene.available === false) && !scenesError"
            type="warning"
            show-icon
            :closable="false"
            title="该场景当前没有可用于生成方案的已发布版本"
          />
          <el-form-item label="方案说明">
            <el-input v-model="basicForm.description" type="textarea" :rows="2" maxlength="4000" />
          </el-form-item>
          <el-form-item label="启用状态">
            <el-switch v-model="basicForm.enabled" active-text="启用" inactive-text="停用" />
          </el-form-item>
        </el-form>
        <div v-if="!previousCode" class="card-actions">
          <el-button type="primary" :loading="saving" :disabled="scenesLoading || !!scenesError" @click="submitCreate">创建方案</el-button>
        </div>
      </el-card>

      <el-card v-if="previousCode" shadow="never" class="block-gap-sm" v-loading="referenceLoading">
        <template #header>旧方案调整参考</template>
        <el-alert v-if="referenceError" type="error" show-icon :closable="false" :title="`读取失败：${referenceError}`">
          <el-button size="small" @click="loadReference">重试</el-button>
        </el-alert>
        <template v-else-if="reference">
          <p class="text-hint">
            {{ reference.plan_name }} 基于场景第 {{ reference.base_scenario_version_no }} 版；
            新方案将基于当前第 {{ selectedScene?.latest_version_no ?? "—" }} 版创建。旧调整仅供参考，不会自动套用。
          </p>
          <el-table :data="reference.items" border stripe size="small" max-height="320" empty-text="旧方案没有相对来源场景的调整">
            <el-table-column prop="category" label="类别" width="100" />
            <el-table-column prop="name" label="配置项" min-width="170" show-overflow-tooltip />
            <el-table-column label="调整" width="75">
              <template #default="{ row }">{{ row.change === "ADDED" ? "新增" : row.change === "REMOVED" ? "移除" : "修改" }}</template>
            </el-table-column>
            <el-table-column label="内容" min-width="300">
              <template #default="{ row }">
                <span v-if="!row.fields.length">{{ row.change === "ADDED" ? "新增整项" : "移除整项" }}</span>
                <div v-for="field in row.fields" :key="field.label" class="adjustment-line">
                  {{ field.label }}：{{ adjustmentValue(field.before) }} → {{ adjustmentValue(field.after) }}
                </div>
              </template>
            </el-table-column>
          </el-table>
          <div class="card-actions block-gap-sm">
            <el-button type="primary" :loading="saving" :disabled="scenesLoading || !!scenesError || !selectedScene || selectedScene.available === false" @click="submitCreate">创建方案</el-button>
          </div>
        </template>
      </el-card>

      <!-- 配置：只有编辑模式才有。内容按配置对象拆成横向标签，避免长页面滚动查找。 -->
      <template v-if="!isCreate && config">
        <section class="plan-config-workspace block-gap-sm">
          <div class="plan-status-line">
            <span>范围状态</span>
            <el-tag :type="plan?.effective ? 'success' : 'warning'" size="small" effect="light">
              {{ plan?.effective ? "已生效" : "待核对" }}
            </el-tag>
            <span v-if="plan?.sync_error?.message" class="text-hint">{{ plan.sync_error.message }}</span>
            <span v-if="hasUnsavedBasic || hasUnsavedConfig" class="text-hint">有未保存修改</span>
          </div>
          <el-tabs v-model="activeTab" type="card" class="plan-tabs">
            <el-tab-pane label="基本信息" name="basic">
              <el-form label-width="100px" class="plan-basic-form">
                <el-form-item label="方案名称" required>
                  <el-input v-model="basicForm.plan_name" maxlength="255" placeholder="请输入方案名称" />
                </el-form-item>
                <el-form-item label="所属场景">
                  <span>{{ plan?.base_scenario_version?.scenario_name ?? plan?.scenario_code }}（第 {{ plan?.base_scenario_version?.version_no ?? "—" }} 版）</span>
                </el-form-item>
                <el-form-item label="方案说明">
                  <el-input v-model="basicForm.description" type="textarea" :rows="3" maxlength="4000" />
                </el-form-item>
                <el-form-item label="启用状态">
                  <el-switch v-model="basicForm.enabled" active-text="启用" inactive-text="停用" />
                </el-form-item>
              </el-form>
            </el-tab-pane>

            <el-tab-pane :label="`材料（${config.materials.length}）`" name="materials">
              <div class="plan-tab-toolbar"><span class="text-muted">共 {{ config.materials.length }} 项材料</span></div>
              <div class="plan-table-wrap">
                <el-table :data="config.materials" row-key="material_key" border stripe size="small" height="100%" empty-text="当前方案没有材料">
                  <el-table-column type="index" label="序号" width="60" />
                  <el-table-column prop="name" label="材料名称" min-width="200" show-overflow-tooltip />
                  <el-table-column prop="description" label="业务说明" min-width="300" show-overflow-tooltip />
                  <el-table-column label="必传" width="80"><template #default="{ row }">{{ row.required ? "是" : "否" }}</template></el-table-column>
                  <el-table-column label="状态" width="100"><template #default="{ row }"><el-tag :type="statusTone(row.config_status)" size="small">{{ statusLabel(row.config_status) }}</el-tag></template></el-table-column>
                  <el-table-column label="操作" width="90" fixed="right"><template #default="{ row }"><el-button link type="primary" size="small" @click="editExtraction('material', row.material_key)">编辑</el-button></template></el-table-column>
                </el-table>
              </div>
            </el-tab-pane>

            <el-tab-pane :label="`字段（${config.fields.length}）`" name="fields">
              <div class="plan-tab-toolbar"><span class="text-muted">共 {{ config.fields.length }} 个字段</span></div>
              <div class="plan-table-wrap">
                <el-table :data="config.fields" row-key="field_key" border stripe size="small" height="100%" empty-text="当前方案没有字段">
                  <el-table-column type="index" label="序号" width="60" />
                  <el-table-column prop="name" label="字段名称" min-width="180" show-overflow-tooltip />
                  <el-table-column label="所属材料" min-width="150" show-overflow-tooltip><template #default="{ row }">{{ materialNames([row.material_key]) }}</template></el-table-column>
                  <el-table-column label="数据类型" width="100"><template #default="{ row }">{{ valueTypeLabel(row.value_type) }}</template></el-table-column>
                  <el-table-column label="抽取策略" width="120"><template #default="{ row }">{{ extractionStrategyLabel(row.extraction_strategy) }}</template></el-table-column>
                  <el-table-column label="必抽" width="70"><template #default="{ row }">{{ row.required ? "是" : "否" }}</template></el-table-column>
                  <el-table-column label="状态" width="100"><template #default="{ row }"><el-tag :type="statusTone(row.config_status, row.enabled)" size="small">{{ statusLabel(row.config_status, row.enabled) }}</el-tag></template></el-table-column>
                  <el-table-column prop="instruction" label="提取要求" min-width="240" show-overflow-tooltip />
                  <el-table-column label="操作" width="90" fixed="right"><template #default="{ row }"><el-button link type="primary" size="small" @click="editExtraction('field', row.field_key)">编辑</el-button></template></el-table-column>
                </el-table>
              </div>
            </el-tab-pane>

            <el-tab-pane :label="`审核规则（${enabledRules.length} 启用）`" name="rules">
              <div class="plan-tab-toolbar"><span class="text-muted">共 {{ businessRules.length }} 条规则，已启用 {{ enabledRules.length }} 条，未启用 {{ disabledRules.length }} 条</span></div>
              <div class="plan-table-wrap">
                <el-table :data="businessRules" row-key="rule_key" border stripe size="small" height="100%" empty-text="当前方案没有审核规则">
                  <el-table-column type="index" label="序号" width="60" />
                  <el-table-column prop="name" label="规则名称" min-width="220" show-overflow-tooltip />
                  <el-table-column label="适用材料" min-width="160" show-overflow-tooltip><template #default="{ row }">{{ ruleMaterialNames(row) }}</template></el-table-column>
                  <el-table-column label="审核要求" min-width="300" show-overflow-tooltip><template #default="{ row }">{{ row.rule_json.requirement || "—" }}</template></el-table-column>
                  <el-table-column label="状态" width="100"><template #default="{ row }"><el-tag :type="statusTone(row.rule_json.config_status, row.enabled)" size="small">{{ statusLabel(row.rule_json.config_status, row.enabled) }}</el-tag></template></el-table-column>
                  <el-table-column label="操作" width="90" fixed="right"><template #default="{ row }"><el-button link type="primary" size="small" @click="editBusiness('rule', row.rule_key)">编辑</el-button></template></el-table-column>
                </el-table>
              </div>
            </el-tab-pane>

            <el-tab-pane :label="`计算规则（${config.calculation_rules.length}）`" name="calculations">
              <div class="plan-tab-toolbar"><span class="text-muted">共 {{ config.calculation_rules.length }} 条计算规则</span></div>
              <div class="plan-table-wrap">
                <el-table :data="config.calculation_rules" row-key="rule_code" border stripe size="small" height="100%" empty-text="当前方案没有计算规则">
                  <el-table-column type="index" label="序号" width="60" />
                  <el-table-column prop="rule_name" label="指标名称" min-width="220" show-overflow-tooltip />
                  <el-table-column prop="formula" label="计算方法" min-width="240" show-overflow-tooltip />
                  <el-table-column prop="requirement" label="业务要求" min-width="240" show-overflow-tooltip />
                  <el-table-column label="状态" width="100"><template #default="{ row }"><el-tag :type="statusTone(row.status, row.enabled)" size="small">{{ statusLabel(row.status, row.enabled) }}</el-tag></template></el-table-column>
                  <el-table-column label="操作" width="90" fixed="right"><template #default="{ row }"><el-button link type="primary" size="small" @click="editBusiness('calculation', row.rule_code)">编辑</el-button></template></el-table-column>
                </el-table>
              </div>
            </el-tab-pane>

            <el-tab-pane :label="`报告章节（${config.sections.length}）`" name="sections">
              <div class="plan-tab-toolbar">
                <span class="text-muted">共 {{ config.sections.length }} 个报告章节</span>
                <el-button type="primary" plain size="small" @click="openSectionEditor()">新增章节</el-button>
              </div>
              <div class="plan-table-wrap">
                <el-table :data="config.sections" row-key="section_key" border stripe size="small" height="100%" empty-text="当前方案没有报告章节">
                  <el-table-column type="index" label="序号" width="60" />
                  <el-table-column prop="title" label="章节名称" min-width="180" show-overflow-tooltip />
                  <el-table-column prop="report_name" label="所属报告" min-width="150" show-overflow-tooltip />
                  <el-table-column label="材料范围" min-width="150" show-overflow-tooltip><template #default="{ row }">{{ materialNames(row.material_keys) }}</template></el-table-column>
                  <el-table-column label="业务数据引用" min-width="220" show-overflow-tooltip><template #default="{ row }">{{ dataNames(row.data_refs) }}</template></el-table-column>
                  <el-table-column label="章节处理" width="130"><template #default="{ row }"><el-tag :type="row.config_status === 'PENDING' ? 'warning' : row.config_status === 'DISABLED' ? 'info' : 'success'" size="small">{{ row.config_status === "PENDING" ? "待确认·不生成" : row.config_status === "DISABLED" ? "不生成" : "生成" }}</el-tag></template></el-table-column>
                  <el-table-column label="操作" width="120" fixed="right"><template #default="{ row }"><el-button link type="primary" size="small" @click="openSectionEditor(row)">编辑</el-button><el-button link type="danger" size="small" @click="removeSection(row)">移除</el-button></template></el-table-column>
                </el-table>
              </div>
            </el-tab-pane>

          </el-tabs>

        </section>

        <el-dialog v-model="sectionDialogVisible" :title="editingSectionKey ? '编辑报告章节' : '新增报告章节'" width="min(680px, 96vw)" :close-on-click-modal="false">
          <el-form label-width="100px">
            <el-form-item label="章节名称" required>
              <el-input v-model="sectionForm.title" maxlength="255" />
            </el-form-item>
            <el-form-item label="业务数据">
              <el-select v-model="sectionForm.data_refs" multiple filterable allow-create placeholder="选择字段、计算结果或规则结论，也可填写引用" style="width: 100%">
                <el-option v-for="item in dataOptions" :key="item.value" :value="item.value" :label="item.label" />
              </el-select>
            </el-form-item>
            <el-form-item label="内容要求">
              <el-input v-model="sectionForm.content_instruction" type="textarea" :rows="3" />
            </el-form-item>
            <el-form-item label="报告编号">
              <el-input v-model="sectionForm.report_code" maxlength="80" />
            </el-form-item>
            <el-form-item label="报告名称">
              <el-input v-model="sectionForm.report_name" maxlength="255" />
            </el-form-item>
            <el-form-item label="材料范围">
              <el-select v-model="sectionForm.material_keys" multiple filterable style="width: 100%">
                <el-option v-for="material in config.materials" :key="material.material_key" :value="material.material_key" :label="material.name" />
              </el-select>
            </el-form-item>
            <el-form-item label="输出格式">
              <el-select v-model="sectionForm.formats" multiple style="width: 100%">
                <el-option v-for="format in reportFormatOptions" :key="format.value" :value="format.value" :label="format.label" />
              </el-select>
            </el-form-item>
            <el-form-item label="章节顺序">
              <el-input-number v-model="sectionForm.section_order" :min="1" :step="1" />
            </el-form-item>
            <el-form-item label="内容类型">
              <el-select v-model="sectionForm.content_type" clearable style="width: 100%">
                <el-option v-for="item in reportContentTypeOptions" :key="item.value" :value="item.value" :label="item.label" />
              </el-select>
            </el-form-item>
            <el-form-item label="结果排序">
              <el-select v-model="sectionForm.result_sort" clearable style="width: 100%">
                <el-option v-for="item in reportResultSortOptions" :key="item.value" :value="item.value" :label="item.label" />
              </el-select>
            </el-form-item>
            <el-form-item label="风险等级顺序">
              <el-input v-model="sectionForm.risk_level_order_text" type="textarea" :rows="3" placeholder="按风险从高到低，每行填写一个场景等级；不排序时可留空" />
            </el-form-item>
            <el-form-item label="章节处理">
              <el-select v-model="sectionForm.config_status" style="width: 100%">
                <el-option value="CLEAR" label="生成（已核对）" />
                <el-option value="PENDING" label="待确认（本章节不生成）" />
                <el-option value="DISABLED" label="不生成" />
              </el-select>
            </el-form-item>
          </el-form>
          <template #footer>
            <el-button @click="sectionDialogVisible = false">取消</el-button>
            <el-button type="primary" @click="applySection">应用修改</el-button>
          </template>
        </el-dialog>

        <PlanExtractionEditor
          v-model="extractionEditorVisible"
          :config="config"
          :kind="extractionEditorKind"
          :item-key="extractionEditorKey"
        />

        <PlanBusinessEditor
          v-if="config"
          v-model="businessEditorVisible"
          :config="config"
          :kind="businessEditorKind"
          :item-key="businessEditorKey"
        />

      </template>
  </div>
</template>

<style scoped>
.plan-editor {
  display: flex;
  flex-direction: column;
  height: calc(100dvh - var(--app-header-height) - var(--app-main-padding-y) - var(--app-main-padding-y));
}

.plan-editor > * {
  flex-shrink: 0;
}

.plan-editor > .plan-config-workspace {
  display: flex;
  flex: 1;
  flex-direction: column;
  min-height: 0;
}

.adjustment-line {
  overflow-wrap: anywhere;
}

.plan-tabs {
  display: flex;
  flex: 1;
  flex-direction: column;
  min-height: 0;
  margin-top: 8px;
}

.plan-tabs :deep(.el-tabs__header) {
  flex-shrink: 0;
  margin-bottom: 12px;
}

.plan-tabs :deep(.el-tabs__content) {
  flex: 1;
  min-height: 0;
}

.plan-tabs :deep(.el-tab-pane) {
  display: flex;
  flex-direction: column;
  height: 100%;
  min-height: 0;
  overflow-y: auto;
}

.plan-tab-toolbar {
  display: flex;
  flex-shrink: 0;
  align-items: center;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: 8px;
  min-height: 32px;
  margin-bottom: 12px;
}

.plan-table-wrap {
  position: relative;
  flex: 1;
  min-height: 0;
}

.plan-table-wrap > .el-table {
  position: absolute;
  inset: 0;
}

.plan-tabs :deep(.el-table__cell) {
  padding: 8px 0;
}

.plan-status-line {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
  font-size: 13px;
}

.plan-basic-form {
  max-width: 720px;
}

</style>
