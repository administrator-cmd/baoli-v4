<script setup lang="ts">
import { computed, onMounted, reactive, ref, watch } from "vue";
import { useRouter } from "vue-router";
import { ElMessage, ElMessageBox } from "element-plus";

import type { PlanListItem, PlanSelection } from "@/api/contracts";
import {
  copyPlan,
  createTaskFromPlan,
  deletePlan,
  getPlan,
  listPlans,
  planExportUrl,
  restorePlan,
  setPlanEnabled,
} from "@/api/plans";
import { operationIdempotencyKey } from "@/api/http";
import { listSceneOptions, type ScenarioOption } from "@/api/scenes";
import { usePagedList } from "@/composables/usePagedList";
import PageHeader from "@/components/PageHeader.vue";
import { nodeLabel } from "@/constants/labels";

/**
 * 方案列表：独立持久化的审核方案（对固定场景版本的规则选择）。
 * 方案必须发布并启用后才能创建任务；旧任务范围可“另存为方案”。
 */
const router = useRouter();

const list = usePagedList<PlanListItem, { name: string; scenario_code: string; enabled: string; effective: string; deleted: boolean }>({
  key: "plans",
  initialFilters: { name: "", scenario_code: "", enabled: "", effective: "", deleted: false },
  fetch: (params) =>
    listPlans({
      name: params.name || undefined,
      scenario_code: params.scenario_code || undefined,
      enabled: params.enabled === "" ? undefined : params.enabled === "true",
      effective: params.effective === "" ? undefined : params.effective === "true",
      deleted: params.deleted,
      page: params.page,
      page_size: params.page_size,
    }),
});

const { filters, page, pageSize, total, items, loading, error } = list;

const sceneOptions = ref<ScenarioOption[]>([]);
/** 场景下拉读取失败时保留可见原因，不静默变成空列表。 */
const sceneOptionsError = ref<string | null>(null);
const selectedRows = ref<PlanListItem[]>([]);
const editSelectedDisabled = computed(() => selectedRows.value.length !== 1);
const deleteDisabled = computed(() => selectedRows.value.length === 0);

function exportUrl(): string {
  return planExportUrl({
    name: filters.name || undefined,
    scenario_code: filters.scenario_code || undefined,
    enabled: filters.enabled === "" ? undefined : filters.enabled === "true",
    effective: filters.effective === "" ? undefined : filters.effective === "true",
    deleted: filters.deleted,
  });
}

function goNew(): void {
  void router.push({ name: "plan-new" });
}

function goEdit(row: PlanListItem): void {
  void router.push({ name: "plan-edit", params: { planCode: row.plan_code } });
}

async function toggleEnabled(row: PlanListItem): Promise<void> {
  try {
    await setPlanEnabled(row.plan_code, !row.enabled);
    ElMessage.success(row.enabled ? "方案已停用（已创建任务不受影响）" : "方案已启用");
    await list.load();
  } catch (failure) {
    ElMessage.error(failure instanceof Error ? failure.message : "操作失败");
  }
}

async function copyRow(row: PlanListItem): Promise<void> {
  let name = `${row.plan_name} 副本`;
  try {
    const result = await ElMessageBox.prompt("复制方案会连同当前草稿的规则选择一起复制（已发布版本不复制）。", "复制方案", {
      inputValue: name,
      confirmButtonText: "复制",
      cancelButtonText: "取消",
      inputValidator: (value) => (value && value.trim() ? true : "请输入新方案名称"),
    });
    name = result.value.trim();
  } catch {
    return;
  }
  try {
    await copyPlan(row.plan_code, name);
    ElMessage.success("方案已复制");
    await list.load();
  } catch (failure) {
    ElMessage.error(failure instanceof Error ? failure.message : "复制失败");
  }
}

async function deleteRow(row: PlanListItem): Promise<void> {
  try {
    await ElMessageBox.confirm(
      `确认删除方案「${row.plan_name}」？删除为逻辑删除：已发布方案版本与使用它的任务保留，方案不再用于新建任务。`,
      "删除方案",
      { type: "warning", confirmButtonText: "删除", cancelButtonText: "取消" },
    );
  } catch {
    return;
  }
  try {
    await deletePlan(row.plan_code);
    ElMessage.success("方案已移入已删除列表");
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
    await ElMessageBox.confirm(`确认删除选中的 ${targets.length} 个方案？删除为逻辑删除，历史版本保留。`, "批量删除方案", {
      type: "warning",
      confirmButtonText: "删除",
      cancelButtonText: "取消",
    });
  } catch {
    return;
  }
  for (const row of targets) {
    try {
      await deletePlan(row.plan_code);
    } catch (failure) {
      failures.push(`${row.plan_name}：${failure instanceof Error ? failure.message : "未知原因"}`);
    }
  }
  if (failures.length) {
    ElMessage.warning(`部分删除失败：${failures.join("；")}`);
  } else {
    ElMessage.success("选中的方案已移入已删除列表");
  }
  selectedRows.value = [];
  await list.load();
}

async function restoreRow(row: PlanListItem): Promise<void> {
  try {
    await restorePlan(row.plan_code);
    ElMessage.success("方案已恢复");
    await list.load();
  } catch (failure) {
    ElMessage.error(failure instanceof Error ? failure.message : "恢复失败");
  }
}

/* ---- 详情抽屉 ---- */
const detailVisible = ref(false);
const detailLoading = ref(false);
const detailError = ref<string | null>(null);
const detail = ref<PlanSelection | null>(null);

async function openDetail(row: PlanListItem): Promise<void> {
  detailVisible.value = true;
  detailLoading.value = true;
  detailError.value = null;
  detail.value = null;
  try {
    detail.value = await getPlan(row.plan_code);
  } catch (failure) {
    detailError.value = failure instanceof Error ? failure.message : "方案详情读取失败";
  } finally {
    detailLoading.value = false;
  }
}

/* ---- 创建任务 ---- */
const taskDialogVisible = ref(false);
const taskPlan = ref<PlanListItem | null>(null);
const taskForm = reactive({ task_name: "", remark: "" });
const taskSaving = ref(false);
let taskAttemptKey: string | null = null;

function openCreateTask(row: PlanListItem): void {
  taskPlan.value = row;
  taskDialogVisible.value = true;
  // 每次打开弹窗生成新的幂等标识：失败重开时不会与上一次请求的内容冲突。
  taskAttemptKey = null;
  taskForm.task_name = "";
  taskForm.remark = "";
}

/**
 * 建任务前展示将被抓取的规则范围。
 *
 * 范围来自方案当前保存的配置；任务创建会把它抓成自己的快照，此后改方案不影响本任务。
 * 页面不在此处改选，避免让用户误以为可以在建任务时临时调整范围。
 */
const createSnapshot = ref<PlanSelection | null>(null);
const snapshotLoading = ref(false);
const snapshotError = ref<string | null>(null);

async function loadCreateSnapshot(): Promise<void> {
  createSnapshot.value = null;
  snapshotError.value = null;
  if (!taskPlan.value) {
    return;
  }
  snapshotLoading.value = true;
  try {
    createSnapshot.value = await getPlan(taskPlan.value.plan_code);
  } catch (failure) {
    snapshotError.value = failure instanceof Error ? failure.message : "方案范围读取失败";
  } finally {
    snapshotLoading.value = false;
  }
}

const snapshotRows = computed(() => {
  const selected = new Set(createSnapshot.value?.selected_rule_keys ?? []);
  return (createSnapshot.value?.selected_rules ?? []).map((rule) => ({
    rule_key: rule.rule_key,
    name: rule.name,
    selection: selected.has(rule.rule_key) ? "EXPLICIT" : "AUTO",
  }));
});
const snapshotExplicitCount = computed(() => snapshotRows.value.filter((row) => row.selection === "EXPLICIT").length);

async function submitCreateTask(): Promise<void> {
  if (!taskPlan.value) {
    ElMessage.warning("请先选择方案");
    return;
  }
  if (!taskForm.task_name.trim()) {
    ElMessage.warning("请填写任务名称");
    return;
  }
  taskSaving.value = true;
  try {
    // 幂等标识在本次弹窗尝试内复用：响应丢失后重发返回原任务，不重复创建。
    taskAttemptKey = taskAttemptKey ?? operationIdempotencyKey("TASK_CREATE", taskPlan.value.plan_code, taskForm.task_name.trim());
    const created = await createTaskFromPlan(taskPlan.value.plan_code, {
      task_name: taskForm.task_name.trim(),
      remark: taskForm.remark.trim() || null,
      idempotency_key: taskAttemptKey,
    });
    taskAttemptKey = null;
    taskDialogVisible.value = false;
    if (created.reused) {
      ElMessage.success("该请求此前已创建任务，已打开原任务");
    } else {
      ElMessage.success("审核任务已创建");
    }
    await router.push({ name: "audit-task-detail", params: { taskNo: created.task_no } });
  } catch (failure) {
    ElMessage.error(failure instanceof Error ? failure.message : "创建任务失败");
  } finally {
    taskSaving.value = false;
  }
}

onMounted(async () => {
  await loadSceneOptions();
  void list.load();
});

async function loadSceneOptions(): Promise<void> {
  sceneOptionsError.value = null;
  try {
    sceneOptions.value = (await listSceneOptions()).items;
  } catch (failure) {
    sceneOptions.value = [];
    sceneOptionsError.value = failure instanceof Error ? failure.message : "场景选项读取失败";
  }
}
</script>

<template>
  <div class="app-panel">
    <PageHeader
      eyebrow="方案管理"
      title="方案列表"
      subtitle="维护对固定场景版本的规则选择；方案发布并启用后才能创建审核任务。"
      :show-refresh="false"
    />
    <el-form inline class="app-query" @submit.prevent="list.search()">
      <el-form-item label="方案名称">
        <el-input v-model="filters.name" clearable placeholder="请输入方案名称" style="width: 200px" @keyup.enter="list.search()" />
      </el-form-item>
      <el-form-item label="所属场景">
        <el-select v-model="filters.scenario_code" clearable filterable placeholder="全部场景" style="width: 220px">
          <el-option v-for="item in sceneOptions" :key="item.scenario_code" :value="item.scenario_code" :label="item.scenario_name" />
        </el-select>
      </el-form-item>
      <el-form-item label="启用状态">
        <el-select v-model="filters.enabled" clearable placeholder="全部" style="width: 120px">
          <el-option value="true" label="启用" />
          <el-option value="false" label="停用" />
        </el-select>
      </el-form-item>
      <el-form-item label="范围状态">
        <el-select v-model="filters.effective" clearable placeholder="全部" style="width: 130px">
          <el-option value="true" label="已生效" />
          <el-option value="false" label="待核对" />
        </el-select>
      </el-form-item>
      <el-form-item>
        <el-button type="primary" @click="list.search()">查询</el-button>
        <el-button @click="list.reset()">重置</el-button>
        <el-button v-if="!filters.deleted" tag="a" :href="exportUrl()">导出</el-button>
        <el-button :loading="loading" @click="list.load()">刷新</el-button>
      </el-form-item>
    </el-form>

    <div class="app-toolbar">
      <div class="app-toolbar__left">
        <template v-if="!filters.deleted">
          <el-button type="primary" @click="goNew">新增</el-button>
          <el-button type="success" plain :disabled="editSelectedDisabled" @click="selectedRows[0] && goEdit(selectedRows[0])">编辑</el-button>
          <el-button type="danger" plain :disabled="deleteDisabled" @click="deleteSelected">删除</el-button>
        </template>
      </div>
      <div class="app-toolbar__right">
        <el-checkbox v-model="filters.deleted" @change="list.search()">已删除</el-checkbox>
      </div>
    </div>

    <el-alert v-if="sceneOptionsError" type="warning" show-icon :closable="false" :title="`场景选项读取失败：${sceneOptionsError}`">
      所属场景下拉为空，按场景筛选与新建方案会缺少选项。
      <el-button size="small" @click="loadSceneOptions">重试</el-button>
    </el-alert>

    <el-alert v-if="error" type="error" show-icon :closable="false" :title="`读取失败：${error}`">
      <el-button size="small" @click="list.load()">重试</el-button>
    </el-alert>

    <el-table v-else :data="items" border stripe v-loading="loading" empty-text="暂无数据" @selection-change="selectedRows = $event">
      <el-table-column type="selection" width="50" />
      <el-table-column type="index" label="序号" width="60" />
      <el-table-column label="方案编码" width="170">
        <template #default="{ row }"><span class="mono">{{ row.plan_code }}</span></template>
      </el-table-column>
      <el-table-column label="方案名称" min-width="200" show-overflow-tooltip>
        <template #default="{ row }">
          <el-button link type="primary" class="table-link" @click="openDetail(row)">{{ row.plan_name }}</el-button>
        </template>
      </el-table-column>
      <el-table-column prop="scenario_name" label="所属场景" min-width="180" show-overflow-tooltip />
      <el-table-column label="场景版本" width="110">
        <template #default="{ row }">
          <span v-if="row.base_scenario_version_no">第 {{ row.base_scenario_version_no }} 版</span>
          <span v-else class="text-hint">未绑定</span>
        </template>
      </el-table-column>
      <el-table-column label="范围状态" width="110">
        <template #default="{ row }">
          <el-tag :type="row.effective ? 'success' : 'warning'" size="small" effect="light" disable-transitions>
            {{ row.effective ? "已生效" : "待核对" }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column label="业务规则数" width="120">
        <template #default="{ row }">
          <span>{{ row.enabled_rule_count }}（含自动依赖）</span>
        </template>
      </el-table-column>
      <el-table-column label="启用状态" width="100">
        <template #default="{ row }">
          <el-tag :type="row.enabled ? 'success' : 'info'" size="small" effect="light" disable-transitions>
            {{ row.enabled ? "启用" : "停用" }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column label="更新时间" width="170">
        <template #default="{ row }">{{ String(row.updated_at).replace("T", " ").slice(0, 19) }}</template>
      </el-table-column>
      <el-table-column label="操作" width="280" fixed="right">
        <template #default="{ row }">
          <template v-if="!filters.deleted">
            <el-button link type="primary" size="small" @click="openDetail(row)">详情</el-button>
            <el-button link type="primary" size="small" @click="goEdit(row)">编辑</el-button>
            <el-button link type="primary" size="small" @click="toggleEnabled(row)">{{ row.enabled ? "停用" : "启用" }}</el-button>
            <el-button link type="primary" size="small" @click="openCreateTask(row)">创建任务</el-button>
            <el-dropdown trigger="click">
              <el-button link type="primary" size="small">更多</el-button>
              <template #dropdown>
                <el-dropdown-menu>
                  <el-dropdown-item @click="copyRow(row)">复制方案</el-dropdown-item>
                  <el-dropdown-item divided @click="deleteRow(row)">删除</el-dropdown-item>
                </el-dropdown-menu>
              </template>
            </el-dropdown>
          </template>
          <template v-else>
            <el-button link type="primary" size="small" @click="restoreRow(row)">恢复</el-button>
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

    <el-drawer v-model="detailVisible" title="方案详情" size="560px">
      <el-alert v-if="detailError" type="error" show-icon :closable="false" :title="detailError" />
      <div v-else v-loading="detailLoading">
        <template v-if="detail">
          <el-descriptions :column="1" border size="small">
            <el-descriptions-item label="方案名称">{{ detail.plan_name }}</el-descriptions-item>
            <el-descriptions-item label="所属场景">{{ detail.scenario_code }}</el-descriptions-item>
            <el-descriptions-item label="绑定场景版本">
              <span v-if="detail.base_scenario_version">第 {{ detail.base_scenario_version.version_no }} 版（{{ detail.base_scenario_version.scenario_name }}）</span>
              <span v-else class="text-hint">未绑定（范围不能生效）</span>
            </el-descriptions-item>
            <el-descriptions-item label="范围状态">
              <el-tag :type="detail.effective ? 'success' : 'warning'" size="small" effect="light" disable-transitions>
                {{ detail.effective ? "已生效" : "待核对" }}
              </el-tag>
              <span v-if="!detail.effective && detail.sync_error" class="text-hint">
                {{ detail.sync_error.message }}
              </span>
            </el-descriptions-item>
            <el-descriptions-item label="显式选择规则">{{ detail.selected_rule_keys.length }} 条</el-descriptions-item>
            <el-descriptions-item label="材料位 / 字段">
              {{ detail.materials.length }} 个材料位 · {{ detail.field_count }} 个字段
            </el-descriptions-item>
            <el-descriptions-item label="说明">{{ detail.description || "—" }}</el-descriptions-item>
          </el-descriptions>
          <h4 class="app-section-title block-gap-sm">显式选择的规则</h4>
          <el-table :data="detail.selected_rules" border size="small" max-height="320" empty-text="尚未选择规则">
            <el-table-column prop="name" label="规则名称" min-width="200" show-overflow-tooltip />
            <el-table-column label="状态" width="90">
              <template #default="{ row }">
                <el-tag v-if="row.missing" type="danger" size="small" effect="plain" disable-transitions>已移除</el-tag>
                <el-tag v-else type="success" size="small" effect="plain" disable-transitions>可选</el-tag>
              </template>
            </el-table-column>
          </el-table>
          <p v-if="detail.selected_rules.some((item) => item.missing)" class="text-danger block-gap-sm">
            有规则在当前绑定的场景版本中已不存在：编辑方案并保存草稿时会被如实移除。
          </p>
        </template>
      </div>
    </el-drawer>

    <el-dialog v-model="taskDialogVisible" title="创建审核任务" width="680px" :close-on-click-modal="false" @open="loadCreateSnapshot">
      <el-alert type="info" show-icon :closable="false" title="任务创建时抓取方案当前的规则范围">
        创建后任务保存自己的执行快照；之后修改方案或场景都不会改变已创建任务。
      </el-alert>
      <el-form label-width="100px" class="block-gap-sm">
        <el-form-item label="方案">
          <span>{{ taskPlan?.plan_name }}</span>
        </el-form-item>
        <el-form-item label="任务名称" required>
          <el-input v-model="taskForm.task_name" maxlength="255" placeholder="例如：供应商甲 9 月合同审核" />
        </el-form-item>
        <el-form-item label="执行范围">
          <div class="snapshot-block">
            <div v-if="snapshotLoading" class="text-muted">正在读取方案的规则范围…</div>
            <el-alert
              v-else-if="snapshotError"
              type="error"
              show-icon
              :closable="false"
              :title="`方案范围读取失败：${snapshotError}`"
            >
              <el-button size="small" @click="loadCreateSnapshot">重试</el-button>
            </el-alert>
            <template v-else-if="createSnapshot">
              <el-alert
                v-if="!createSnapshot.effective"
                type="warning"
                show-icon
                :closable="false"
                title="该方案的范围尚未生效，不能创建任务"
              >
                {{ createSnapshot.sync_error?.message ?? "配置不完整" }}；请先在方案编辑页保存成功。
              </el-alert>
              <div class="text-muted">
                显式选择 {{ snapshotExplicitCount }} 条 · 字段 {{ createSnapshot.field_count }} 个 ·
                材料位 {{ createSnapshot.materials.length }} 个
              </div>
              <el-table :data="snapshotRows" size="small" border max-height="220" empty-text="该方案没有规则">
                <el-table-column prop="name" label="规则" min-width="220" show-overflow-tooltip />
              </el-table>
              <div class="text-hint">
                以上范围在创建时写入任务快照；之后修改方案或场景都不会改变本任务。
              </div>
            </template>
          </div>
        </el-form-item>
        <el-form-item label="备注">
          <el-input v-model="taskForm.remark" type="textarea" :rows="2" maxlength="4000" placeholder="可选" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="taskDialogVisible = false">取消</el-button>
        <el-button
          type="primary"
          :loading="taskSaving"
          :disabled="!createSnapshot?.effective"
          @click="submitCreateTask"
        >
          创建任务
        </el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.snapshot-block {
  width: 100%;
}

.snapshot-block > * + * {
  margin-top: 8px;
}
</style>
