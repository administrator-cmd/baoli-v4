<script setup lang="ts">
import { computed, onMounted, reactive, ref, watch } from "vue";
import { useRoute, useRouter } from "vue-router";
import { ElMessage, ElMessageBox } from "element-plus";

import type { AuditTaskRow } from "@/api/contracts";
import { operationIdempotencyKey } from "@/api/http";
import {
  deleteAuditTask,
  listAuditTasksPage,
  updateAuditTask,
} from "@/api/audits";
import {
  createTaskFromPlan,
  listPlanOptions,
  type PlanOption,
} from "@/api/plans";
import { usePagedList } from "@/composables/usePagedList";
import StatusBadge from "@/components/StatusBadge.vue";
import type { DetailTab } from "@/constants/auditWorkspaces";
import { formatDate, statusFilterOptions, statusLabel } from "@/constants/labels";
import { PAGE_SIZE_OPTIONS } from "@/constants/pagination";

/**
 * 审核任务列表：任务从已启用且配置生效的方案创建；
 * 方案名称来自任务创建时的冻结快照。
 */
const route = useRoute();
const router = useRouter();

const list = usePagedList<AuditTaskRow, {
  name: string;
  plan_code: string;
  status: string;
  created_from: string;
  created_to: string;
}>({
  key: "audit-tasks",
  initialFilters: {
    name: "",
    plan_code: "",
    status: "",
    created_from: "",
    created_to: "",
  },
  fetch: (params) =>
    listAuditTasksPage({
      name: params.name || undefined,
      plan_code: params.plan_code || undefined,
      status: params.status || undefined,
      created_from: params.created_from || undefined,
      created_to: params.created_to || undefined,
      page: params.page,
      page_size: params.page_size,
    }),
});

const { filters, page, pageSize, total, items, loading, error } = list;

const statusOptions = statusFilterOptions(
  ["READY", "RUNNING", "WAITING_FIELD_REVIEW", "WAITING_REVIEW", "BLOCKED", "COMPLETED", "FAILED"],
  { COMPLETED: "执行已结束" },
);

const selectedRows = ref<AuditTaskRow[]>([]);
const deleteDisabled = computed(() => selectedRows.value.length === 0);

/** 方案筛选下拉：与建任务一致，只列启用且配置生效的方案。 */
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
 * 操作列按状态提供任务详情入口：
 *   待执行   → 进入审核；
 *   执行中   → 查看进度；
 *   历史等待复核 → 查看记录；
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
  if (row.status === "WAITING_FIELD_REVIEW") {
    return { label: "核对字段", tab: "fields" };
  }
  if (row.status === "WAITING_REVIEW") {
    return { label: "查看记录", tab: "executions" };
  }
  if (row.status === "COMPLETED") {
    return { label: "查看结果", tab: "rules" };
  }
  if (row.status === "FAILED" || row.status === "BLOCKED") {
    return { label: "查看原因", tab: "executions" };
  }
  return { label: "进入审核", tab: "materials" };
}

/* 新增任务 */
const planOptions = ref<PlanOption[]>([]);
const createVisible = ref(false);
const createSaving = ref(false);
const createOptionsLoading = ref(false);
const createError = ref<string | null>(null);
const createForm = reactive({ plan_code: "", task_name: "", remark: "" });
let createAttemptKey: string | null = null;
let createAttemptPayload: string | null = null;
let createOpenRequest = 0;

async function openCreate(planCode?: string): Promise<void> {
  const request = ++createOpenRequest;
  createVisible.value = true;
  // 每次打开弹窗生成新的幂等标识：失败重开时不会与上一次请求的内容冲突。
  createAttemptKey = null;
  createAttemptPayload = null;
  planOptions.value = [];
  createError.value = null;
  createForm.plan_code = "";
  createForm.task_name = "";
  createForm.remark = "";
  createOptionsLoading.value = true;
  try {
    const planPayload = await listPlanOptions();
    if (request !== createOpenRequest || !createVisible.value) return;
    planOptions.value = planPayload.items;
    if (!planOptions.value.length) {
      createError.value = "当前没有可用于创建任务的方案。";
    }
    if (!planCode) return;
    if (!planOptions.value.some((item) => item.plan_code === planCode)) {
      createError.value = "该方案当前不可用于创建任务，请核对方案状态。";
      return;
    }
    createForm.plan_code = planCode;
  } catch (failure) {
    if (request === createOpenRequest && createVisible.value) {
      createError.value = failure instanceof Error ? failure.message : "创建任务所需信息读取失败";
    }
  } finally {
    if (request === createOpenRequest) createOptionsLoading.value = false;
  }
}

/**
 * 创建任务使用的方案范围：直接取下拉选项。
 *
 * 选项只包含「启用、未删除、范围已生效」的方案，并带 `scope_hash`；
 * 同一哈希随创建请求提交，服务端据此拒绝打开弹窗后发生的并发修改。
 */
const createSnapshot = computed(() =>
  planOptions.value.find((item) => item.plan_code === createForm.plan_code) ?? null,
);
const canCreateTask = computed(() => Boolean(createSnapshot.value?.scope_hash));

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
  if (!snapshot?.scope_hash) {
    ElMessage.warning("方案范围尚未就绪，请重新选择方案");
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
    ElMessage.success("任务名称与备注已保存");
    editVisible.value = false;
    await list.load();
  } catch (failure) {
    ElMessage.error(failure instanceof Error ? failure.message : "保存失败");
  } finally {
    editSaving.value = false;
  }
}

/* 永久删除 */
async function deleteSelected(): Promise<void> {
  const targets = selectedRows.value;
  if (!targets.length) {
    return;
  }
  const failures: string[] = [];
  try {
    await ElMessageBox.confirm(`确认删除选中的 ${targets.length} 个任务？删除后，这些任务的材料文件和审核结果将一并永久删除，无法恢复。`, "批量删除任务", {
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
    ElMessage.success("选中的任务已永久删除");
  }
  selectedRows.value = [];
  await list.load();
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
    planFilterOptions.value = (await listPlanOptions()).items;
  } catch (failure) {
    planFilterOptions.value = [];
    failures.push(`方案：${failure instanceof Error ? failure.message : "读取失败"}`);
  }
  filterOptionsError.value = failures.length ? failures.join("；") : null;
}
</script>

<template>
  <div class="app-panel audit-task-list">
    <el-form class="app-query task-query" @submit.prevent="list.search()">
      <el-form-item label="任务名称">
        <el-input v-model="filters.name" clearable placeholder="任务名称" @keyup.enter="list.search()" />
      </el-form-item>
      <el-form-item label="方案">
        <el-select v-model="filters.plan_code" clearable filterable placeholder="全部方案">
          <el-option
            v-for="item in planFilterOptions"
            :key="item.plan_code"
            :value="item.plan_code"
            :label="item.plan_name"
          />
        </el-select>
      </el-form-item>
      <el-form-item label="任务状态">
        <el-select v-model="filters.status" clearable placeholder="全部">
          <el-option v-for="item in statusOptions" :key="item.value" :value="item.value" :label="item.label" />
        </el-select>
      </el-form-item>
      <el-form-item label="创建时间" class="task-query__dates">
        <el-date-picker v-model="filters.created_from" type="date" value-format="YYYY-MM-DD" placeholder="开始日期" />
        <span style="margin: 0 6px">至</span>
        <el-date-picker v-model="filters.created_to" type="date" value-format="YYYY-MM-DD" placeholder="结束日期" />
      </el-form-item>
      <el-form-item class="task-query__submit">
        <el-button type="primary" @click="list.search()">查询</el-button>
      </el-form-item>
    </el-form>

    <div class="app-toolbar">
      <div class="app-toolbar__left">
        <el-button type="primary" @click="openCreate()">新增任务</el-button>
        <el-button type="danger" plain :disabled="deleteDisabled" @click="deleteSelected">批量删除</el-button>
      </div>
    </div>

    <el-alert v-if="filterOptionsError" type="warning" show-icon :closable="false" :title="`筛选下拉读取失败：${filterOptionsError}`">
      方案筛选选项读取失败。
      <el-button size="small" @click="loadFilterOptions">重试</el-button>
    </el-alert>

    <el-alert v-if="error" type="error" show-icon :closable="false" :title="`读取失败：${error}`">
      <el-button size="small" @click="list.load()">重试</el-button>
    </el-alert>

    <el-table v-else :data="items" border stripe class="management-table" v-loading="loading" empty-text="暂无数据" @selection-change="selectedRows = $event">
      <el-table-column type="selection" width="50" />
      <el-table-column label="任务名称" min-width="200" show-overflow-tooltip>
        <template #default="{ row }">
          <el-button link type="primary" class="table-link" @click="openTask(row)">{{ row.task_name }}</el-button>
        </template>
      </el-table-column>
      <el-table-column label="方案" min-width="190">
        <template #default="{ row }">
          <span>{{ row.plan_name || "方案名称未记录" }}</span>
        </template>
      </el-table-column>
      <el-table-column label="任务状态" width="140">
        <template #default="{ row }">
          <StatusBadge :status="row.status" :label="row.status === 'COMPLETED' ? '执行已结束' : statusLabel(row.status)" />
        </template>
      </el-table-column>
      <el-table-column label="创建时间" width="200">
        <template #default="{ row }"><span class="table-nowrap">{{ formatDate(String(row.created_at)) }}</span></template>
      </el-table-column>
      <el-table-column label="更新时间" width="200">
        <template #default="{ row }"><span class="table-nowrap">{{ formatDate(String(row.updated_at)) }}</span></template>
      </el-table-column>
      <el-table-column label="操作" width="160" class-name="table-actions" fixed="right">
        <template #default="{ row }">
          <el-button link type="primary" size="small" @click="openTask(row, primaryEntry(row).tab)">
            {{ primaryEntry(row).label }}
          </el-button>
          <el-button link type="primary" size="small" @click="openEdit(row)">编辑</el-button>
        </template>
      </el-table-column>
    </el-table>

    <div class="app-pagination">
      <el-pagination
        :current-page="page"
        :page-size="pageSize"
        :total="total"
        :page-sizes="PAGE_SIZE_OPTIONS"
        layout="total, sizes, prev, pager, next, jumper"
        background
        @current-change="list.setPage"
        @size-change="list.setPageSize"
      />
    </div>

    <el-dialog v-model="createVisible" title="新增审核任务" width="640px" :close-on-click-modal="false">
      <el-alert v-if="createError" class="block-gap-sm" type="warning" show-icon :closable="false" :title="createError" />
      <el-form label-width="100px" class="block-gap-sm">
        <el-form-item label="方案" required>
          <el-select v-model="createForm.plan_code" filterable :loading="createOptionsLoading" :disabled="createOptionsLoading" placeholder="选择已启用的方案" style="width: 100%">
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
        <el-button type="primary" :loading="createSaving" :disabled="createOptionsLoading || !canCreateTask" @click="submitCreate">创建任务</el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="editVisible" title="编辑任务名称与备注" width="600px" :close-on-click-modal="false">
      <el-form label-width="100px">
        <el-form-item label="任务名称" required>
          <el-input v-model="editForm.task_name" maxlength="255" />
        </el-form-item>
        <el-form-item label="备注">
          <el-input v-model="editForm.remark" type="textarea" :rows="3" maxlength="4000" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="editVisible = false">取消</el-button>
        <el-button type="primary" :loading="editSaving" @click="submitEdit">保存</el-button>
      </template>
    </el-dialog>

  </div>
</template>

<style scoped>
.audit-task-list {
  container: task-list / inline-size;
}

.task-query {
  display: grid;
  grid-template-columns: minmax(0, 1fr) minmax(0, 1fr) minmax(0, 0.85fr) minmax(340px, 1.5fr) auto;
  column-gap: 16px;
}

.task-query :deep(.el-form-item) {
  min-width: 0;
  margin-right: 0;
}

.task-query :deep(.el-form-item__content) {
  min-width: 0;
  flex-wrap: nowrap;
}

.task-query :deep(.el-select) {
  width: 100%;
}

.task-query__dates :deep(.el-date-editor) {
  flex: 1;
  min-width: 0;
  width: 0;
}

@container task-list (max-width: 1050px) {
  .task-query {
    grid-template-columns: minmax(0, 1fr) minmax(0, 1fr) auto;
  }

  .task-query__submit {
    grid-column: 3;
    grid-row: 1;
  }

  .task-query__dates {
    grid-column: 2 / 4;
  }
}

@container task-list (max-width: 640px) {
  .task-query {
    grid-template-columns: minmax(0, 1fr) auto;
  }

  .task-query__submit {
    grid-column: 2;
  }

  .task-query :deep(.el-form-item:not(.task-query__submit):not(:first-child)) {
    grid-column: 1 / -1;
  }
}
</style>
