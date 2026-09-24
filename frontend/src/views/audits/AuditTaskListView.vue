<script setup lang="ts">
import { computed, onMounted, reactive, ref, watch } from "vue";
import { useRoute, useRouter } from "vue-router";
import { ElMessage, ElMessageBox } from "element-plus";

import type { AuditTaskRow, PlanSelection } from "@/api/contracts";
import { operationIdempotencyKey } from "@/api/http";
import {
  copyAuditTask,
  deleteAuditTask,
  listAuditTasksPage,
  restoreAuditTask,
  updateAuditTask,
} from "@/api/audits";
import {
  createTaskFromPlan,
  getPlan,
  listPlanOptions,
  type PlanOption,
} from "@/api/plans";
import { listSceneOptions, type ScenarioOption } from "@/api/scenes";
import { usePagedList } from "@/composables/usePagedList";
import PageHeader from "@/components/PageHeader.vue";
import StatusBadge from "@/components/StatusBadge.vue";
import type { DetailTab } from "@/constants/auditWorkspaces";
import { nodeLabel, nodeOptions, statusLabel } from "@/constants/labels";

/**
 * 审核任务列表：任务只能从已发布且启用的方案版本创建；
 * 旧任务没有方案关联时显示“历史任务配置”，不伪造关联、不改写历史结果。
 */
const route = useRoute();
const router = useRouter();

const list = usePagedList<AuditTaskRow, {
  name: string;
  scenario_code: string;
  plan_code: string;
  legacy: string;
  status: string;
  current_node: string;
  created_from: string;
  created_to: string;
  deleted: boolean;
}>({
  key: "audit-tasks",
  initialFilters: {
    name: "",
    scenario_code: "",
    plan_code: "",
    legacy: "",
    status: "",
    current_node: "",
    created_from: "",
    created_to: "",
    deleted: false,
  },
  fetch: (params) =>
    listAuditTasksPage({
      name: params.name || undefined,
      scenario_code: params.scenario_code || undefined,
      plan_code: params.plan_code || undefined,
      legacy: params.legacy === "" ? undefined : params.legacy === "true",
      status: params.status || undefined,
      current_node: params.current_node || undefined,
      created_from: params.created_from || undefined,
      created_to: params.created_to || undefined,
      deleted: params.deleted,
      page: params.page,
      page_size: params.page_size,
    }),
});

const { filters, page, pageSize, total, items, loading, error } = list;

const statusOptions = [
  { value: "READY", label: "待执行" },
  { value: "RUNNING", label: "执行中" },
  { value: "WAITING_REVIEW", label: "等待人工复核" },
  { value: "BLOCKED", label: "已阻断" },
  { value: "COMPLETED", label: "已完成" },
  { value: "FAILED", label: "执行失败" },
];

const selectedRows = ref<AuditTaskRow[]>([]);
const editDisabled = computed(() => selectedRows.value.length !== 1);
const deleteDisabled = computed(() => selectedRows.value.length === 0);

/** 方案筛选下拉：与建任务一致，只列启用且已有发布版本的方案。 */
const planFilterOptions = ref<PlanOption[]>([]);

/** 进入任务详情并打开指定页签；没有页签时由任务详情页按「材料」页处理。 */
function openTask(row: AuditTaskRow, tab?: DetailTab): void {
  void router.push({
    name: "audit-task-detail",
    params: { taskNo: row.task_no },
    query: tab ? { tab } : {},
  });
}

/**
 * 操作列的主入口。每个状态只有一个明确的下一步，其余入口收进「更多」菜单：
 *   待执行   → 继续审核；
 *   执行中   → 查看进度；
 *   等待复核 → 进入复核；
 *   已完成   → 查看结果；
 *   失败/阻断 → 查看原因。
 */
interface PrimaryEntry {
  label: string;
  tab?: DetailTab;
}

function primaryEntry(row: AuditTaskRow): PrimaryEntry {
  if (row.status === "RUNNING") {
    return { label: "查看进度", tab: "executions" };
  }
  if (row.status === "WAITING_REVIEW") {
    return { label: "进入复核", tab: "reviews" };
  }
  if (row.status === "COMPLETED") {
    return { label: "查看结果", tab: "rules" };
  }
  if (row.status === "FAILED" || row.status === "BLOCKED") {
    return { label: "查看原因", tab: "executions" };
  }
  return { label: "继续审核", tab: "materials" };
}

/* 新增任务 */
const scenes = ref<ScenarioOption[]>([]);
const planOptions = ref<PlanOption[]>([]);
const createVisible = ref(false);
const createSaving = ref(false);
const createPrefillLoading = ref(false);
const createError = ref<string | null>(null);
const createForm = reactive({ scenario_code: "", plan_code: "", task_name: "", remark: "" });
let createAttemptKey: string | null = null;
let createAttemptPayload: string | null = null;
let planOptionsRequest = 0;
let snapshotRequest = 0;
let createOpenRequest = 0;

async function openCreate(planCode?: string): Promise<void> {
  const request = ++createOpenRequest;
  createVisible.value = true;
  // 每次打开弹窗生成新的幂等标识：失败重开时不会与上一次请求的内容冲突。
  createAttemptKey = null;
  createAttemptPayload = null;
  ++planOptionsRequest;
  ++snapshotRequest;
  createSnapshot.value = null;
  snapshotLoading.value = false;
  snapshotError.value = null;
  planOptions.value = [];
  createError.value = null;
  createForm.scenario_code = "";
  createForm.plan_code = "";
  createForm.task_name = "";
  createForm.remark = "";
  createPrefillLoading.value = true;
  try {
    const payload = await listSceneOptions();
    if (request !== createOpenRequest || !createVisible.value) return;
    scenes.value = payload.items.filter((item) => item.enabled);
    if (!planCode) return;
    const selected = await getPlan(planCode);
    if (request !== createOpenRequest || !createVisible.value) return;
    if (selected.deleted || !selected.enabled || !selected.effective) {
      createError.value = "该方案当前不可用于创建任务，请在方案管理中核对启用状态和配置范围。";
      return;
    }
    if (!scenes.value.some((item) => item.scenario_code === selected.scenario_code)) {
      createError.value = "该方案所属场景当前不可用于创建任务。";
      return;
    }
    createForm.scenario_code = selected.scenario_code;
    await onScenarioChange();
    if (request !== createOpenRequest || !createVisible.value || createError.value) return;
    if (!planOptions.value.some((item) => item.plan_code === planCode)) {
      createError.value = "该方案不在当前可用方案列表中，请刷新后核对方案状态。";
      return;
    }
    createForm.plan_code = planCode;
  } catch (failure) {
    if (request === createOpenRequest && createVisible.value) {
      createError.value = failure instanceof Error ? failure.message : "创建任务所需信息读取失败";
    }
  } finally {
    if (request === createOpenRequest) createPrefillLoading.value = false;
  }
}

async function onScenarioChange(): Promise<void> {
  const scenarioCode = createForm.scenario_code;
  const request = ++planOptionsRequest;
  createForm.plan_code = "";
  planOptions.value = [];
  createError.value = null;
  if (!scenarioCode) {
    return;
  }
  try {
    const payload = await listPlanOptions(scenarioCode);
    if (request !== planOptionsRequest || createForm.scenario_code !== scenarioCode) {
      return;
    }
    planOptions.value = payload.items;
    if (!payload.items.length) {
      createError.value = "该场景还没有范围已生效的启用方案：请先在方案管理中建立方案并保存成功。";
    } else {
      createError.value = null;
    }
  } catch (failure) {
    if (request === planOptionsRequest && createForm.scenario_code === scenarioCode) {
      createError.value = failure instanceof Error ? failure.message : "方案列表读取失败";
    }
  }
}

/** 创建时校验方案配置，并提交范围哈希以防止并发修改。 */
const createSnapshot = ref<PlanSelection | null>(null);
const snapshotLoading = ref(false);
const snapshotError = ref<string | null>(null);
const canCreateTask = computed(() => Boolean(
  createSnapshot.value?.effective
  && createSnapshot.value.scope_hash
  && createSnapshot.value.plan_code === createForm.plan_code,
));

async function loadCreateSnapshot(): Promise<void> {
  const planCode = createForm.plan_code;
  const request = ++snapshotRequest;
  createSnapshot.value = null;
  snapshotError.value = null;
  if (!planCode) {
    snapshotLoading.value = false;
    return;
  }
  snapshotLoading.value = true;
  try {
    const snapshot = await getPlan(planCode);
    if (request === snapshotRequest && createForm.plan_code === planCode) {
      createSnapshot.value = snapshot;
    }
  } catch (failure) {
    if (request === snapshotRequest && createForm.plan_code === planCode) {
      snapshotError.value = failure instanceof Error ? failure.message : "方案范围读取失败";
    }
  } finally {
    if (request === snapshotRequest) {
      snapshotLoading.value = false;
    }
  }
}

watch(
  () => createForm.plan_code,
  () => void loadCreateSnapshot(),
);

watch(
  () => route.query.createPlan,
  (value) => {
    if (typeof value !== "string" || !value) return;
    void openCreate(value);
    void router.replace({ query: { ...route.query, createPlan: undefined } });
  },
  { immediate: true },
);

async function submitCreate(): Promise<void> {
  if (!createForm.plan_code) {
    ElMessage.warning("请选择方案");
    return;
  }
  if (!createForm.task_name.trim()) {
    ElMessage.warning("请填写任务名称");
    return;
  }
  const snapshot = createSnapshot.value;
  if (!snapshot?.effective || !snapshot.scope_hash || snapshot.plan_code !== createForm.plan_code) {
    ElMessage.warning("方案范围尚未读取或已变化，请重新核对后创建任务");
    return;
  }
  const taskName = createForm.task_name.trim();
  const remark = createForm.remark.trim() || null;
  const attemptPayload = JSON.stringify([createForm.plan_code, taskName, remark, snapshot.scope_hash]);
  const attemptKey = createAttemptPayload === attemptPayload && createAttemptKey
    ? createAttemptKey
    : operationIdempotencyKey("TASK_CREATE", createForm.plan_code, taskName);
  createAttemptKey = attemptKey;
  createAttemptPayload = attemptPayload;
  createSaving.value = true;
  createError.value = null;
  try {
    const created = await createTaskFromPlan(createForm.plan_code, {
      task_name: taskName,
      remark,
      expected_scope_hash: snapshot.scope_hash,
      idempotency_key: attemptKey,
    });
    createAttemptKey = null;
    createAttemptPayload = null;
    createVisible.value = false;
    if (created.reused) {
      ElMessage.success("该请求此前已创建任务，已打开原任务");
    } else {
      ElMessage.success("审核任务已创建");
    }
    await router.push({ name: "audit-task-detail", params: { taskNo: created.task_no } });
  } catch (failure) {
    createError.value = failure instanceof Error ? failure.message : "创建任务失败";
  } finally {
    createSaving.value = false;
  }
}

/* 编辑 */
const editVisible = ref(false);
const editRow = ref<AuditTaskRow | null>(null);
const editForm = reactive({ task_name: "", remark: "" });
const editSaving = ref(false);

function openEdit(row: AuditTaskRow): void {
  editRow.value = row;
  editForm.task_name = row.task_name;
  editForm.remark = row.remark ?? "";
  editVisible.value = true;
}

async function submitEdit(): Promise<void> {
  if (!editRow.value || !editForm.task_name.trim()) {
    ElMessage.warning("请填写任务名称");
    return;
  }
  editSaving.value = true;
  try {
    await updateAuditTask(editRow.value.task_no, {
      task_name: editForm.task_name.trim(),
      remark: editForm.remark.trim() || null,
    });
  ElMessage.success("任务资料已保存（冻结的创建时范围不受影响）");
    editVisible.value = false;
    await list.load();
  } catch (failure) {
    ElMessage.error(failure instanceof Error ? failure.message : "保存失败");
  } finally {
    editSaving.value = false;
  }
}

/* 复制 */
const copyVisible = ref(false);
const copyRowTarget = ref<AuditTaskRow | null>(null);
const copyName = ref("");
const copySaving = ref(false);
let copyAttemptKey: string | null = null;

function openCopy(row: AuditTaskRow): void {
  copyRowTarget.value = row;
  copyName.value = `${row.task_name} 副本`;
  copyVisible.value = true;
}

async function submitCopy(): Promise<void> {
  if (!copyRowTarget.value || !copyName.value.trim()) {
    ElMessage.warning("请填写新任务名称");
    return;
  }
  copySaving.value = true;
  try {
    // 与新建任务同一口径：幂等标识在本次尝试内复用，响应丢失后重发返回原任务。
    copyAttemptKey =
      copyAttemptKey ?? operationIdempotencyKey("TASK_COPY", copyRowTarget.value.task_no, copyName.value.trim());
    const created = await copyAuditTask(copyRowTarget.value.task_no, copyName.value.trim(), copyAttemptKey);
    copyAttemptKey = null;
    ElMessage.success("已按方案当前范围创建新任务（材料与结果重新开始，原任务报告保留）");
    copyVisible.value = false;
    await router.push({ name: "audit-task-detail", params: { taskNo: created.task_no } });
  } catch (failure) {
    ElMessage.error(failure instanceof Error ? failure.message : "复制失败");
  } finally {
    copySaving.value = false;
  }
}

/* 删除 / 恢复 */
async function removeRow(row: AuditTaskRow): Promise<void> {
  try {
    await ElMessageBox.confirm(
      `确认删除任务「${row.task_name}」？删除为逻辑删除：历史事实、文件、证据与报告保留；执行中的任务会被服务端拒绝。`,
      "删除审核任务",
      { type: "warning", confirmButtonText: "删除", cancelButtonText: "取消" },
    );
  } catch {
    return;
  }
  try {
    await deleteAuditTask(row.task_no);
    ElMessage.success("任务已移入已删除列表");
    selectedRows.value = [];
    await list.load();
  } catch (failure) {
    ElMessage.error(failure instanceof Error ? failure.message : "删除失败");
  }
}

async function deleteSelected(): Promise<void> {
  const targets = selectedRows.value;
  if (!targets.length) {
    return;
  }
  const failures: string[] = [];
  try {
    await ElMessageBox.confirm(`确认删除选中的 ${targets.length} 个任务？删除为逻辑删除，历史资料保留。`, "批量删除任务", {
      type: "warning",
      confirmButtonText: "删除",
      cancelButtonText: "取消",
    });
  } catch {
    return;
  }
  for (const row of targets) {
    try {
      await deleteAuditTask(row.task_no);
    } catch (failure) {
      failures.push(`${row.task_name}：${failure instanceof Error ? failure.message : "未知原因"}`);
    }
  }
  if (failures.length) {
    ElMessage.warning(`部分删除失败：${failures.join("；")}`);
  } else {
    ElMessage.success("选中的任务已移入已删除列表");
  }
  selectedRows.value = [];
  await list.load();
}

async function restoreRow(row: AuditTaskRow): Promise<void> {
  try {
    await restoreAuditTask(row.task_no);
    ElMessage.success("任务已恢复");
    await list.load();
  } catch (failure) {
    ElMessage.error(failure instanceof Error ? failure.message : "恢复失败");
  }
}

onMounted(async () => {
  await loadFilterOptions();
  void list.load();
});

/** 筛选下拉是真实数据；读取失败时保留可见原因，不静默变成空列表。 */
const filterOptionsError = ref<string | null>(null);

async function loadFilterOptions(): Promise<void> {
  filterOptionsError.value = null;
  const failures: string[] = [];
  try {
    scenes.value = (await listSceneOptions()).items;
  } catch (failure) {
    scenes.value = [];
    failures.push(`场景：${failure instanceof Error ? failure.message : "读取失败"}`);
  }
  try {
    planFilterOptions.value = (await listPlanOptions()).items;
  } catch (failure) {
    planFilterOptions.value = [];
    failures.push(`方案：${failure instanceof Error ? failure.message : "读取失败"}`);
  }
  filterOptionsError.value = failures.length ? failures.join("；") : null;
}
</script>

<template>
  <div class="app-panel">
    <PageHeader
      eyebrow="审核管理"
      title="审核任务"
      subtitle="任务只能从已发布且启用的方案版本创建；创建时冻结场景版本与规则快照，之后不随升级变化。"
      :show-refresh="false"
    />
    <el-form inline class="app-query" @submit.prevent="list.search()">
      <el-form-item label="任务/编号">
        <el-input v-model="filters.name" clearable placeholder="任务名称或任务编号" style="width: 200px" @keyup.enter="list.search()" />
      </el-form-item>
      <el-form-item label="场景">
        <el-select v-model="filters.scenario_code" clearable filterable placeholder="全部场景" style="width: 200px">
          <el-option v-for="item in scenes" :key="item.scenario_code" :value="item.scenario_code" :label="item.scenario_name" />
        </el-select>
      </el-form-item>
      <el-form-item label="方案">
        <el-select v-model="filters.plan_code" clearable filterable placeholder="全部方案" style="width: 200px">
          <el-option
            v-for="item in planFilterOptions"
            :key="item.plan_code"
            :value="item.plan_code"
            :label="item.plan_name"
          />
        </el-select>
      </el-form-item>
      <el-form-item label="任务状态">
        <el-select v-model="filters.status" clearable placeholder="全部" style="width: 150px">
          <el-option v-for="item in statusOptions" :key="item.value" :value="item.value" :label="item.label" />
        </el-select>
      </el-form-item>
      <el-form-item label="当前节点">
        <el-select v-model="filters.current_node" clearable filterable placeholder="全部节点" style="width: 150px">
          <el-option v-for="item in nodeOptions" :key="item.value" :value="item.value" :label="item.label" />
        </el-select>
      </el-form-item>
      <el-form-item label="方案归属">
        <el-select v-model="filters.legacy" clearable placeholder="全部" style="width: 150px">
          <el-option value="true" label="仅历史任务配置" />
          <el-option value="false" label="仅方案任务" />
        </el-select>
      </el-form-item>
      <el-form-item label="创建时间">
        <el-date-picker v-model="filters.created_from" type="date" value-format="YYYY-MM-DD" placeholder="开始日期" style="width: 150px" />
        <span style="margin: 0 6px">至</span>
        <el-date-picker v-model="filters.created_to" type="date" value-format="YYYY-MM-DD" placeholder="结束日期" style="width: 150px" />
      </el-form-item>
      <el-form-item>
        <el-button type="primary" @click="list.search()">查询</el-button>
      </el-form-item>
    </el-form>

    <div class="app-toolbar">
      <div class="app-toolbar__left">
        <template v-if="!filters.deleted">
          <el-button type="primary" @click="openCreate()">新增</el-button>
          <el-button type="success" plain :disabled="editDisabled" @click="selectedRows[0] && openEdit(selectedRows[0])">编辑</el-button>
          <el-button type="danger" plain :disabled="deleteDisabled" @click="deleteSelected">删除</el-button>
        </template>
      </div>
      <div class="app-toolbar__right">
        <el-checkbox v-model="filters.deleted" @change="list.search()">已删除</el-checkbox>
      </div>
    </div>

    <el-alert v-if="filterOptionsError" type="warning" show-icon :closable="false" :title="`筛选下拉读取失败：${filterOptionsError}`">
      对应的筛选下拉为空，筛选条件仍可手工输入。
      <el-button size="small" @click="loadFilterOptions">重试</el-button>
    </el-alert>

    <el-alert v-if="error" type="error" show-icon :closable="false" :title="`读取失败：${error}`">
      <el-button size="small" @click="list.load()">重试</el-button>
    </el-alert>

    <el-table v-else :data="items" border stripe v-loading="loading" empty-text="暂无数据" @selection-change="selectedRows = $event">
      <el-table-column type="selection" width="50" />
      <el-table-column label="任务编号" width="170">
        <template #default="{ row }">
          <el-button link type="primary" class="table-link mono" @click="openTask(row)">{{ row.task_no }}</el-button>
        </template>
      </el-table-column>
      <el-table-column label="任务名称" min-width="200" show-overflow-tooltip>
        <template #default="{ row }">{{ row.task_name }}</template>
      </el-table-column>
      <el-table-column label="场景 / 版本" min-width="180">
        <template #default="{ row }">
          <span>{{ row.scenario_name }}</span>
          <span class="text-hint"> · 第 {{ row.scenario_version_no }} 版</span>
        </template>
      </el-table-column>
      <el-table-column label="方案" min-width="190">
        <template #default="{ row }">
          <span v-if="row.is_legacy_config" class="text-hint">历史任务配置</span>
          <span v-else>{{ row.plan_name || "方案名称未记录" }}</span>
        </template>
      </el-table-column>
      <el-table-column label="当前阶段" width="130">
        <template #default="{ row }">{{ row.current_node_name || nodeLabel(row.current_node) }}</template>
      </el-table-column>
      <el-table-column label="任务状态" width="130">
        <template #default="{ row }">
          <StatusBadge :status="row.status" :label="statusLabel(row.status)" />
        </template>
      </el-table-column>
      <el-table-column label="创建时间" width="170">
        <template #default="{ row }">{{ String(row.created_at).replace("T", " ").slice(0, 19) }}</template>
      </el-table-column>
      <el-table-column label="更新时间" width="170">
        <template #default="{ row }">{{ String(row.updated_at).replace("T", " ").slice(0, 19) }}</template>
      </el-table-column>
      <el-table-column label="操作" width="150" fixed="right">
        <template #default="{ row }">
          <template v-if="!filters.deleted">
            <el-button link type="primary" size="small" @click="openTask(row, primaryEntry(row).tab)">
              {{ primaryEntry(row).label }}
            </el-button>
            <el-dropdown trigger="click">
              <el-button link type="primary" size="small">更多</el-button>
              <template #dropdown>
                <el-dropdown-menu>
                  <el-dropdown-item @click="openTask(row, 'materials')">材料</el-dropdown-item>
                  <el-dropdown-item @click="openTask(row, 'fields')">字段结果</el-dropdown-item>
                  <el-dropdown-item @click="openTask(row, 'rules')">规则结果</el-dropdown-item>
                  <el-dropdown-item @click="openTask(row, 'reviews')">人工复核</el-dropdown-item>
                  <el-dropdown-item @click="openTask(row, 'risks')">风险</el-dropdown-item>
                  <el-dropdown-item @click="openTask(row, 'report')">报告</el-dropdown-item>
                  <el-dropdown-item divided @click="openEdit(row)">编辑资料</el-dropdown-item>
                  <el-dropdown-item @click="openCopy(row)">复制任务</el-dropdown-item>
                  <el-dropdown-item divided @click="removeRow(row)">删除</el-dropdown-item>
                </el-dropdown-menu>
              </template>
            </el-dropdown>
          </template>
          <template v-else>
            <el-button link type="primary" size="small" @click="restoreRow(row)">恢复</el-button>
            <el-button link type="primary" size="small" @click="openTask(row)">详情</el-button>
          </template>
        </template>
      </el-table-column>
    </el-table>

    <div class="app-pagination">
      <el-pagination
        :current-page="page"
        :page-size="pageSize"
        :total="total"
        :page-sizes="[10, 20, 50, 100]"
        layout="total, sizes, prev, pager, next, jumper"
        background
        @current-change="list.setPage"
        @size-change="list.setPageSize"
      />
    </div>

    <el-dialog v-model="createVisible" title="新增审核任务" width="640px" :close-on-click-modal="false">
      <el-alert v-if="createError" class="block-gap-sm" type="warning" show-icon :closable="false" :title="createError" />
      <el-alert v-if="snapshotError" class="block-gap-sm" type="error" show-icon :closable="false" :title="`方案读取失败：${snapshotError}`">
        <el-button size="small" @click="loadCreateSnapshot">重试</el-button>
      </el-alert>
      <el-alert
        v-if="createSnapshot && !createSnapshot.effective"
        class="block-gap-sm"
        type="warning"
        show-icon
        :closable="false"
        :title="`方案配置尚未生效：${createSnapshot.sync_error?.message ?? '配置不完整'}`"
      />
      <el-form label-width="100px" class="block-gap-sm">
        <el-form-item label="场景" required>
          <el-select v-model="createForm.scenario_code" filterable :disabled="createPrefillLoading" placeholder="选择场景" style="width: 100%" @change="onScenarioChange">
            <el-option v-for="item in scenes" :key="item.scenario_code" :value="item.scenario_code" :label="item.scenario_name" />
          </el-select>
        </el-form-item>
        <el-form-item label="方案" required>
          <el-select v-model="createForm.plan_code" filterable :disabled="createPrefillLoading || !createForm.scenario_code" placeholder="选择已启用的方案" style="width: 100%">
            <el-option v-for="item in planOptions" :key="item.plan_code" :value="item.plan_code" :label="item.plan_name" />
          </el-select>
        </el-form-item>
        <el-form-item label="任务名称" required>
          <el-input v-model="createForm.task_name" maxlength="255" placeholder="例如：供应商甲 9 月合同审核" />
        </el-form-item>
        <el-form-item label="备注">
          <el-input v-model="createForm.remark" type="textarea" :rows="2" maxlength="4000" placeholder="可选" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="createVisible = false">取消</el-button>
        <el-button type="primary" :loading="createSaving" :disabled="createPrefillLoading || snapshotLoading || !canCreateTask" @click="submitCreate">创建任务</el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="editVisible" title="编辑任务资料" width="600px" :close-on-click-modal="false">
      <el-form label-width="100px">
        <el-form-item label="任务名称" required>
          <el-input v-model="editForm.task_name" maxlength="255" />
        </el-form-item>
        <el-form-item label="备注">
          <el-input v-model="editForm.remark" type="textarea" :rows="3" maxlength="4000" />
        </el-form-item>
      </el-form>
      <p class="text-hint">冻结的创建时范围不会随重命名或备注修改变化；已完成任务需要更换输入时，请按方案当前范围重新创建任务。</p>
      <template #footer>
        <el-button @click="editVisible = false">取消</el-button>
        <el-button type="primary" :loading="editSaving" @click="submitEdit">保存</el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="copyVisible" title="复制为新任务" width="600px" :close-on-click-modal="false">
      <el-form label-width="100px">
        <el-form-item label="新任务名称" required>
          <el-input v-model="copyName" maxlength="255" />
        </el-form-item>
      </el-form>
      <p class="text-hint">按所属方案的**当前范围**重新创建一个新任务；材料与结果重新开始，原任务报告保留。</p>
      <template #footer>
        <el-button @click="copyVisible = false">取消</el-button>
        <el-button type="primary" :loading="copySaving" @click="submitCopy">复制</el-button>
      </template>
    </el-dialog>
  </div>
</template>
