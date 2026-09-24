<script setup lang="ts">
import { computed, reactive, ref, watch } from "vue";
import { ElMessage, ElMessageBox } from "element-plus";

import type { ScenarioDraftView } from "@/api/contracts";
import type { DraftNodeItem } from "@/api/contracts/scenes";
import { createDraftNode, deleteDraftNode, getSceneDraft, updateDraftNode } from "@/api/scenes";
import { getProfileSheets } from "@/api/imports";
import type { ProfileSummary } from "@/api/imports";

/**
 * 节点配置面板：**用户配置**流程节点。
 *
 * 节点的存在、顺序与依赖由用户决定，不由系统按规则内容或固定序列生成。
 * 一个场景可以有两个同类节点（两张报表各一个字段抽取），因此实例码（node_code）
 * 与能力类型（node_type）分开：实例码由用户填写，能力类型从统一模板协议里选。
 *
 * 依赖填的是**上游实例码**：发布期会校验缺失依赖、自依赖、循环依赖与顺序冲突，
 * 这里先做同样的显式校验，让用户在配置时就看到问题。
 *
 * Sheet 绑定不在这里编辑。确认绑定只发生在导入作业写入场景草稿时。
 */
const props = defineProps<{
  scenarioCode: string | null;
  /** 由父页面触发的外部刷新（例如发布后）。 */
  refreshToken?: number;
}>();

const emit = defineEmits<{
  changed: [];
}>();

const nodes = ref<DraftNodeItem[]>([]);
const draft = ref<ScenarioDraftView | null>(null);
const profile = ref<ProfileSummary | null>(null);
const profileError = ref<string | null>(null);
const loading = ref(false);
const loadError = ref<string | null>(null);
const saving = ref(false);

const canEdit = computed(() => Boolean(props.scenarioCode));
const nodeTypeOptions = computed(() => profile.value?.node_types ?? []);

const form = reactive({
  node_code: "",
  node_type: "",
  name: "",
  sequence_no: 1,
  depends_on: [] as string[],
  enabled: true,
});

/** 未删除的实例码集合，供依赖下拉与校验使用。 */
const knownCodes = computed(() => new Set(nodes.value.map((item) => item.node_code)));

function describeError(error: unknown): string {
  return error instanceof Error ? error.message : "操作失败";
}

async function loadProfile(): Promise<void> {
  profileError.value = null;
  try {
    const payload = await getProfileSheets();
    profile.value = {
      profile_version: payload.profile_version,
      display_name: payload.display_name,
      schema_version: payload.schema_version,
      sheet_count: payload.sheets.length,
      node_types: payload.node_types,
      sheets: payload.sheets.map((item) => ({
        sheet_role: item.sheet_role,
        display_name: item.display_name,
        node_types: item.node_types,
        column_count: item.column_count,
      })),
    };
  } catch (error) {
    profile.value = null;
    profileError.value = describeError(error);
  }
}

async function load(): Promise<void> {
  if (!props.scenarioCode) {
    nodes.value = [];
    draft.value = null;
    return;
  }
  loading.value = true;
  loadError.value = null;
  try {
    const payload = await getSceneDraft(props.scenarioCode);
    draft.value = payload;
    nodes.value = payload.nodes ?? [];
  } catch (error) {
    loadError.value = describeError(error);
    nodes.value = [];
  } finally {
    loading.value = false;
  }
}

watch(() => [props.scenarioCode, props.refreshToken], () => void load(), { immediate: true });
watch(() => props.scenarioCode, () => void loadProfile(), { immediate: true });

function nextSequence(): number {
  return nodes.value.reduce((max, item) => Math.max(max, item.sequence_no), 0) + 1;
}

function resetForm(): void {
  form.node_code = "";
  form.node_type = "";
  form.name = "";
  form.sequence_no = nextSequence();
  form.depends_on = [];
  form.enabled = true;
}

const formProblem = computed<string | null>(() => {
  const code = form.node_code.trim();
  if (!code) return "请填写节点实例码（同一场景内唯一，例如 EXT_BS）";
  if (knownCodes.value.has(code)) return `实例码“${code}”已存在；同一场景内实例码必须唯一`;
  if (!form.node_type) return "请选择节点能力类型";
  if (form.depends_on.includes(code)) return "节点不能依赖自己";
  const missing = form.depends_on.filter((item) => !knownCodes.value.has(item));
  if (missing.length) return `依赖的上游节点不存在：${missing.join("、")}`;
  return null;
});

async function submit(): Promise<void> {
  if (!props.scenarioCode || formProblem.value) {
    ElMessage.warning(formProblem.value ?? "当前不能保存");
    return;
  }
  saving.value = true;
  try {
    const updated = await createDraftNode(props.scenarioCode, {
      node_code: form.node_code.trim(),
      node_type: form.node_type,
      name: form.name.trim() || form.node_code.trim(),
      sequence_no: form.sequence_no,
      depends_on: [...form.depends_on],
      enabled: form.enabled,
    });
    draft.value = updated;
    nodes.value = updated.nodes ?? [];
    resetForm();
    ElMessage.success("节点已保存");
    emit("changed");
  } catch (error) {
    ElMessage.error(describeError(error));
  } finally {
    saving.value = false;
  }
}

/** 就地调整顺序：顺序只影响展示与「依赖必须排在前面」的校验，执行依据是依赖。 */
async function moveSequence(item: DraftNodeItem, delta: number): Promise<void> {
  if (!props.scenarioCode) return;
  const target = item.sequence_no + delta;
  if (target < 1 || target > nodes.value.length) return;
  saving.value = true;
  try {
    const updated = await updateDraftNode(props.scenarioCode, item.node_code, { sequence_no: target });
    draft.value = updated;
    nodes.value = updated.nodes ?? [];
    emit("changed");
  } catch (error) {
    ElMessage.error(describeError(error));
  } finally {
    saving.value = false;
  }
}

async function toggleEnabled(item: DraftNodeItem): Promise<void> {
  if (!props.scenarioCode) return;
  saving.value = true;
  try {
    const updated = await updateDraftNode(props.scenarioCode, item.node_code, { enabled: !item.enabled });
    draft.value = updated;
    nodes.value = updated.nodes ?? [];
    emit("changed");
  } catch (error) {
    ElMessage.error(describeError(error));
  } finally {
    saving.value = false;
  }
}

async function remove(item: DraftNodeItem): Promise<void> {
  if (!props.scenarioCode) return;
  try {
    await ElMessageBox.confirm(
      `确认删除节点「${item.name}」？仍被规则或下游节点引用时会被拒绝，请先处理引用。`,
      "删除节点",
      { type: "warning", confirmButtonText: "删除", cancelButtonText: "取消" },
    );
  } catch {
    return;
  }
  saving.value = true;
  try {
    const updated = await deleteDraftNode(props.scenarioCode, item.node_code);
    draft.value = updated;
    nodes.value = updated.nodes ?? [];
    ElMessage.success("节点已删除");
    emit("changed");
  } catch (error) {
    ElMessage.error(describeError(error));
  } finally {
    saving.value = false;
  }
}

function dependLabel(code: string): string {
  const found = nodes.value.find((item) => item.node_code === code);
  return found ? `${found.name}（${found.node_code}）` : code;
}
</script>

<template>
  <div class="app-panel">
    <el-alert v-if="loadError" type="error" show-icon :closable="false" :title="`读取失败：${loadError}`">
      <el-button size="small" @click="load()">重试</el-button>
    </el-alert>

    <el-alert
      v-else-if="profileError"
      type="error"
      show-icon
      :closable="false"
      :title="`统一模板协议读取失败：${profileError}`"
    >
      <el-button size="small" @click="loadProfile">重试</el-button>
    </el-alert>

    <el-card shadow="never" class="block-gap">
      <template #header>新增节点</template>
      <el-form label-width="120px" :disabled="!canEdit || saving">
        <el-form-item label="节点实例码" required>
          <el-input v-model="form.node_code" placeholder="例如 EXT_BS；同一场景内唯一" style="width: 260px" />
          <span class="text-hint" style="margin-left: 8px">实例码是规则归属的依据，一个场景可以有两个同类节点。</span>
        </el-form-item>
        <el-form-item label="能力类型" required>
          <el-select v-model="form.node_type" placeholder="请选择能力类型" style="width: 260px">
            <el-option
              v-for="item in nodeTypeOptions"
              :key="item.node_type"
              :value="item.node_type"
              :label="item.display_name"
            />
          </el-select>
        </el-form-item>
        <el-form-item label="节点名称">
          <el-input v-model="form.name" placeholder="留空则用实例码" style="width: 260px" />
        </el-form-item>
        <el-form-item label="顺序">
          <el-input-number v-model="form.sequence_no" :min="1" />
          <span class="text-hint" style="margin-left: 8px">只用于展示；执行依据是下面的依赖。</span>
        </el-form-item>
        <el-form-item label="上游依赖">
          <el-select v-model="form.depends_on" multiple style="width: 320px" placeholder="不依赖任何节点则留空">
            <el-option v-for="item in nodes" :key="item.node_code" :value="item.node_code" :label="`${item.name}（${item.node_code}）`" />
          </el-select>
        </el-form-item>
        <el-form-item>
          <el-button type="primary" :loading="saving" @click="submit">保存节点</el-button>
          <span v-if="formProblem" class="text-hint" style="margin-left: 8px">{{ formProblem }}</span>
        </el-form-item>
      </el-form>
    </el-card>

    <el-card shadow="never">
      <template #header>已配置节点（{{ nodes.length }}）</template>
      <el-table :data="nodes" border size="small" v-loading="loading" empty-text="还没有配置节点；发布前必须至少配置一个节点">
        <el-table-column label="顺序" width="130">
          <template #default="{ row }">
            <el-button link size="small" :disabled="!canEdit || saving || row.sequence_no <= 1" @click="moveSequence(row, -1)">上移</el-button>
            <span class="mono">{{ row.sequence_no }}</span>
            <el-button link size="small" :disabled="!canEdit || saving || row.sequence_no >= nodes.length" @click="moveSequence(row, 1)">下移</el-button>
          </template>
        </el-table-column>
        <el-table-column label="实例码" width="160">
          <template #default="{ row }"><span class="mono">{{ row.node_code }}</span></template>
        </el-table-column>
        <el-table-column label="能力类型" width="160">
          <template #default="{ row }">{{ row.node_type_label }}<div class="table-note mono">{{ row.node_type }}</div></template>
        </el-table-column>
        <el-table-column label="名称" min-width="160" show-overflow-tooltip>
          <template #default="{ row }">{{ row.name }}</template>
        </el-table-column>
        <el-table-column label="上游依赖" min-width="200" show-overflow-tooltip>
          <template #default="{ row }">
            <span v-if="!row.depends_on.length" class="text-hint">无（流程起点）</span>
            <span v-else>{{ row.depends_on.map(dependLabel).join("、") }}</span>
          </template>
        </el-table-column>
        <el-table-column label="启停" width="90">
          <template #default="{ row }">
            <el-tag :type="row.enabled ? 'success' : 'info'" size="small" effect="light" disable-transitions>
              {{ row.enabled ? "启用" : "停用" }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="150" fixed="right">
          <template #default="{ row }">
            <el-button link type="primary" size="small" :disabled="!canEdit || saving" @click="toggleEnabled(row)">
              {{ row.enabled ? "停用" : "启用" }}
            </el-button>
            <el-button link type="danger" size="small" :disabled="!canEdit || saving" @click="remove(row)">删除</el-button>
          </template>
        </el-table-column>
      </el-table>
      <div class="text-hint block-gap-sm">
        节点顺序与依赖由用户配置；发布时会校验缺失依赖、自依赖、循环依赖与顺序冲突。
        Sheet 与节点的绑定在导入作业里确认，写入场景草稿后才会出现在场景页。
      </div>
    </el-card>
  </div>
</template>
