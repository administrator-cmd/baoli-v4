<script setup lang="ts">
import { computed, reactive, ref, watch } from "vue";
import { useRoute, useRouter } from "vue-router";
import { ElMessage, ElMessageBox } from "element-plus";
import { CircleCheck, FileUp, Pencil, Plus, Power, Rocket, Save, Trash2 } from "lucide-vue-next";

import type {
  DraftDisabledSourceRow,
  DraftSourceActivationPreview,
  DraftSourceActivationPreviewData,
  ScenarioDetail,
  ScenarioDraftView,
} from "@/api/contracts";
import { operationIdempotencyKey } from "@/api/http";
import { useOperationTracker } from "@/composables/useOperationTracker";
import {
  deleteScene,
  getScene,
  getSceneDraft,
  previewSourceActivation,
  confirmSourceActivation,
  publishSceneDraft,
  setSceneEnabled,
  updateScene,
  updateDisabledSourceRow,
} from "@/api/scenes";
import SceneFieldsPanel from "@/components/scenes/SceneFieldsPanel.vue";
import SceneMaterialsPanel from "@/components/scenes/SceneMaterialsPanel.vue";
import SceneRulesPanel from "@/components/scenes/SceneRulesPanel.vue";
import SceneSectionsPanel from "@/components/scenes/SceneSectionsPanel.vue";
import CalculationSources from "@/components/CalculationSources.vue";
import BackButton from "@/components/BackButton.vue";

/**
 * 场景配置按来源、材料、字段、规则、计算和报告组织。
 * 保存只更新草稿；管理员在场景页显式发布后才生成生效版本。
 */
const publishing = ref(false);
const route = useRoute();
const router = useRouter();

const scenarioCode = computed(() => String(route.params.scenarioCode ?? ""));
const returnImportJobNo = computed(() => typeof route.query.importJob === "string" ? route.query.importJob : null);
const detail = ref<ScenarioDetail | null>(null);
const configDraft = ref<ScenarioDraftView | null>(null);
const configError = ref<string | null>(null);
const loading = ref(false);
const loadError = ref<string | null>(null);
const saving = ref(false);
const deleting = ref(false);
const refreshToken = ref(0);
let loadSequence = 0;

/**
 * 场景详情分区。`?tab=` 是当前分区的唯一真相源：URL 变分区就变，分区变就写回 URL。
 */
const TAB_NAMES = ["basic", "source", "materials", "fields", "rules", "calculations", "sections"];

const tabs = computed({
  get: () => {
    const requested = route.query.tab;
    return typeof requested === "string" && TAB_NAMES.includes(requested) ? requested : "basic";
  },
  set: (value: string) => {
    // 分区切换不增加浏览器历史，后退直接返回场景列表。
    void router.replace({ query: { ...route.query, tab: value } });
  },
});

const canEditBasic = computed(() => detail.value !== null);
const calculations = computed(() => configDraft.value?.calculations ?? []);
const disabledSourceRows = computed(() => configDraft.value?.disabled_source_rows ?? []);
const sourceSheets = computed(() => {
  const draft = configDraft.value;
  if (!draft) return [];
  const items = [
    ...draft.materials.map((item) => ({ kind: "材料", name: item.name, origin: item.origin })),
    ...draft.fields.map((item) => ({ kind: "字段", name: item.name, origin: item.origin })),
    ...draft.rules.filter((item) => !item.system).map((item) => ({ kind: "审核规则", name: item.name, origin: item.origin })),
    ...draft.calculations.map((item) => ({ kind: "计算规则", name: item.name, origin: item.origin })),
    ...draft.sections.map((item) => ({ kind: "报告章节", name: item.title, origin: item.origin })),
  ];
  return draft.sheets.map((sheet) => ({
    ...sheet,
    items: items.filter((item) => item.origin && item.origin.sheet_key === sheet.sheet_key),
  }));
});
const editDisabledVisible = ref(false);
const editDisabledSaving = ref(false);
const editingDisabledRow = ref<DraftDisabledSourceRow | null>(null);
const editingDisabledScene = ref("");
const editingDisabledRevision = ref(0);
const disabledValues = ref<Record<string, string>>({});
const selectedSourceRows = ref<DraftDisabledSourceRow[]>([]);
const sourcePreview = ref<DraftSourceActivationPreview | null>(null);
const sourcePreviewVisible = ref(false);
const sourcePreviewLoading = ref(false);
const sourceConfirmLoading = ref(false);
const sourcePreviewScene = ref("");
const { track: trackSourceOperation, reset: resetSourceOperation } = useOperationTracker();

function selectSourceRows(rows: DraftDisabledSourceRow[]): void {
  selectedSourceRows.value = rows;
}

async function parseSourceRows(rows: DraftDisabledSourceRow[]): Promise<void> {
  const revision = configDraft.value?.revision_no;
  if (revision === undefined || !rows.length) return;
  const code = scenarioCode.value;
  sourcePreviewLoading.value = true;
  sourcePreview.value = null;
  sourcePreviewVisible.value = false;
  resetSourceOperation();
  try {
    const operation = await previewSourceActivation(
      code,
      rows.map((row) => row.row_key),
      revision,
      operationIdempotencyKey(code, "source_activation_preview"),
    );
    trackSourceOperation(operation, {
      onFinished: async (finished) => {
        if (code !== scenarioCode.value) return;
        sourcePreviewLoading.value = false;
        if (finished.status !== "SUCCEEDED") {
          throw new Error(finished.failure_message || finished.message || "解析失败");
        }
        const result = finished.result as unknown as DraftSourceActivationPreviewData | null;
        if (!result || typeof result.preview_id !== "string" || typeof result.revision_no !== "number"
          || !Array.isArray(result.rows) || !Array.isArray(result.items) || !Array.isArray(result.notices)) {
          throw new Error("后台解析完成，但没有返回有效的预览结果");
        }
        sourcePreview.value = { ...result, request_id: finished.request_id };
        sourcePreviewScene.value = code;
        sourcePreviewVisible.value = true;
      },
      onError: (error) => {
        if (code === scenarioCode.value) {
          sourcePreviewLoading.value = false;
          ElMessage.error(error instanceof Error ? error.message : "解析状态查询失败");
        }
      },
    });
  } catch (error) {
    if (code === scenarioCode.value) {
      sourcePreviewLoading.value = false;
      ElMessage.error(error instanceof Error ? error.message : "解析受理失败");
    }
  }
}

async function confirmParsedSources(): Promise<void> {
  const preview = sourcePreview.value;
  const code = sourcePreviewScene.value;
  if (!preview?.can_activate || code !== scenarioCode.value) return;
  sourceConfirmLoading.value = true;
  try {
    await confirmSourceActivation(code, preview.preview_id, preview.revision_no);
    if (code !== scenarioCode.value) return;
    sourcePreviewVisible.value = false;
    selectedSourceRows.value = [];
    ElMessage.success("解析配置已追加到场景草稿");
    await load();
  } catch (error) {
    if (code === scenarioCode.value) ElMessage.error(error instanceof Error ? error.message : "启用失败");
  } finally {
    sourceConfirmLoading.value = false;
  }
}

const basicForm = reactive({ scenario_name: "", category: "", description: "", enabled: true });

const publishBlockedReason = computed(() => {
  if (!detail.value?.draft) {
    return "还没有场景草稿";
  }
  if (configError.value) {
    return `草稿配置读取失败：${configError.value}`;
  }
  const error = detail.value?.draft?.sync_error;
  if (!error) {
    return null;
  }
  return error.message;
});

async function load(): Promise<void> {
  const sequence = ++loadSequence;
  const code = scenarioCode.value;
  const previousCode = detail.value?.scenario_code;
  if (previousCode && previousCode !== code) {
    detail.value = null;
    configDraft.value = null;
    configError.value = null;
  }
  loading.value = true;
  loadError.value = null;
  try {
    const payload = await getScene(code);
    if (sequence !== loadSequence || code !== scenarioCode.value) return;
    detail.value = payload;
    basicForm.scenario_name = payload.scenario_name;
    basicForm.category = payload.category ?? "";
    basicForm.description = payload.description ?? "";
    basicForm.enabled = payload.enabled;
    try {
      const draft = await getSceneDraft(payload.scenario_code);
      if (sequence !== loadSequence || code !== scenarioCode.value) return;
      configDraft.value = draft;
      configError.value = null;
    } catch (error) {
      if (sequence !== loadSequence || code !== scenarioCode.value) return;
      configDraft.value = null;
      configError.value = error instanceof Error ? error.message : "草稿配置读取失败";
    }
  } catch (failure) {
    if (sequence !== loadSequence || code !== scenarioCode.value) return;
    detail.value = null;
    configDraft.value = null;
    loadError.value = failure instanceof Error ? failure.message : "场景读取失败";
  } finally {
    if (sequence === loadSequence && code === scenarioCode.value) loading.value = false;
  }
}

async function onChildChanged(): Promise<void> {
  refreshToken.value += 1;
  await load();
}

watch(() => scenarioCode.value, () => void load(), { immediate: true });
watch(() => scenarioCode.value, () => {
  editDisabledVisible.value = false;
  editingDisabledRow.value = null;
  resetSourceOperation();
  sourcePreviewLoading.value = false;
  sourceConfirmLoading.value = false;
  sourcePreview.value = null;
  sourcePreviewVisible.value = false;
  sourcePreviewScene.value = "";
  selectedSourceRows.value = [];
});

function editDisabledRow(row: DraftDisabledSourceRow): void {
  disabledValues.value = Object.fromEntries(row.columns.map((column) => [column.key, column.value]));
  editingDisabledRow.value = row;
  editingDisabledScene.value = scenarioCode.value;
  editingDisabledRevision.value = configDraft.value?.revision_no ?? 0;
  editDisabledVisible.value = true;
}

async function saveDisabledRow(): Promise<void> {
  const row = editingDisabledRow.value;
  if (!row || editingDisabledScene.value !== scenarioCode.value) return;
  const values = { ...disabledValues.value };
  editDisabledSaving.value = true;
  try {
    await updateDisabledSourceRow(editingDisabledScene.value, row.row_key, {
      expected_revision: editingDisabledRevision.value,
      values,
    });
    if (editingDisabledScene.value !== scenarioCode.value) return;
    editDisabledVisible.value = false;
    ElMessage.success("停用来源行已保存");
    await load();
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : "保存失败");
  } finally {
    editDisabledSaving.value = false;
  }
}

async function publishScene(): Promise<void> {
  if (!detail.value?.draft || publishBlockedReason.value) {
    return;
  }
  let summary = "";
  try {
    const result = await ElMessageBox.prompt("发布当前规则库配置。已有方案和任务继续使用各自冻结的版本。", "确认并生效", {
      inputPlaceholder: "本次改了什么",
      confirmButtonText: "确认并生效",
      cancelButtonText: "取消",
    });
    summary = result.value.trim();
  } catch {
    return;
  }
  publishing.value = true;
  try {
    const published = await publishSceneDraft(detail.value.scenario_code, {
      expected_revision: detail.value.draft.revision_no,
      change_summary: summary || null,
    });
    ElMessage.success(published.reused ? "配置与当前版本一致" : `第 ${published.version_no} 版已生效`);
    await load();
  } catch (failure) {
    ElMessage.error(failure instanceof Error ? failure.message : "发布失败");
  } finally {
    publishing.value = false;
  }
}

async function saveBasic(): Promise<void> {
  if (!detail.value) {
    return;
  }
  if (!basicForm.scenario_name.trim()) {
    ElMessage.warning("请填写场景名称");
    return;
  }
  saving.value = true;
  try {
    await updateScene(detail.value.scenario_code, {
      scenario_name: basicForm.scenario_name.trim(),
      category: basicForm.category.trim() || null,
      description: basicForm.description.trim() || null,
      enabled: basicForm.enabled,
    });
    ElMessage.success("场景资料已保存");
    await load();
  } catch (failure) {
    ElMessage.error(failure instanceof Error ? failure.message : "保存失败");
  } finally {
    saving.value = false;
  }
}

async function toggleEnabled(): Promise<void> {
  if (!detail.value) {
    return;
  }
  try {
    await setSceneEnabled(detail.value.scenario_code, !detail.value.enabled);
    ElMessage.success(detail.value.enabled ? "场景已停用（不影响已创建任务）" : "场景已启用");
    await load();
  } catch (failure) {
    ElMessage.error(failure instanceof Error ? failure.message : "操作失败");
  }
}

async function removeScenario(): Promise<void> {
  if (!detail.value) {
    return;
  }
  try {
    await ElMessageBox.confirm(
      `确认永久删除场景「${detail.value.scenario_name}」及其草稿？删除后无法恢复，已有方案和任务不受影响。`,
      "删除场景",
      { type: "warning", confirmButtonText: "删除", cancelButtonText: "取消" },
    );
  } catch {
    return;
  }
  deleting.value = true;
  try {
    await deleteScene(detail.value.scenario_code);
    ElMessage.success("场景已删除");
    await router.replace({ name: "scene-config" });
  } catch (failure) {
    ElMessage.error(failure instanceof Error ? failure.message : "删除失败");
  } finally {
    deleting.value = false;
  }
}

function formatDate(value: string | null): string {
  return value ? String(value).replace("T", " ").slice(0, 19) : "—";
}

function openImports(): void {
  if (!detail.value) {
    return;
  }
  void router.push({ name: "scene-imports", query: { scenario: detail.value.scenario_code } });
}

function openPlanCreation(): void {
  if (!detail.value || !detail.value.enabled || detail.value.latest_version_no <= 0) {
    return;
  }
  void router.push({ name: "plan-new", query: { scenario: detail.value.scenario_code } });
}
</script>

<template>
  <div class="app-panel">
    <el-alert v-if="loadError" type="error" show-icon :closable="false" :title="loadError">
      <el-button size="small" @click="load">重试</el-button>
    </el-alert>

    <div v-else v-loading="loading">
      <template v-if="detail">
        <div class="app-detail-head">
          <div class="app-detail-head__identity">
            <BackButton label="返回场景列表" @click="router.push({ name: 'scene-config' })" />
            <span class="app-detail-head__divider" aria-hidden="true" />
            <div class="app-detail-head__title-wrap">
              <h1 class="app-detail-head__name">{{ detail.scenario_name }}</h1>
              <div class="app-detail-head__meta">
                <el-tag v-if="detail.category" size="small" effect="plain" disable-transitions>{{ detail.category }}</el-tag>
                <el-tag :type="detail.enabled ? 'success' : 'info'" size="small" effect="light" disable-transitions>
                  {{ detail.enabled ? "已启用" : "已停用" }}
                </el-tag>
              </div>
            </div>
          </div>
          <div class="app-detail-head__actions">
            <BackButton
              v-if="returnImportJobNo"
              label="返回导入作业"
              @click="router.push({ name: 'scene-import-detail', params: { jobNo: returnImportJobNo } })"
            />
            <el-button plain @click="toggleEnabled">
              <template #icon><Power :size="15" /></template>
              {{ detail.enabled ? "停用场景" : "启用场景" }}
            </el-button>
            <el-button type="danger" plain :loading="deleting" @click="removeScenario">
              <template #icon><Trash2 :size="15" /></template>
              删除场景
            </el-button>
          </div>
        </div>

        <div class="app-toolbar">
          <div class="app-toolbar__left">
            <el-tag v-if="detail.draft?.sync_error || configError" type="warning" effect="plain">配置待完善</el-tag>
            <el-tag v-else-if="detail.latest_version_no > 0" type="success" effect="plain">配置已生效</el-tag>
            <el-tag v-else type="info" effect="plain">待发布</el-tag>
            <span class="text-muted">当前版本：{{ detail.latest_version_no > 0 ? `第 ${detail.latest_version_no} 版` : "无" }}</span>
            <span v-if="detail.draft?.sync_error || configError" class="text-danger">{{ detail.draft?.sync_error?.message || configError }}</span>
          </div>
          <div class="app-toolbar__right">
            <el-button @click="openImports">
              <template #icon><FileUp :size="15" /></template>
              规则库导入
            </el-button>
            <el-button
              v-if="detail.draft"
              type="primary"
              :loading="publishing"
              :disabled="Boolean(publishBlockedReason)"
              @click="publishScene"
            >
              <template #icon><Rocket :size="15" /></template>
              发布场景
            </el-button>
            <el-button v-if="detail.enabled && detail.latest_version_no > 0" @click="openPlanCreation">
              <template #icon><Plus :size="15" /></template>
              创建方案
            </el-button>
          </div>
        </div>

        <el-tabs v-model="tabs">
          <el-tab-pane label="基本信息" name="basic">
            <el-form label-width="100px" class="app-reading">
              <el-form-item label="场景名称" required>
                <el-input v-model="basicForm.scenario_name" :disabled="!canEditBasic" maxlength="255" />
              </el-form-item>
              <el-form-item label="业务类型">
                <el-input v-model="basicForm.category" :disabled="!canEditBasic" maxlength="80" />
              </el-form-item>
              <el-form-item label="场景说明">
                <el-input v-model="basicForm.description" :disabled="!canEditBasic" type="textarea" :rows="3" maxlength="4000" />
              </el-form-item>
              <el-form-item label="启用状态">
                <el-switch v-model="basicForm.enabled" :disabled="!canEditBasic" active-text="启用" inactive-text="停用" />
              </el-form-item>
              <el-form-item label="场景编码">
                <span class="mono">{{ detail.scenario_code }}</span>
              </el-form-item>
              <el-form-item label="草稿修订">
                <span>{{ detail.draft ? `第 ${detail.draft.revision_no} 次修订` : "还没有草稿" }}</span>
              </el-form-item>
              <el-form-item label="生效状态">
                <span>{{ detail.draft?.sync_error || configError ? "配置待完善" : detail.latest_version_no > 0 ? "当前配置已生效" : "待发布" }}</span>
              </el-form-item>
              <el-form-item label="已发布版本">
                <span>{{ detail.latest_version_no > 0 ? `第 ${detail.latest_version_no} 版` : "尚未发布" }}</span>
              </el-form-item>
              <el-form-item label="关联方案">
                <span>{{ detail.plan_count }} 个方案使用该场景</span>
              </el-form-item>
              <el-form-item label="创建时间">
                <span>{{ formatDate(detail.created_at) }}</span>
              </el-form-item>
              <el-form-item label="更新时间">
                <span>{{ formatDate(detail.updated_at) }}</span>
              </el-form-item>
              <el-form-item>
                <el-button type="primary" :disabled="!canEditBasic" :loading="saving" @click="saveBasic">
                  <template #icon><Save :size="15" /></template>
                  保存基本资料
                </el-button>
              </el-form-item>
            </el-form>
          </el-tab-pane>

          <el-tab-pane label="来源配置" name="source">
            <el-alert v-if="configError" type="error" show-icon :closable="false" :title="configError" />
            <template v-else>
              <h3 class="app-section-title">已导入工作表（{{ sourceSheets.length }}）</h3>
              <el-table :data="sourceSheets" border empty-text="当前草稿没有 Excel 来源工作表">
                <el-table-column type="expand" width="48">
                  <template #default="{ row }">
                    <el-table :data="row.items" border empty-text="该工作表没有写入材料、字段、规则或报告章节">
                      <el-table-column prop="kind" label="配置类型" width="110" />
                      <el-table-column prop="name" label="配置名称" min-width="170" />
                      <el-table-column label="来源行" width="90">
                        <template #default="{ row: item }">{{ item.origin.row_no }}</template>
                      </el-table-column>
                      <el-table-column label="来源原文" min-width="300">
                        <template #default="{ row: item }">
                          <span v-for="cell in item.origin.cells" :key="cell" class="source-cell">
                            {{ cell }}：{{ item.origin.texts[cell] }}
                          </span>
                        </template>
                      </el-table-column>
                    </el-table>
                  </template>
                </el-table-column>
                <el-table-column label="工作表" min-width="240">
                  <template #default="{ row }">{{ row.sheet_name || row.sheet_role_label }}</template>
                </el-table-column>
                <el-table-column label="已写入配置数" width="140">
                  <template #default="{ row }">{{ row.items.length }}</template>
                </el-table-column>
              </el-table>
            </template>
            <div v-if="disabledSourceRows.length" class="block-gap">
              <h3 class="app-section-title">停用来源行（{{ disabledSourceRows.length }}）</h3>
              <div class="gap-row">
                <span class="app-muted">可以选中关联来源行交给模型一起解析，预览后追加到草稿。</span>
                <el-button
                  :disabled="!selectedSourceRows.length || sourcePreviewLoading || sourceConfirmLoading"
                  :loading="sourcePreviewLoading"
                  @click="parseSourceRows(selectedSourceRows)"
                >解析所选来源（{{ selectedSourceRows.length }}）</el-button>
              </div>
              <el-table :data="disabledSourceRows" row-key="row_key" border @selection-change="selectSourceRows">
                <el-table-column type="selection" width="42" />
                <el-table-column type="expand">
                  <template #default="{ row }">
                    <el-descriptions :column="2" border>
                      <el-descriptions-item v-for="column in row.columns" :key="column.key" :label="column.label">
                        {{ column.value || "未填写" }}
                      </el-descriptions-item>
                    </el-descriptions>
                  </template>
                </el-table-column>
                <el-table-column prop="sheet_name" label="工作表" min-width="170" />
                <el-table-column label="来源行" width="90">
                  <template #default="{ row }">{{ row.source.row_no }}</template>
                </el-table-column>
                <el-table-column label="业务编号" min-width="150">
                  <template #default="{ row }">{{ row.source_id || "未填写" }}</template>
                </el-table-column>
                <el-table-column label="操作" width="110" fixed="right">
                  <template #default="{ row }">
                    <el-tooltip content="编辑停用来源行">
                      <el-button link aria-label="编辑停用来源行" @click="editDisabledRow(row)">
                        <Pencil :size="16" />
                      </el-button>
                    </el-tooltip>
                    <el-tooltip content="解析并预览启用配置">
                      <el-button
                        link
                        :disabled="sourcePreviewLoading || sourceConfirmLoading"
                        aria-label="解析并预览启用配置"
                        @click="parseSourceRows([row])"
                      >
                        <CircleCheck :size="16" />
                      </el-button>
                    </el-tooltip>
                  </template>
                </el-table-column>
              </el-table>
            </div>
          </el-tab-pane>

          <el-tab-pane label="材料" name="materials">
            <SceneMaterialsPanel :scenario-code="detail.scenario_code" :refresh-token="refreshToken" @changed="onChildChanged" />
          </el-tab-pane>
          <el-tab-pane label="字段" name="fields">
            <SceneFieldsPanel :scenario-code="detail.scenario_code" :refresh-token="refreshToken" @changed="onChildChanged" />
          </el-tab-pane>
          <el-tab-pane label="审核规则" name="rules">
            <SceneRulesPanel :scenario-code="detail.scenario_code" :refresh-token="refreshToken" @changed="onChildChanged" />
          </el-tab-pane>
          <el-tab-pane label="计算规则" name="calculations">
            <el-alert v-if="configError" type="error" show-icon :closable="false" :title="configError" />
            <el-table v-else :data="calculations" border empty-text="当前草稿没有计算规则">
              <el-table-column type="expand" width="48">
                <template #default="{ row }">
                  <CalculationSources :origin="row.origin" :parameter-sources="row.parameter_sources" />
                </template>
              </el-table-column>
              <el-table-column prop="rule_code" label="规则编号" min-width="120" />
              <el-table-column prop="name" label="名称" min-width="160" />
              <el-table-column prop="formula" label="计算方法" min-width="220" show-overflow-tooltip />
              <el-table-column prop="requirement" label="业务要求" min-width="220" show-overflow-tooltip />
              <el-table-column prop="result_code" label="结果编号" min-width="120" />
              <el-table-column prop="unit" label="单位" width="90" />
              <el-table-column prop="currency" label="币种" width="90" />
              <el-table-column prop="status" label="状态" width="110" />
            </el-table>
          </el-tab-pane>
          <el-tab-pane label="报告" name="sections">
            <SceneSectionsPanel :scenario-code="detail.scenario_code" :refresh-token="refreshToken" @changed="onChildChanged" />
          </el-tab-pane>
        </el-tabs>
      </template>
    </div>
    <el-dialog v-model="editDisabledVisible" title="编辑停用来源行" width="680px" :close-on-click-modal="false">
      <template v-if="editingDisabledRow">
        <div class="text-muted">{{ editingDisabledRow.sheet_name }} · 第 {{ editingDisabledRow.source.row_no }} 行</div>
        <el-form label-position="top" class="disabled-row-form">
          <el-form-item v-for="column in editingDisabledRow.columns" :key="column.key" :label="column.cell ? `${column.label}（${column.cell}）` : column.label">
            <el-input v-model="disabledValues[column.key]" />
          </el-form-item>
        </el-form>
      </template>
      <template #footer>
        <el-button @click="editDisabledVisible = false">取消</el-button>
        <el-button type="primary" :loading="editDisabledSaving" @click="saveDisabledRow">保存</el-button>
      </template>
    </el-dialog>
    <el-dialog v-model="sourcePreviewVisible" title="来源解析预览" width="min(1100px, 94vw)" :close-on-click-modal="false">
      <template v-if="sourcePreview">
        <p>本次解析 {{ sourcePreview.rows.length }} 行，生成 {{ sourcePreview.items.length }} 项配置。</p>
        <el-alert v-if="sourcePreview.notices.length" type="warning" :closable="false" title="以下说明不阻断已有候选追加；未导入的原始来源会保留。" />
        <p v-for="notice in sourcePreview.notices" :key="notice">{{ notice }}</p>
        <el-table :data="sourcePreview.items" border max-height="480">
          <el-table-column prop="kind" label="类型" width="100" />
          <el-table-column prop="name" label="名称" min-width="200" />
          <el-table-column prop="description" label="业务定义" min-width="440" />
        </el-table>
      </template>
      <template #footer>
        <el-button :disabled="sourceConfirmLoading" @click="sourcePreviewVisible = false">关闭</el-button>
        <el-button type="primary" :loading="sourceConfirmLoading" :disabled="!sourcePreview?.can_activate || sourcePreviewScene !== scenarioCode" @click="confirmParsedSources">确认追加到草稿</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.gap-row {
  display: flex;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 8px;
}

.app-detail-head {
  align-items: center;
  justify-content: space-between;
  gap: 20px;
  padding: 2px 0 18px;
  border-bottom: 1px solid var(--el-border-color-lighter);
}

.app-detail-head__identity,
.app-detail-head__actions,
.app-detail-head__meta {
  display: flex;
  align-items: center;
}

.app-detail-head__identity {
  min-width: 0;
  gap: 14px;
}

.app-detail-head__actions {
  flex: 0 0 auto;
  flex-wrap: wrap;
  justify-content: flex-end;
  gap: 8px;
}

.app-detail-head__divider {
  width: 1px;
  height: 24px;
  background: var(--el-border-color);
}

.app-detail-head__title-wrap {
  min-width: 0;
}

.app-detail-head__name {
  margin: 0;
  overflow: hidden;
  color: var(--el-text-color-primary);
  font-size: 20px;
  font-weight: 650;
  line-height: 1.35;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.app-detail-head__meta {
  flex-wrap: wrap;
  gap: 6px;
  margin-top: 6px;
}

@media (max-width: 900px) {
  .app-detail-head {
    align-items: stretch;
    flex-direction: column;
  }

  .app-detail-head__actions {
    justify-content: flex-start;
  }
}

@media (max-width: 560px) {
  .app-detail-head__identity {
    align-items: flex-start;
    gap: 10px;
  }

  .app-detail-head__divider {
    display: none;
  }

  .app-detail-head__identity {
    flex-wrap: wrap;
  }

  .app-detail-head__title-wrap {
    flex: 1 1 calc(100% - 140px);
  }

  .app-detail-head__actions {
    width: 100%;
  }

  .app-detail-head__actions .el-button {
    flex: 1 1 auto;
  }
}

.disabled-row-form {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(min(100%, 260px), 1fr));
  gap: 0 16px;
  max-height: 58vh;
  overflow-y: auto;
  margin-top: 16px;
}

.source-cell {
  display: inline-block;
  margin-right: 16px;
}
</style>
