<script setup lang="ts">
import { computed, reactive, ref, watch } from "vue";
import { useRoute, useRouter } from "vue-router";
import { ElMessage, ElMessageBox } from "element-plus";
import { FileUp, Plus, Power, Rocket, Save, Trash2 } from "lucide-vue-next";

import type {
  ScenarioDetail,
  ScenarioConfigurationView,
} from "@/api/contracts";
import {
  deleteScene,
  getScene,
  getSceneConfiguration,
  publishSceneDraft,
  setSceneEnabled,
  updateScene,
} from "@/api/scenes";
import SceneFieldsPanel from "@/components/scenes/SceneFieldsPanel.vue";
import SceneCalculationsPanel from "@/components/scenes/SceneCalculationsPanel.vue";
import SceneMaterialsPanel from "@/components/scenes/SceneMaterialsPanel.vue";
import SceneRulesPanel from "@/components/scenes/SceneRulesPanel.vue";
import SceneSectionsPanel from "@/components/scenes/SceneSectionsPanel.vue";
import BackButton from "@/components/BackButton.vue";

/**
 * 场景配置按材料、字段、规则、计算和报告组织。
 * 保存只更新草稿；管理员在场景页显式发布后才生成生效版本。
 */
const publishing = ref(false);
const route = useRoute();
const router = useRouter();

const scenarioCode = computed(() => String(route.params.scenarioCode ?? ""));
const returnImportJobNo = computed(() => typeof route.query.importJob === "string" ? route.query.importJob : null);
const detail = ref<ScenarioDetail | null>(null);
const configuration = ref<ScenarioConfigurationView | null>(null);
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
const TAB_NAMES = ["basic", "materials", "fields", "rules", "calculations", "sections"];

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
const calculations = computed(() => configuration.value?.calculations ?? []);
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
    configuration.value = null;
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
      const config = await getSceneConfiguration(payload.scenario_code);
      if (sequence !== loadSequence || code !== scenarioCode.value) return;
      configuration.value = config;
      configError.value = null;
    } catch (error) {
      if (sequence !== loadSequence || code !== scenarioCode.value) return;
      configuration.value = null;
      configError.value = error instanceof Error ? error.message : "场景配置读取失败";
    }
  } catch (failure) {
    if (sequence !== loadSequence || code !== scenarioCode.value) return;
    detail.value = null;
    configuration.value = null;
    loadError.value = failure instanceof Error ? failure.message : "场景读取失败";
  } finally {
    if (sequence === loadSequence && code === scenarioCode.value) loading.value = false;
  }
}

async function onChildChanged(): Promise<void> {
  await load();
  refreshToken.value += 1;
}

watch(scenarioCode, () => void load(), { immediate: true });
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
    refreshToken.value += 1;
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
    if (detail.value?.draft) await onChildChanged();
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
            <el-tag v-if="configError" type="danger" effect="plain">配置读取失败</el-tag>
            <el-tag v-else-if="configuration?.configuration_kind === 'DRAFT'" type="warning" effect="plain">{{ detail.latest_version_no > 0 ? '有未发布修改' : '尚未发布' }}</el-tag>
            <el-tag v-else-if="configuration?.configuration_kind === 'PUBLISHED'" type="success" effect="plain">已发布</el-tag>
            <span class="text-muted">已发布版本：{{ detail.latest_version_no > 0 ? `第 ${detail.latest_version_no} 版` : "无" }}</span>
            <span v-if="configError" class="text-danger">{{ configError }}</span>
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
                <span>{{ detail.draft ? `未发布草稿 · 第 ${detail.draft.revision_no} 次修订` : "无未发布草稿" }}</span>
              </el-form-item>
              <el-form-item label="生效状态">
                <span>{{ configError ? "配置读取失败" : configuration?.configuration_kind === 'DRAFT' ? "草稿尚未发布" : "已发布配置" }}</span>
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
            <SceneCalculationsPanel v-else :key="detail.scenario_code" :items="calculations" />
          </el-tab-pane>
          <el-tab-pane label="报告" name="sections">
            <SceneSectionsPanel :scenario-code="detail.scenario_code" :refresh-token="refreshToken" @changed="onChildChanged" />
          </el-tab-pane>
        </el-tabs>
      </template>
    </div>
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
  flex-wrap: nowrap;
  justify-content: flex-end;
  gap: 8px;
}

.app-detail-head__actions .el-button + .el-button {
  margin-left: 0;
}

.app-detail-head__divider {
  width: 1px;
  height: 24px;
  background: var(--el-border-color);
}

.app-detail-head__title-wrap {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  min-width: 0;
  gap: 8px 12px;
}

.app-detail-head__name {
  margin: 0;
  max-width: 100%;
  overflow: hidden;
  color: var(--el-text-color-primary);
  font-size: 20px;
  font-weight: 650;
  line-height: 1.35;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.app-detail-head__meta {
  flex-shrink: 0;
  flex-wrap: wrap;
  gap: 6px;
}

@media (max-width: 900px) {
  .app-detail-head {
    align-items: stretch;
    flex-direction: column;
  }

  .app-detail-head__actions {
    justify-content: flex-start;
    overflow-x: auto;
  }
}

@media (max-width: 560px) {
  .app-detail-head__identity {
    align-items: center;
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
    flex: 0 0 auto;
  }
}

</style>
