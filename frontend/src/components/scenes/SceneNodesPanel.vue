<script setup lang="ts">
import { computed, ref, watch } from "vue";
import { ElMessage, ElMessageBox } from "element-plus";

import type { ScenarioDraftView } from "@/api/contracts";
import type { DraftNodeItem } from "@/api/contracts/scenes";
import { deleteDraftNode, getSceneDraft, updateDraftNode } from "@/api/scenes";
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
const loading = ref(false);
const loadError = ref<string | null>(null);
const saving = ref(false);

const canEdit = computed(() => Boolean(props.scenarioCode));

function describeError(error: unknown): string {
  return error instanceof Error ? error.message : "操作失败";
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
