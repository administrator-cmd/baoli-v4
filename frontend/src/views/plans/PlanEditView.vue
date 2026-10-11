<script setup lang="ts">
import { computed, onMounted, reactive, ref, watch } from "vue";
import { useRoute, useRouter } from "vue-router";
import { ElMessage, ElMessageBox } from "element-plus";

import type { PlanAdjustments, PlanConfig, PlanFieldItem, PlanRuleItem, PlanSectionItem, PlanSelection } from "@/api/contracts";
import { createPlan, getPlan, getPlanAdjustments, getPlanConfig, updatePlan, updatePlanConfig } from "@/api/plans";
import { listSceneOptions, type ScenarioOption } from "@/api/scenes";
import BackButton from "@/components/BackButton.vue";
import PageHeader from "@/components/PageHeader.vue";
import CalculationSources from "@/components/CalculationSources.vue";
import {
  defaultExtractionStrategy,
  extractionStrategyOptions,
  extractionStrategySupports,
  reportContentTypeOptions,
  reportFormatOptions,
  reportResultSortOptions,
  valueTypeOptions,
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
function nodeName(code: string | null): string {
  return config.value?.nodes.find((node) => node.node_code === code)?.name ?? "所属节点未记录";
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
const fieldSchemaDialogVisible = ref(false);
const editingFieldSchema = ref<PlanFieldItem | null>(null);
const fieldSchemaText = ref("");
const editingSectionKey = ref<string | null>(null);
const sectionForm = reactive({
  title: "",
  content_instruction: "",
  field_keys: [] as string[],
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
const fieldOptions = computed(() => (config.value?.fields ?? []).map((field) => ({
  value: field.field_key,
  label: `${config.value?.materials.find((material) => material.material_key === field.material_key)?.name ?? field.material_key} · ${field.name}`,
})));

function handlePlanFieldTypeChange(field: PlanFieldItem): void {
  if (field.extraction_strategy !== "STRUCTURED") field.structure_schema = null;
}

function handlePlanFieldStrategyChange(field: PlanFieldItem): void {
  if (field.extraction_strategy !== "STRUCTURED") {
    field.structure_schema = null;
  }
}

function openFieldSchemaEditor(field: PlanFieldItem): void {
  editingFieldSchema.value = field;
  fieldSchemaText.value = field.structure_schema ? JSON.stringify(field.structure_schema, null, 2) : "";
  fieldSchemaDialogVisible.value = true;
}

function applyFieldSchema(): void {
  const field = editingFieldSchema.value;
  if (!field) return;
  const text = fieldSchemaText.value.trim();
  if (!text) {
    ElMessage.warning("结构化抽取必须填写结构定义");
    return;
  }
  let parsed: unknown;
  try {
    parsed = JSON.parse(text);
  } catch {
    ElMessage.warning("结构定义不是有效的 JSON");
    return;
  }
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
    ElMessage.warning("结构定义的根节点必须是 JSON 对象");
    return;
  }
  field.structure_schema = parsed as Record<string, unknown>;
  fieldSchemaDialogVisible.value = false;
}

function validatePlanFieldStrategies(fields: PlanFieldItem[]): boolean {
  for (const field of fields) {
    if (field.extraction_strategy === "STRUCTURED" && !field.structure_schema) {
      ElMessage.warning(`字段“${field.name}”使用结构化抽取，必须填写结构定义`);
      return false;
    }
    if (field.extraction_strategy !== "STRUCTURED" && field.structure_schema) {
      ElMessage.warning(`字段“${field.name}”不是结构化抽取，不能保留结构定义`);
      return false;
    }
  }
  return true;
}

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
  if (!validatePlanFieldStrategies(currentConfig.fields)) return;
  const submittedConfigJson = JSON.stringify(currentConfig);
  saving.value = true;
  try {
    // 整体提交配置及计算规则选择。
    const updated = await updatePlanConfig(code, {
      materials: currentConfig.materials,
      fields: currentConfig.fields,
      rules: currentConfig.rules,
      sections: currentConfig.sections,
      calculation_rules: currentConfig.calculation_rules.map(({ rule_code, enabled }) => ({ rule_code, enabled })),
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

const enabledRules = computed(() => (config.value?.rules ?? []).filter((item) => item.enabled));
const disabledRules = computed(() => (config.value?.rules ?? []).filter((item) => !item.enabled));

function openSectionEditor(item?: PlanSectionItem): void {
  editingSectionKey.value = item?.section_key ?? null;
  sectionForm.title = item?.title ?? "";
  sectionForm.content_instruction = item?.content_instruction ?? "";
  sectionForm.field_keys = [...(item?.field_keys ?? [])];
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
    field_keys: [...sectionForm.field_keys],
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

function fieldNames(keys: string[]): string {
  return keys.map((key) => config.value?.fields.find((field) => field.field_key === key)?.name ?? key).join("、") || "未绑定";
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
  <div class="app-panel">
    <PageHeader
      eyebrow="方案管理"
      :title="isCreate ? '新增方案' : `方案编辑：${basicForm.plan_name || planCode}`"
      subtitle="基于已发布场景版本创建个性化方案。"
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
            <div class="text-hint">
              <template v-if="isCreate">
                基于所选版本创建；场景更新不会改动已有方案。
              </template>
              <template v-else>
                本方案固定到创建时的场景版本。
              </template>
            </div>
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
              <el-table :data="config.materials" border stripe size="small" max-height="420">
                <el-table-column label="材料名称" min-width="240">
                  <template #default="{ row }"><el-input v-model="row.name" maxlength="255" aria-label="材料名称" /></template>
                </el-table-column>
                <el-table-column label="是否必传" width="120">
                  <template #default="{ row }"><el-switch v-model="row.required" aria-label="是否必传" /></template>
                </el-table-column>
              </el-table>
            </el-tab-pane>

            <el-tab-pane :label="`字段（${config.fields.length}）`" name="fields">
              <el-table :data="config.fields" border stripe size="small" max-height="420">
                <el-table-column label="字段名称" min-width="180">
                  <template #default="{ row }"><el-input v-model="row.name" maxlength="255" aria-label="字段名称" /></template>
                </el-table-column>
                <el-table-column label="所属材料" min-width="170">
                  <template #default="{ row }">
                    <el-select v-model="row.material_key" aria-label="所属材料">
                      <el-option v-for="material in config.materials" :key="material.material_key" :label="material.name" :value="material.material_key" />
                    </el-select>
                  </template>
                </el-table-column>
                <el-table-column label="数据类型" width="150">
                  <template #default="{ row }">
                    <el-select v-model="row.value_type" :aria-label="`${row.name}的数据类型`" @change="handlePlanFieldTypeChange(row)">
                      <el-option v-for="item in valueTypeOptions" :key="item.value" :value="item.value" :label="item.label" />
                    </el-select>
                  </template>
                </el-table-column>
                <el-table-column label="抽取策略" width="155">
                  <template #default="{ row }">
                    <el-select v-model="row.extraction_strategy" :aria-label="`${row.name}的抽取策略`" @change="handlePlanFieldStrategyChange(row)">
                      <el-option
                        v-for="item in extractionStrategyOptions"
                        :key="item.value"
                        :value="item.value"
                        :label="item.label"
                        :disabled="!extractionStrategySupports(item.value, row.value_type)"
                      />
                    </el-select>
                  </template>
                </el-table-column>
                <el-table-column label="结构定义" width="110">
                  <template #default="{ row }">
                    <el-button
                      v-if="row.extraction_strategy === 'STRUCTURED'"
                      link
                      type="primary"
                      size="small"
                      @click="openFieldSchemaEditor(row)"
                    >
                      {{ row.structure_schema ? "编辑" : "填写" }}
                    </el-button>
                    <span v-else class="text-muted">不适用</span>
                  </template>
                </el-table-column>
                <el-table-column label="必抽" width="75">
                  <template #default="{ row }"><el-switch v-model="row.required" aria-label="是否必抽" /></template>
                </el-table-column>
                <el-table-column label="单位" width="110">
                  <template #default="{ row }"><el-input v-model="row.unit" aria-label="单位" /></template>
                </el-table-column>
                <el-table-column label="币种" width="110">
                  <template #default="{ row }"><el-input v-model="row.currency" aria-label="币种" /></template>
                </el-table-column>
                <el-table-column label="提取要求" min-width="260">
                  <template #default="{ row }"><el-input v-model="row.instruction" aria-label="提取要求" /></template>
                </el-table-column>
              </el-table>
            </el-tab-pane>

            <el-tab-pane :label="`审核规则（${enabledRules.length} 启用）`" name="rules">
              <el-table :data="config.rules" border stripe size="small" max-height="420">
                <el-table-column label="启用" width="80">
                  <template #default="{ row }">
                    <el-switch v-model="row.enabled" :aria-label="`启用规则 ${row.name}，适用材料 ${ruleMaterialNames(row)}`" />
                  </template>
                </el-table-column>
                <el-table-column prop="name" label="规则名称" min-width="220" show-overflow-tooltip />
                <el-table-column label="阶段" width="130"><template #default="{ row }">{{ nodeName(row.node_code) }}</template></el-table-column>
                <el-table-column label="类型" width="90"><template #default="{ row }">{{ row.is_system ? "系统" : "业务" }}</template></el-table-column>
                <el-table-column label="适用材料" min-width="160" show-overflow-tooltip><template #default="{ row }">{{ ruleMaterialNames(row) }}</template></el-table-column>
              </el-table>
              <div class="text-hint tab-hint">已启用 {{ enabledRules.length }} 条，已取消 {{ disabledRules.length }} 条。保存后才会影响后续任务。</div>
            </el-tab-pane>

            <el-tab-pane :label="`计算规则（${config.calculation_rules.length}）`" name="calculations">
              <el-table :data="config.calculation_rules" border stripe size="small" max-height="420" empty-text="当前方案没有计算规则">
                <el-table-column type="expand" width="48"><template #default="{ row }"><CalculationSources :origin="row.origin" :parameter-sources="row.parameter_sources" /></template></el-table-column>
                <el-table-column label="启用" width="80"><template #default="{ row }"><el-switch v-model="row.enabled" :aria-label="`启用计算规则 ${row.rule_code}`" /></template></el-table-column>
                <el-table-column prop="rule_name" label="指标名称" min-width="220" show-overflow-tooltip />
                <el-table-column prop="formula" label="计算方法" min-width="220" show-overflow-tooltip />
                <el-table-column prop="requirement" label="业务要求" min-width="220" show-overflow-tooltip />
                <el-table-column prop="rule_code" label="编号" width="150" />
                <el-table-column label="计算节点" min-width="150"><template #default="{ row }">{{ nodeName(row.node_code) }}</template></el-table-column>
              </el-table>
            </el-tab-pane>

            <el-tab-pane :label="`报告章节（${config.sections.length}）`" name="sections">
              <div class="app-toolbar">
                <div class="app-toolbar__left"><el-button @click="openSectionEditor()">新增章节</el-button></div>
              </div>
              <el-table :data="config.sections" border stripe size="small" max-height="420" empty-text="当前方案没有报告章节">
                <el-table-column prop="report_code" label="报告编号" width="130" show-overflow-tooltip />
                <el-table-column prop="title" label="章节名称" min-width="180" show-overflow-tooltip />
                <el-table-column prop="report_name" label="所属报告" min-width="150" show-overflow-tooltip />
                <el-table-column label="材料范围" min-width="150" show-overflow-tooltip><template #default="{ row }">{{ materialNames(row.material_keys) }}</template></el-table-column>
                <el-table-column label="绑定字段" min-width="200" show-overflow-tooltip><template #default="{ row }">{{ fieldNames(row.field_keys) }}</template></el-table-column>
                <el-table-column label="章节处理" width="115"><template #default="{ row }">{{ row.config_status === "PENDING" ? "待核对" : row.config_status === "DISABLED" ? "不生成" : "生成" }}</template></el-table-column>
                <el-table-column label="操作" width="120" fixed="right"><template #default="{ row }"><el-button link type="primary" size="small" @click="openSectionEditor(row)">编辑</el-button><el-button link type="danger" size="small" @click="removeSection(row)">移除</el-button></template></el-table-column>
              </el-table>
            </el-tab-pane>

          </el-tabs>

        </section>

        <el-dialog v-model="sectionDialogVisible" :title="editingSectionKey ? '编辑报告章节' : '新增报告章节'" width="min(680px, 96vw)" :close-on-click-modal="false">
          <el-form label-width="100px">
            <el-form-item label="章节名称" required>
              <el-input v-model="sectionForm.title" maxlength="255" />
            </el-form-item>
            <el-form-item label="绑定字段">
              <el-select v-model="sectionForm.field_keys" multiple filterable style="width: 100%">
                <el-option v-for="field in fieldOptions" :key="field.value" :value="field.value" :label="field.label" />
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
                <el-option value="PENDING" label="待核对（方案暂不可用）" />
                <el-option value="DISABLED" label="不生成" />
              </el-select>
            </el-form-item>
          </el-form>
          <template #footer>
            <el-button @click="sectionDialogVisible = false">取消</el-button>
            <el-button type="primary" @click="applySection">应用修改</el-button>
          </template>
        </el-dialog>

        <el-dialog v-model="fieldSchemaDialogVisible" title="编辑字段结构" width="min(680px, 96vw)" :close-on-click-modal="false">
          <el-alert
            type="info"
            show-icon
            :closable="false"
            :title="editingFieldSchema ? `字段：${editingFieldSchema.name}` : '结构化字段'"
          />
          <el-input
            v-model="fieldSchemaText"
            class="block-gap-sm"
            type="textarea"
            :rows="14"
            placeholder='例如：{"type":"object","properties":{"付款期限":{"type":"string"}},"required":["付款期限"],"additionalProperties":false}'
          />
          <div class="text-hint">填写 JSON Schema。保存方案时后端会继续校验支持的类型、属性和约束。</div>
          <template #footer>
            <el-button @click="fieldSchemaDialogVisible = false">取消</el-button>
            <el-button type="primary" @click="applyFieldSchema">应用修改</el-button>
          </template>
        </el-dialog>

      </template>
  </div>
</template>

<style scoped>
.adjustment-line {
  overflow-wrap: anywhere;
}

.plan-tabs {
  margin-top: 8px;
}

.plan-tabs :deep(.el-tab-pane) {
  min-height: 280px;
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

.tab-hint {
  margin-top: 10px;
}
</style>
