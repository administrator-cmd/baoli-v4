<script setup lang="ts">
import { computed, onMounted, onUnmounted, reactive, ref, watch } from "vue";
import { onBeforeRouteLeave, onBeforeRouteUpdate, useRoute, useRouter } from "vue-router";
import { ElMessage, ElMessageBox } from "element-plus";

import type { PlanCalculationRuleItem, PlanConfig, PlanFieldItem, PlanMaterialItem, PlanRuleItem, PlanSectionItem, PlanStatus } from "@/api/contracts";
import { createPlan, getPlanConfig, updatePlan, updatePlanConfig } from "@/api/plans";
import { listSceneOptions, type ScenarioOption } from "@/api/scenes";
import { operationIdempotencyKey } from "@/api/http";
import BackButton from "@/components/BackButton.vue";
import PageHeader from "@/components/PageHeader.vue";
import PlanBusinessEditor from "@/components/plans/PlanBusinessEditor.vue";
import PlanExtractionEditor from "@/components/plans/PlanExtractionEditor.vue";
import PlanConfigList from "@/components/plans/PlanConfigList.vue";
import { sourceSheetLabel } from "@/composables/useRuleSourceFilter";
import { executionModeLabel } from "@/constants/executionModes";
import { TABLE_COLUMN_WIDTHS } from "@/constants/tableColumns";
import {
  extractionStrategyLabel,
  reportContentTypeOptions,
  reportFormatOptions,
  reportResultSortOptions,
  valueTypeLabel,
} from "@/constants/labels";
import { newId } from "@/utils/ids";

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
const plan = ref<PlanStatus | null>(null);
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

/**
 * 可编辑配置的快照：只取五份业务集合。
 *
 * request_id、effective、scope_hash、sync_error 等是服务端返回的元数据，
 * 参与比较会让「已经保存但仍显示未保存」。
 */
function configSnapshot(source: PlanConfig | null): string {
  if (!source) return "";
  return JSON.stringify({
    materials: source.materials,
    fields: source.fields,
    rules: source.rules,
    sections: source.sections,
    calculation_rules: source.calculation_rules,
  });
}
const loading = ref(false);
const loadError = ref<string | null>(null);
const planStatusError = ref<string | null>(null);
const saving = ref(false);
let createAttempt: { signature: string; key: string } | null = null;
let loadRequest = 0;
/** 新建成功后要跳到编辑页，这次导航不应再问「未保存的修改」。 */
let allowLeave = false;

const basicForm = reactive({ plan_name: "", scenario_code: "", description: "", enabled: true });
const hasUnsavedBasic = computed(() => plan.value !== null && (
  basicForm.plan_name.trim() !== plan.value.plan_name
  || (basicForm.description.trim() || null) !== plan.value.description
  || basicForm.enabled !== plan.value.enabled
));
const hasUnsavedConfig = computed(() => config.value !== null && configSnapshot(config.value) !== savedConfigJson.value);
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
  // 新对象重新加载后，之前的「不再询问」豁免不再适用。
  allowLeave = false;
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
    const planConfig = await getPlanConfig(code);
    if (request !== loadRequest || planCode.value !== code) return;
    plan.value = planConfig;
    config.value = planConfig;
    savedConfigJson.value = configSnapshot(planConfig);
    basicForm.plan_name = planConfig.plan_name;
    basicForm.scenario_code = planConfig.scenario_code;
    basicForm.description = planConfig.description ?? "";
    basicForm.enabled = planConfig.enabled;
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

watch(() => planCode.value, () => { createAttempt = null; void load(); }, { immediate: true });
watch(requestedSceneCode, (code) => {
  basicForm.scenario_code = code ?? "";
}, { immediate: true });
onMounted(() => void loadScenes());

/**
 * 未保存修改的离开提示。
 *
 * 只有确实存在未保存内容时才拦；同一对象的页签与查询参数切换不触发离开守卫。
 */
const hasUnsavedInput = computed(() => (
  hasUnsavedBasic.value
  || hasUnsavedConfig.value
  || (plan.value === null && Boolean(basicForm.plan_name.trim() || basicForm.description.trim() || basicForm.scenario_code))
));

async function confirmLeave(): Promise<boolean> {
  if (allowLeave || !hasUnsavedInput.value) return true;
  try {
    await ElMessageBox.confirm("当前方案有未保存的修改，离开后将丢失。", "未保存的修改", {
      type: "warning", confirmButtonText: "仍要离开", cancelButtonText: "留在本页",
    });
    return true;
  } catch {
    return false;
  }
}

function warnBeforeUnload(event: BeforeUnloadEvent): void {
  if (!hasUnsavedInput.value) return;
  event.preventDefault();
  event.returnValue = "";
}

onMounted(() => window.addEventListener("beforeunload", warnBeforeUnload));
onUnmounted(() => window.removeEventListener("beforeunload", warnBeforeUnload));
onBeforeRouteLeave(() => confirmLeave());
onBeforeRouteUpdate((to, from) => (to.params.planCode === from.params.planCode ? true : confirmLeave()));

function describeError(error: unknown): string {
  return error instanceof Error ? error.message : "操作失败";
}

function openCreateTask(): void {
  if (!plan.value || !canCreateTask.value) return;
  void router.push({ name: "audit-tasks", query: { createPlan: plan.value.plan_code } });
}

async function submitCreate(): Promise<void> {
  if (saving.value) return;
  const createRoute = route.fullPath;
  const scene = selectedScene.value;
  if (!basicForm.plan_name.trim() || !scene || scene.available === false) {
    // 下拉里只列「已启用且已有已发布版本」的场景，选不到就是还不具备建方案的条件。
    ElMessage.warning("请填写方案名称，并选择一个已发布且已启用的场景");
    return;
  }
  const payload = {
      plan_name: basicForm.plan_name.trim(),
      scenario_code: scene.scenario_code,
      scenario_version_no: scene.latest_version_no,
      description: basicForm.description.trim() || null,
      enabled: basicForm.enabled,
  };
  const signature = JSON.stringify(payload);
  let attempt = createAttempt;
  if (!attempt || attempt.signature !== signature) {
    attempt = { signature, key: operationIdempotencyKey("PLAN_CREATE", scene.scenario_code) };
    createAttempt = attempt;
  }
  saving.value = true;
  try {
    const created = await createPlan({ ...payload, idempotency_key: attempt.key });
    if (route.fullPath !== createRoute) return;
    ElMessage.success("方案已创建，配置已从场景版本复制");
    allowLeave = true;
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

async function refreshPlanStatus(): Promise<PlanStatus | null> {
  const code = planCode.value;
  const request = loadRequest;
  if (!code) return null;
  try {
    const planConfig = await getPlanConfig(code);
    if (request !== loadRequest || planCode.value !== code) return null;
    plan.value = planConfig;
    planStatusError.value = null;
    return planConfig;
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
  const submittedConfigJson = configSnapshot(currentConfig);
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
    const editedDuringSave = configSnapshot(currentConfig) !== submittedConfigJson;
    savedConfigJson.value = configSnapshot(updated);
    config.value = editedDuringSave ? currentConfig : updated;
    // 保存响应已经带回方案状态（effective / sync_error），不再为拿状态多读一次配置。
    plan.value = updated;
    planStatusError.value = null;
    const refreshedPlan = updated;
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

function materialSearchText(item: PlanMaterialItem): string {
  return [item.name, item.source_ref, item.description].join("\n");
}
function fieldSearchText(item: PlanFieldItem): string {
  return [item.name, item.source_ref, materialNames([item.material_key]), item.instruction].join("\n");
}
function ruleSearchText(item: PlanRuleItem): string {
  return [item.name, item.rule_code, item.source_ref, sourceSheetLabel(item.origin), ruleMaterialNames(item), item.rule_json.requirement].join("\n");
}
function calculationSearchText(item: PlanCalculationRuleItem): string {
  return [item.rule_name, item.rule_code, item.result_code, item.formula, item.requirement, sourceSheetLabel(item.origin)].join("\n");
}
function sectionSearchText(item: PlanSectionItem): string {
  return [item.title, item.report_code, item.report_name, item.content_instruction, dataNames(item.data_refs)].join("\n");
}
function ruleMaterialKeys(item: PlanRuleItem): string[] {
  const keys = item.rule_json.materials;
  return Array.isArray(keys) ? keys.filter((key): key is string => typeof key === "string") : [];
}
function sectionStatusLabel(item: PlanSectionItem): string {
  return item.config_status === "PENDING" ? "待确认·不生成" : item.config_status === "DISABLED" ? "不生成" : "生成";
}

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
    section_key: existing?.section_key ?? newId(),
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
  return materialNames(ruleMaterialKeys(rule));
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

</script>

<template>
  <div class="app-panel">
    <PageHeader
      :title="isCreate ? '新增方案' : (basicForm.plan_name ? `方案配置：${basicForm.plan_name}` : '方案配置')"
      :show-refresh="false"
    >
      <template #leading>
        <BackButton label="返回方案列表" @click="router.push({ name: 'plans' })" />
      </template>
      <template #actions>
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
              :disabled="!isCreate"
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
          <el-form-item label="方案说明">
            <el-input v-model="basicForm.description" type="textarea" :rows="2" maxlength="4000" />
          </el-form-item>
          <el-form-item label="启用状态">
            <el-switch v-model="basicForm.enabled" active-text="启用" inactive-text="停用" />
          </el-form-item>
        </el-form>
        <div class="card-actions">
          <el-button type="primary" :loading="saving" :disabled="scenesLoading || !!scenesError" @click="submitCreate">创建方案</el-button>
        </div>
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
          <el-tabs v-model="activeTab" class="plan-tabs">
            <el-tab-pane label="基本信息" name="basic">
              <el-form label-width="100px" class="plan-basic-form">
                <el-form-item label="方案名称" required>
                  <el-input v-model="basicForm.plan_name" maxlength="255" placeholder="请输入方案名称" />
                </el-form-item>
                <el-form-item label="所属场景">
                  <span>{{ plan?.base_scenario_version?.scenario_name || "未记录场景名称" }}（第 {{ plan?.base_scenario_version?.version_no ?? "—" }} 版）</span>
                </el-form-item>
                <el-form-item label="方案说明">
                  <el-input v-model="basicForm.description" type="textarea" :rows="3" maxlength="4000" />
                </el-form-item>
                <el-form-item label="启用状态">
                  <el-switch v-model="basicForm.enabled" active-text="启用" inactive-text="停用" />
                </el-form-item>
              </el-form>
            </el-tab-pane>

            <el-tab-pane :label="`材料（${config.materials.length}）`" name="materials" lazy>
              <PlanConfigList
                :items="config.materials" row-key="material_key"
                :number-of="(item: PlanMaterialItem) => item.source_ref"
                :search-text="materialSearchText"
                :status-text="(item: PlanMaterialItem) => statusLabel(item.config_status)"
                keyword-placeholder="材料名称、编号或说明" empty-text="当前方案没有材料"
              >
                  <el-table-column label="材料编号" width="140" show-overflow-tooltip>
                    <template #default="{ row }"><span class="mono">{{ row.source_ref || "未填写" }}</span></template>
                  </el-table-column>
                  <el-table-column prop="name" label="材料名称" width="180" show-overflow-tooltip />
                  <el-table-column prop="description" label="业务说明" min-width="300" show-overflow-tooltip />
                  <el-table-column label="必传" width="88"><template #default="{ row }">{{ row.required ? "是" : "否" }}</template></el-table-column>
                  <el-table-column label="状态" width="140"><template #default="{ row }"><el-tag :type="statusTone(row.config_status)" size="small">{{ statusLabel(row.config_status) }}</el-tag></template></el-table-column>
                  <el-table-column label="操作" width="84" class-name="table-actions" fixed="right"><template #default="{ row }"><el-button link type="primary" size="small" @click="editExtraction('material', row.material_key)">编辑</el-button></template></el-table-column>
              </PlanConfigList>
            </el-tab-pane>

            <el-tab-pane :label="`字段（${config.fields.length}）`" name="fields" lazy>
              <PlanConfigList
                :items="config.fields" row-key="field_key"
                :number-of="(item: PlanFieldItem) => item.source_ref"
                :search-text="fieldSearchText"
                :status-text="(item: PlanFieldItem) => statusLabel(item.config_status, item.enabled)"
                :materials="config.materials" :material-keys="(item: PlanFieldItem) => [item.material_key]"
                group-label="数据类型" :group-text="(item: PlanFieldItem) => valueTypeLabel(item.value_type)"
                keyword-placeholder="字段名称、编号或提取要求" empty-text="当前方案没有字段"
              >
                  <el-table-column label="字段编号" width="140" show-overflow-tooltip>
                    <template #default="{ row }"><span class="mono">{{ row.source_ref || "未填写" }}</span></template>
                  </el-table-column>
                  <el-table-column prop="name" label="字段名称" width="180" show-overflow-tooltip />
                  <el-table-column label="所属材料" width="168" show-overflow-tooltip><template #default="{ row }">{{ materialNames([row.material_key]) }}</template></el-table-column>
                  <el-table-column label="数据类型" width="88"><template #default="{ row }">{{ valueTypeLabel(row.value_type) }}</template></el-table-column>
                  <el-table-column label="抽取策略" :width="TABLE_COLUMN_WIDTHS.extractionStrategy"><template #default="{ row }">{{ extractionStrategyLabel(row.extraction_strategy) }}</template></el-table-column>
                  <el-table-column label="必抽" width="88"><template #default="{ row }">{{ row.required ? "是" : "否" }}</template></el-table-column>
                  <el-table-column label="状态" width="140"><template #default="{ row }"><el-tag :type="statusTone(row.config_status, row.enabled)" size="small">{{ statusLabel(row.config_status, row.enabled) }}</el-tag></template></el-table-column>
                  <el-table-column prop="instruction" label="提取要求" min-width="300" show-overflow-tooltip />
                  <el-table-column label="操作" width="84" class-name="table-actions" fixed="right"><template #default="{ row }"><el-button link type="primary" size="small" @click="editExtraction('field', row.field_key)">编辑</el-button></template></el-table-column>
              </PlanConfigList>
            </el-tab-pane>

            <el-tab-pane :label="`审核规则（${businessRules.length}）`" name="rules" lazy>
              <PlanConfigList
                :items="businessRules" row-key="rule_key"
                :number-of="(item: PlanRuleItem) => item.rule_code || item.source_ref"
                :search-text="ruleSearchText"
                :status-text="(item: PlanRuleItem) => statusLabel(item.rule_json.config_status, item.enabled)"
                :materials="config.materials" :material-keys="ruleMaterialKeys"
                group-label="来源工作表" :group-text="(item: PlanRuleItem) => sourceSheetLabel(item.origin)"
                :summary="`已启用 ${enabledRules.length} 条，未启用 ${disabledRules.length} 条`"
                keyword-placeholder="规则名称、业务编号或审核要求" empty-text="当前方案没有审核规则"
              >
                  <el-table-column label="业务编号" width="140" show-overflow-tooltip>
                    <template #default="{ row }"><span class="mono">{{ row.rule_code || row.source_ref || "未填写" }}</span></template>
                  </el-table-column>
                  <el-table-column prop="name" label="规则名称" min-width="180" show-overflow-tooltip align="center" />
                  <el-table-column label="执行方式" width="112" align="center">
                    <template #default="{ row }">{{ executionModeLabel(typeof row.rule_json?.execution_mode === "string" ? row.rule_json.execution_mode : null) }}</template>
                  </el-table-column>
                  <el-table-column label="来源工作表" width="204" show-overflow-tooltip align="center">
                    <template #default="{ row }">{{ sourceSheetLabel(row.origin) }}</template>
                  </el-table-column>
                  <el-table-column label="适用材料" width="180" show-overflow-tooltip align="center"><template #default="{ row }">{{ ruleMaterialNames(row) }}</template></el-table-column>
                  <el-table-column label="审核要求" min-width="340" show-overflow-tooltip align="center"><template #default="{ row }">{{ row.rule_json.requirement || "—" }}</template></el-table-column>
                  <el-table-column label="状态" width="140"><template #default="{ row }"><el-tag :type="statusTone(row.rule_json.config_status, row.enabled)" size="small">{{ statusLabel(row.rule_json.config_status, row.enabled) }}</el-tag></template></el-table-column>
                  <el-table-column label="操作" width="84" class-name="table-actions" fixed="right"><template #default="{ row }"><el-button link type="primary" size="small" @click="editBusiness('rule', row.rule_key)">编辑</el-button></template></el-table-column>
              </PlanConfigList>
            </el-tab-pane>

            <el-tab-pane :label="`计算规则（${config.calculation_rules.length}）`" name="calculations" lazy>
              <PlanConfigList
                :items="config.calculation_rules" row-key="rule_code"
                :number-of="(item: PlanCalculationRuleItem) => item.rule_code"
                :search-text="calculationSearchText"
                :status-text="(item: PlanCalculationRuleItem) => statusLabel(item.status, item.enabled)"
                group-label="来源工作表" :group-text="(item: PlanCalculationRuleItem) => sourceSheetLabel(item.origin)"
                keyword-placeholder="指标名称、规则编号或计算方法" empty-text="当前方案没有计算规则"
              >
                  <el-table-column prop="rule_code" label="规则编号" width="140" show-overflow-tooltip />
                  <el-table-column prop="rule_name" label="指标名称" min-width="180" show-overflow-tooltip />
                  <el-table-column label="来源工作表" width="204" show-overflow-tooltip align="center">
                    <template #default="{ row }">{{ sourceSheetLabel(row.origin) }}</template>
                  </el-table-column>
                  <el-table-column prop="formula" label="计算方法" min-width="300" show-overflow-tooltip />
                  <el-table-column prop="requirement" label="业务要求" min-width="300" show-overflow-tooltip />
                  <el-table-column label="状态" width="140"><template #default="{ row }"><el-tag :type="statusTone(row.status, row.enabled)" size="small">{{ statusLabel(row.status, row.enabled) }}</el-tag></template></el-table-column>
                  <el-table-column label="操作" width="84" class-name="table-actions" fixed="right"><template #default="{ row }"><el-button link type="primary" size="small" @click="editBusiness('calculation', row.rule_code)">编辑</el-button></template></el-table-column>
              </PlanConfigList>
            </el-tab-pane>

            <el-tab-pane :label="`报告章节（${config.sections.length}）`" name="sections" lazy>
              <PlanConfigList
                :items="config.sections" row-key="section_key"
                :number-of="(item: PlanSectionItem) => item.report_code"
                :search-text="sectionSearchText" :status-text="sectionStatusLabel"
                :materials="config.materials" :material-keys="(item: PlanSectionItem) => item.material_keys ?? []"
                group-label="所属报告" :group-text="(item: PlanSectionItem) => item.report_name || item.report_code || '未填写报告归属'"
                keyword-placeholder="章节名称、报告编号或内容要求" empty-text="当前方案没有报告章节"
              >
                <template #actions><el-button type="primary" @click="openSectionEditor()">新增章节</el-button></template>
                  <el-table-column label="报告编号" width="140" show-overflow-tooltip>
                    <template #default="{ row }"><span class="mono">{{ row.report_code || "未填写" }}</span></template>
                  </el-table-column>
                  <el-table-column prop="title" label="章节名称" min-width="160" show-overflow-tooltip />
                  <el-table-column prop="report_name" label="所属报告" width="192" show-overflow-tooltip />
                  <el-table-column label="材料范围" width="120" show-overflow-tooltip><template #default="{ row }">{{ materialNames(row.material_keys) }}</template></el-table-column>
                  <el-table-column label="业务数据引用" min-width="300" show-overflow-tooltip><template #default="{ row }">{{ dataNames(row.data_refs) }}</template></el-table-column>
                  <el-table-column label="章节处理" width="96"><template #default="{ row }"><el-tag :type="row.config_status === 'PENDING' ? 'warning' : row.config_status === 'DISABLED' ? 'info' : 'success'" size="small">{{ sectionStatusLabel(row) }}</el-tag></template></el-table-column>
                  <el-table-column label="操作" width="124" class-name="table-actions" fixed="right"><template #default="{ row }"><el-button link type="primary" size="small" @click="openSectionEditor(row)">编辑</el-button><el-button link type="danger" size="small" @click="removeSection(row)">移除</el-button></template></el-table-column>
              </PlanConfigList>
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
.adjustment-line {
  overflow-wrap: anywhere;
}

.plan-tabs {
  margin-top: 12px;
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
