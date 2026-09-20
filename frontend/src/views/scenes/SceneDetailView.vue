<script setup lang="ts">
import { computed, onMounted, reactive, ref, watch } from "vue";
import { useRoute, useRouter } from "vue-router";
import { ElMessage, ElMessageBox } from "element-plus";

import type { ScenarioDetail } from "@/api/contracts";
import {
  copyScene,
  deleteScene,
  getScene,
  restoreScene,
  setSceneEnabled,
  updateScene,
} from "@/api/scenes";
import SceneFieldsPanel from "@/components/scenes/SceneFieldsPanel.vue";
import SceneMaterialsPanel from "@/components/scenes/SceneMaterialsPanel.vue";
import SceneRulesPanel from "@/components/scenes/SceneRulesPanel.vue";
import SceneSectionsPanel from "@/components/scenes/SceneSectionsPanel.vue";

/**
 * 场景详情：基本信息 + 配置页签（材料/字段/规则/报告配置）。
 *
 * 配置只有一份、保存即生效，因此这里没有“配置版本”选择与“版本记录”页签。
 * 基本资料与配置分开：改名不改变已建方案与历史报告里的名称快照。
 */
const route = useRoute();
const router = useRouter();

const scenarioCode = computed(() => String(route.params.scenarioCode ?? ""));
const detail = ref<ScenarioDetail | null>(null);
const loading = ref(false);
const loadError = ref<string | null>(null);
const saving = ref(false);
const refreshToken = ref(0);
const tabs = ref("basic");

const isDeleted = computed(() => detail.value?.deleted === true);
const canEditBasic = computed(() => detail.value !== null && !isDeleted.value);

const basicForm = reactive({ scenario_name: "", category: "", description: "", enabled: true });

async function load(): Promise<void> {
  loading.value = true;
  loadError.value = null;
  try {
    const payload = await getScene(scenarioCode.value);
    detail.value = payload;
    basicForm.scenario_name = payload.scenario_name;
    basicForm.category = payload.category ?? "";
    basicForm.description = payload.description ?? "";
    basicForm.enabled = payload.enabled;
  } catch (failure) {
    detail.value = null;
    loadError.value = failure instanceof Error ? failure.message : "场景读取失败";
  } finally {
    loading.value = false;
  }
}

watch(() => scenarioCode.value, () => void load(), { immediate: true });
onMounted(() => {
  if (typeof route.query.tab === "string") {
    tabs.value = route.query.tab;
  }
});

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
      `确认删除场景「${detail.value.scenario_name}」？删除为逻辑删除：历史任务、报告与证据全部保留。`,
      "删除场景",
      { type: "warning", confirmButtonText: "删除", cancelButtonText: "取消" },
    );
  } catch {
    return;
  }
  try {
    await deleteScene(detail.value.scenario_code);
    ElMessage.success("场景已移入已删除列表");
    await load();
  } catch (failure) {
    ElMessage.error(failure instanceof Error ? failure.message : "删除失败");
  }
}

async function restoreScenario(): Promise<void> {
  if (!detail.value) {
    return;
  }
  try {
    await restoreScene(detail.value.scenario_code);
    ElMessage.success("场景已恢复");
    await load();
  } catch (failure) {
    ElMessage.error(failure instanceof Error ? failure.message : "恢复失败");
  }
}

async function copyScenario(): Promise<void> {
  if (!detail.value) {
    return;
  }
  let name = `${detail.value.scenario_name} 副本`;
  try {
    const result = await ElMessageBox.prompt("复制场景会连同当前配置一起复制。", "复制场景", {
      inputValue: name,
      confirmButtonText: "复制",
      cancelButtonText: "取消",
      inputValidator: (value) => (value && value.trim() ? true : "请输入新场景名称"),
    });
    name = result.value.trim();
  } catch {
    return;
  }
  try {
    const created = await copyScene(detail.value.scenario_code, name);
    ElMessage.success("场景已复制");
    await router.push({ name: "scene-detail", params: { scenarioCode: created.scenario_code } });
  } catch (failure) {
    ElMessage.error(failure instanceof Error ? failure.message : "复制失败");
  }
}

function formatDate(value: string | null): string {
  return value ? String(value).replace("T", " ").slice(0, 19) : "—";
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
          <el-button text aria-label="返回场景列表" @click="router.push({ name: 'scenes' })">← 返回</el-button>
          <span class="app-detail-head__name">{{ detail.scenario_name }}</span>
          <el-tag v-if="detail.category" size="small" effect="plain" disable-transitions>{{ detail.category }}</el-tag>
          <el-tag :type="detail.enabled ? 'success' : 'info'" size="small" effect="light" disable-transitions>
            {{ detail.enabled ? "启用" : "停用" }}
          </el-tag>
          <el-tag v-if="isDeleted" type="danger" size="small" effect="light" disable-transitions>已删除</el-tag>
          <div class="card-actions">
            <el-button v-if="!isDeleted" @click="copyScenario">复制场景</el-button>
            <el-button v-if="!isDeleted" @click="toggleEnabled">{{ detail.enabled ? "停用" : "启用" }}</el-button>
            <el-button v-if="!isDeleted" type="danger" plain @click="removeScenario">删除</el-button>
            <el-button v-else type="primary" @click="restoreScenario">恢复场景</el-button>
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
              <el-form-item label="配置状态">
                <el-tag v-if="detail.draft?.sync_error" type="warning" size="small" effect="light" disable-transitions>
                  尚未生效
                </el-tag>
                <el-tag v-else type="success" size="small" effect="light" disable-transitions>已生效</el-tag>
                <span v-if="detail.draft?.sync_error" class="text-hint" style="margin-left: 8px">
                  {{ detail.draft.sync_error.message }}
                </span>
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
                <el-button type="primary" :disabled="!canEditBasic" :loading="saving" @click="saveBasic">保存基本资料</el-button>
              </el-form-item>
            </el-form>
            <el-alert type="info" show-icon :closable="false" title="配置修改保存后立即生效">
              已建方案与任务使用它们创建时的快照，不受后续修改影响；名称修改也不会改变历史报告中的名称快照。
            </el-alert>
          </el-tab-pane>

          <el-tab-pane v-for="tab in [
            { name: 'materials', label: '材料配置' },
            { name: 'fields', label: '字段配置' },
            { name: 'rules', label: '规则配置' },
            { name: 'sections', label: '报告配置' },
          ]" :key="tab.name" :label="tab.label" :name="tab.name">
            <SceneMaterialsPanel v-if="tab.name === 'materials'" :scenario-code="detail.scenario_code" :refresh-token="refreshToken" />
            <SceneFieldsPanel v-else-if="tab.name === 'fields'" :scenario-code="detail.scenario_code" :refresh-token="refreshToken" />
            <SceneRulesPanel v-else-if="tab.name === 'rules'" :scenario-code="detail.scenario_code" :refresh-token="refreshToken" />
            <SceneSectionsPanel v-else :scenario-code="detail.scenario_code" :refresh-token="refreshToken" />
          </el-tab-pane>
        </el-tabs>
      </template>
    </div>
  </div>
</template>
