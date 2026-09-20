<script setup lang="ts">
import { computed, onMounted, reactive, ref, watch } from "vue";
import { useRoute, useRouter } from "vue-router";
import { ElMessage, ElMessageBox } from "element-plus";

import type { PlanScopePreview, PlanSelection } from "@/api/contracts";
import { listSceneOptions, type ScenarioOption } from "@/api/scenes";
import {
  createPlan,
  getPlan,
  previewPlanScope,
  savePlan,
  updatePlan,
} from "@/api/plans";
import { nodeLabel, planRuleEmptyHint } from "@/constants/labels";

/**
 * 方案编辑：独立页面维护「对某个场景版本的规则选择」，保存即生效。
 * 上方保存基本资料；下方规则选择表实时显示显式选择、自动依赖与未选规则。
 * 方案不自动跟随场景升级；修改方案只影响之后新建的任务。
 */
const route = useRoute();
const router = useRouter();

const planCode = computed(() => (typeof route.params.planCode === "string" ? route.params.planCode : null));
const isCreate = computed(() => planCode.value === null);

const scenes = ref<ScenarioOption[]>([]);
const plan = ref<PlanSelection | null>(null);
const preview = ref<PlanScopePreview | null>(null);
const loading = ref(false);
const loadError = ref<string | null>(null);
const saving = ref(false);
const previewLoading = ref(false);
const previewError = ref<string | null>(null);

const basicForm = reactive({ plan_name: "", scenario_code: "", description: "", enabled: true });
const draftForm = reactive({ rule_keys: [] as string[], stage: "" });

const explicitKeys = computed(() => new Set(draftForm.rule_keys));

// 规则表必须渲染全部三类（显式/自动依赖/未启用）：未启用行可勾选，
// 否则“0 条显式选择”的新方案永远无法勾上第一条规则（历史缺陷：未启用行被过滤后不可见）。
const businessRows = computed(() => preview.value?.rows ?? []);
const unenabledRows = computed(() => (preview.value?.rows ?? []).filter((row) => row.selection === "UNENABLED"));
const stageOptions = computed(() => {
  const codes = Array.from(new Set((preview.value?.rows ?? []).map((row) => row.node).filter((code): code is string => Boolean(code))));
  return codes.map((code) => ({ value: code, label: nodeLabel(code) }));
});
const autoKeys = computed(() => new Set(preview.value?.auto_included_rule_keys ?? []));

async function loadScenes(): Promise<void> {
  const payload = await listSceneOptions();
  scenes.value = payload.items;
}

async function loadPlan(): Promise<void> {
  if (!planCode.value) {
    return;
  }
  const payload = await getPlan(planCode.value);
  plan.value = payload;
  basicForm.plan_name = payload.plan_name;
  basicForm.scenario_code = payload.scenario_code;
  basicForm.description = payload.description ?? "";
  basicForm.enabled = payload.enabled;
  draftForm.rule_keys = [...payload.selected_rule_keys];
  await refreshPreview();
}

async function refreshPreview(): Promise<void> {
  if (!basicForm.scenario_code) {
    preview.value = null;
    return;
  }
  previewLoading.value = true;
  previewError.value = null;
  try {
    preview.value = await previewPlanScope({
      scenario_code: basicForm.scenario_code,
      rule_keys: draftForm.rule_keys,
    });
  } catch (failure) {
    // 预览失败时保留上一次成功的规则清单：否则用户取消最后一条规则后会无法再勾选回来。
    previewError.value = failure instanceof Error ? failure.message : "范围预览失败";
  } finally {
    previewLoading.value = false;
  }
}

watch(
  () => draftForm.rule_keys.slice().sort().join(","),
  () => void refreshPreview(),
);

/**
 * 一步创建方案：先建基本资料，再按已勾选的规则保存一次范围。
 *
 * 保存即生效——没有「已创建但未发布」的中间态：创建成功就是可用方案。
 * 保存失败时保留已创建出来的方案，用户可以在编辑页看到它并继续改，
 * 不回滚成“什么都没发生”，那会让已落库的方案变成看不见的孤儿。
 */
async function createAndSave(): Promise<void> {
  if (!basicForm.plan_name.trim() || !basicForm.scenario_code) {
    ElMessage.warning("请填写方案名称并选择所属场景");
    return;
  }
  if (!draftForm.rule_keys.length) {
    // 与后端 PLAN_RULE_EMPTY 同一口径的文案，直接取用，不另写一套说法。
    ElMessage.warning(planRuleEmptyHint);
    return;
  }
  saving.value = true;
  try {
    const created = await createPlan({
      plan_name: basicForm.plan_name.trim(),
      scenario_code: basicForm.scenario_code,
      description: basicForm.description.trim() || null,
      enabled: basicForm.enabled,
    });
    // 直接按创建结果的 plan_code 继续，不等路由跳转的副作用：
    // 从 plan-new 到 plan-edit 复用同一组件实例，loadPlan 由 watcher 异步触发，
    // 依赖它会让下面的保存读到还没加载完的 plan。
    const saved = await savePlan(created.plan_code, { rule_keys: [...draftForm.rule_keys] });
    await router.replace({ name: "plan-edit", params: { planCode: created.plan_code } });
    if (saved.dropped_rule_keys.length) {
      // 有规则在当前场景配置里不存在：选择已被服务端裁剪，如实说明，不假装全部选中。
      ElMessage.warning(
        `方案已创建，但以下规则在当前场景配置中不存在，已从选择中移除：${saved.dropped_rule_keys.join("、")}。`,
      );
      return;
    }
    ElMessage.success("方案已创建并生效");
  } catch (failure) {
    ElMessage.error(failure instanceof Error ? failure.message : "创建失败");
  } finally {
    saving.value = false;
  }
}

async function saveBasic(): Promise<void> {
  if (!planCode.value || !basicForm.plan_name.trim()) {
    ElMessage.warning("请填写方案名称");
    return;
  }
  saving.value = true;
  try {
    await updatePlan(planCode.value, {
      plan_name: basicForm.plan_name.trim(),
      description: basicForm.description.trim() || null,
      enabled: basicForm.enabled,
    });
    ElMessage.success("方案资料已保存");
    await loadPlan();
  } catch (failure) {
    ElMessage.error(failure instanceof Error ? failure.message : "保存失败");
  } finally {
    saving.value = false;
  }
}

/**
 * 保存方案：写规则选择并立即重算范围（保存即生效）。
 *
 * 被裁剪的规则必须如实回给用户：规则在当前场景配置里不存在时服务端会直接移除，
 * 如果只弹一句「保存成功」，用户会以为选中的规则都还在。
 */
async function saveDraft(): Promise<boolean> {
  if (!planCode.value) {
    return false;
  }
  if (!draftForm.rule_keys.length) {
    ElMessage.warning(planRuleEmptyHint);
    return false;
  }
  saving.value = true;
  try {
    const payload = await savePlan(planCode.value, {
      rule_keys: [...draftForm.rule_keys],
    });
    await loadPlan();
    if (payload.dropped_rule_keys.length) {
      ElMessage.warning(
        `方案已保存，但以下规则在当前场景配置中不存在，已从选择中移除：${payload.dropped_rule_keys.join("、")}。`,
      );
    } else if (payload.effective) {
      ElMessage.success("方案已保存并生效");
    } else {
      // 保存成功但范围没算出来：如实说明原因，不谎报「已生效」。
      ElMessage.warning(`方案已保存，但范围尚未生效：${payload.sync_error?.message ?? "配置不完整"}。`);
    }
    return true;
  } catch (failure) {
    ElMessage.error(failure instanceof Error ? failure.message : "保存失败");
    return false;
  } finally {
    saving.value = false;
  }
}

function toggleRule(ruleKey: string, checked: boolean): void {
  if (checked) {
    if (!draftForm.rule_keys.includes(ruleKey)) {
      draftForm.rule_keys = [...draftForm.rule_keys, ruleKey];
    }
  } else {
    draftForm.rule_keys = draftForm.rule_keys.filter((key) => key !== ruleKey);
  }
}

/** 当前阶段筛选下实际渲染的业务规则行；未选阶段时是整表。 */
const filteredRows = computed(() => {
  const stage = draftForm.stage;
  const rows = [...businessRows.value];
  return stage ? rows.filter((row) => row.node === stage) : rows;
});

/** 当前阶段筛选下可批量勾选的业务规则（自动依赖不能单独取消，未启用规则不在表内）。 */
const stageToggleKeys = computed(() =>
  filteredRows.value.filter((row) => row.selection !== "AUTO").map((row) => row.rule_key),
);

/** 当前阶段是否已全部显式选入；用于给批量按钮一个准确的动作文案。 */
const stageAllSelected = computed(
  () => stageToggleKeys.value.length > 0 && stageToggleKeys.value.every((key) => explicitKeys.value.has(key)),
);

/**
 * 按当前阶段筛选批量勾选。
 * 不筛选（全部阶段）时对整表生效；按钮文案只说事实，不写成“全选/取消”这种先点再看的说法。
 */
function toggleCurrentStage(): void {
  const next = new Set(draftForm.rule_keys);
  for (const key of stageToggleKeys.value) {
    if (stageAllSelected.value) {
      next.delete(key);
    } else {
      next.add(key);
    }
  }
  draftForm.rule_keys = Array.from(next);
}

async function unsavedHint(): Promise<boolean> {
  if (!plan.value) {
    return true;
  }
  const current = [...draftForm.rule_keys].sort().join(",");
  const saved = [...plan.value.selected_rule_keys].sort().join(",");
  if (current !== saved) {
    try {
      await ElMessageBox.confirm("规则选择有未保存的改动，确认离开？", "未保存的改动", {
        type: "warning",
        confirmButtonText: "离开",
        cancelButtonText: "留下",
      });
      return true;
    } catch {
      return false;
    }
  }
  return true;
}

async function back(): Promise<void> {
  if (await unsavedHint()) {
    await router.push({ name: "plans" });
  }
}

async function switchScenario(): Promise<void> {
  const previous = plan.value?.scenario_code;
  if (previous && previous !== basicForm.scenario_code) {
    try {
      await ElMessageBox.confirm(
        "切换所属场景会清空当前规则选择（不同场景的规则不可混用），需要重新选择并保存。是否继续？",
        "切换所属场景",
        { type: "warning", confirmButtonText: "切换", cancelButtonText: "取消" },
      );
    } catch {
      basicForm.scenario_code = previous;
      return;
    }
    draftForm.rule_keys = [];
  }
}

async function applyScenarioChange(): Promise<void> {
  await switchScenario();
  await refreshPreview();
}

async function initialise(): Promise<void> {
  loading.value = true;
  loadError.value = null;
  try {
    await loadScenes();
    if (isCreate.value) {
      // 从场景页跳进来时带 scenario；直接进新增页时默认落在第一个启用场景，
      // 否则规则选择区一开始是空的，用户得先手动选一次场景才能看到可选规则。
      const scenarioFromQuery = typeof route.query.scenario === "string" ? route.query.scenario : "";
      const fallback = scenes.value.find((item) => item.enabled) ?? scenes.value[0] ?? null;
      basicForm.scenario_code = scenarioFromQuery || fallback?.scenario_code || "";
      if (basicForm.scenario_code) {
        await refreshPreview();
      }
    } else {
      await loadPlan();
    }
  } catch (failure) {
    loadError.value = failure instanceof Error ? failure.message : "加载失败";
  } finally {
    loading.value = false;
  }
}

// 创建方案后 router.replace 从 plan-new 到 plan-edit 复用同一组件实例，onMounted 不会重跑；
// 必须侦听方案编码变化重新加载，否则规则清单为空。
watch(planCode, () => {
  void initialise();
});

onMounted(() => {
  void initialise();
});
</script>

<template>
  <div class="app-panel">
    <div class="app-detail-head">
      <el-button text aria-label="返回方案列表" @click="back">← 返回</el-button>
      <span class="app-detail-head__name">{{ isCreate ? "新增方案" : `编辑方案：${basicForm.plan_name}` }}</span>
      <el-tag v-if="plan" size="small" effect="plain" disable-transitions>
        {{ plan.effective ? "已生效" : "待核对" }}
      </el-tag>
    </div>

    <el-alert v-if="loadError" type="error" show-icon :closable="false" :title="loadError" />

    <div v-else v-loading="loading">
      <h3 class="app-section-title">基本信息</h3>
      <el-form label-width="110px" class="app-reading">
        <el-form-item label="方案名称" required>
          <el-input v-model="basicForm.plan_name" maxlength="255" placeholder="例如：采购合同全流程审核方案" />
        </el-form-item>
        <el-form-item label="所属场景" required>
          <el-select
            v-model="basicForm.scenario_code"
            filterable
            :disabled="!isCreate"
            placeholder="选择审核场景"
            style="width: 100%"
            @change="applyScenarioChange"
          >
            <el-option v-for="item in scenes" :key="item.scenario_code" :value="item.scenario_code" :label="`${item.scenario_name}${item.enabled ? '' : '（已停用）'}`" />
          </el-select>
          <div class="text-hint">方案保存的是对该场景的规则选择；不复制字段与规则定义。</div>
        </el-form-item>
        <el-form-item label="方案说明">
          <el-input v-model="basicForm.description" type="textarea" :rows="2" maxlength="4000" />
        </el-form-item>
        <el-form-item label="启用状态">
          <el-switch v-model="basicForm.enabled" active-text="启用" inactive-text="停用" />
        </el-form-item>
        <el-form-item v-if="!isCreate">
          <el-button :loading="saving" @click="saveBasic">保存基本资料</el-button>
        </el-form-item>
      </el-form>

      <div class="app-section-head">
        <h3 class="app-section-title">审核范围（规则选择）</h3>
        <div class="card-actions">
          <template v-if="isCreate">
            <el-button type="primary" :loading="saving" :disabled="!preview" @click="createAndSave">
              创建方案
            </el-button>
          </template>
          <template v-else>
            <el-button type="primary" :loading="saving" :disabled="!preview" @click="saveDraft">
              保存方案
            </el-button>
          </template>
        </div>
      </div>
      <el-form inline class="app-query">
        <el-form-item label="阶段筛选">
          <el-select v-model="draftForm.stage" clearable placeholder="全部阶段" style="width: 180px">
            <el-option v-for="item in stageOptions" :key="item.value" :value="item.value" :label="item.label" />
          </el-select>
        </el-form-item>
        <el-form-item>
          <el-button :disabled="!stageToggleKeys.length" @click="toggleCurrentStage">
            {{ draftForm.stage ? `本阶段${stageAllSelected ? "取消全选" : "全选"}` : stageAllSelected ? "取消全选" : "全选" }}（{{ stageToggleKeys.length }} 条）
          </el-button>
        </el-form-item>
        <el-form-item>
          <span class="text-muted">
            已选 {{ draftForm.rule_keys.length }} 条显式规则 · 自动依赖 {{ preview?.auto_included_rule_keys.length ?? 0 }} 条 ·
            未启用 {{ unenabledRows.length }} 条 · 字段 {{ preview?.field_count ?? 0 }} 个
          </span>
        </el-form-item>
      </el-form>

      <el-alert v-if="previewError" type="error" show-icon :closable="false" :title="`范围预览失败：${previewError}`">
        规则表仍按上一次成功的范围显示；修正后点「重试」重新预览。
        <el-button size="small" @click="refreshPreview">重试</el-button>
      </el-alert>

      <el-empty v-if="!preview" description="请选择所属场景以加载可选规则" />
      <el-table v-else :data="filteredRows" border v-loading="previewLoading" empty-text="暂无规则">
        <el-table-column label="选择" width="70">
          <template #default="{ row }">
            <el-checkbox
              :model-value="row.selection !== 'UNENABLED'"
              :disabled="row.selection === 'AUTO'"
              @update:model-value="(value: string | number | boolean) => toggleRule(row.rule_key, Boolean(value))"
            />
          </template>
        </el-table-column>
        <el-table-column prop="name" label="规则名称" min-width="220" show-overflow-tooltip />
        <el-table-column label="阶段" width="130">
          <template #default="{ row }">{{ nodeLabel(row.node) }}</template>
        </el-table-column>
        <el-table-column label="选入方式" width="140">
          <template #default="{ row }">
            <el-tag v-if="row.selection === 'EXPLICIT'" type="success" size="small" effect="plain" disable-transitions>显式选择</el-tag>
            <el-tag v-else-if="row.selection === 'AUTO'" type="warning" size="small" effect="plain" disable-transitions>
              自动依赖{{ row.system ? "（系统）" : "" }}
            </el-tag>
            <el-tag v-else type="info" size="small" effect="plain" disable-transitions>未启用</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="说明" min-width="260">
          <template #default="{ row }">
            <span v-if="row.selection === 'AUTO' && row.reason">{{ row.reason }}</span>
            <span v-else-if="row.selection === 'AUTO'" class="text-hint">由所选规则的依赖或系统能力自动包含，不能单独取消</span>
            <span v-else-if="row.selection === 'UNENABLED'" class="text-hint">当前未选入本方案</span>
            <span v-else class="text-hint">本方案显式选择</span>
          </template>
        </el-table-column>
      </el-table>

      <el-alert class="block-gap-sm" type="info" show-icon :closable="false" title="保存即生效">
        保存后本方案立即生效，可以用于新建审核任务。修改本方案只影响之后新建的任务：已建任务保留创建时的规则范围快照，不会跟着变。
      </el-alert>

    </div>
  </div>
</template>
