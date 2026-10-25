<script setup lang="ts">
import { computed, onMounted, ref } from "vue";
import { useRouter } from "vue-router";
import { ElMessage, ElMessageBox } from "element-plus";

import type { PlanListItem, PlanSelection } from "@/api/contracts";
import {
  copyPlan,
  deletePlan,
  getPlan,
  listPlans,
  restorePlan,
  setPlanEnabled,
} from "@/api/plans";
import { listSceneOptions, type ScenarioOption } from "@/api/scenes";
import { formatDate } from "@/constants/labels";
import { usePagedList } from "@/composables/usePagedList";
import PageHeader from "@/components/PageHeader.vue";

/**
 * 方案列表：基于已发布场景版本生成、随后独立编辑的审核方案。
 * 方案基于已发布且启用的场景版本。启用且范围已生效的方案才能创建审核任务。
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
const deleteSelectedDisabled = computed(() => selectedRows.value.length === 0);
const singleSelectedRow = computed(() => selectedRows.value.length === 1 ? selectedRows.value[0] : null);
const deleteSaving = ref(false);

function goNew(): void {
  void router.push({ name: "plan-new" });
}

function goEdit(row: PlanListItem): void {
  void router.push({ name: "plan-edit", params: { planCode: row.plan_code } });
}

function goRegenerate(row: PlanListItem): void {
  void router.push({ name: "plan-new", query: { previous: row.plan_code } });
}

function copySelected(): void {
  if (singleSelectedRow.value) {
    void copyRow(singleSelectedRow.value);
  }
}

function regenerateSelected(): void {
  if (singleSelectedRow.value) {
    goRegenerate(singleSelectedRow.value);
  }
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
    const result = await ElMessageBox.prompt("复制当前方案配置，仍沿用原场景版本。", "复制方案", {
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

async function removePlans(targets: PlanListItem[]): Promise<void> {
  if (!targets.length) return;
  try {
    await ElMessageBox.confirm(
      targets.length === 1
        ? `确认删除方案「${targets[0].plan_name}」？删除后可在“已删除”列表恢复，已创建的审核任务不受影响。`
        : `确认删除选中的 ${targets.length} 个方案？删除后可在“已删除”列表恢复，已创建的审核任务不受影响。`,
      "删除方案",
      { type: "warning", confirmButtonText: "删除", cancelButtonText: "取消" },
    );
  } catch {
    return;
  }
  deleteSaving.value = true;
  try {
    const failures: string[] = [];
    let deletedCount = 0;
    for (const row of targets) {
      try {
        await deletePlan(row.plan_code);
        deletedCount += 1;
      } catch (failure) {
        failures.push(`${row.plan_name}：${failure instanceof Error ? failure.message : "删除失败"}`);
      }
    }
    if (deletedCount) ElMessage.success(`已删除 ${deletedCount} 个方案`);
    if (failures.length) ElMessage.error(failures.join("；"));
    selectedRows.value = [];
    await list.load();
  } finally {
    deleteSaving.value = false;
  }
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

/* 详情抽屉 */
const detailVisible = ref(false);
const detailLoading = ref(false);
const detailError = ref<string | null>(null);
const detail = ref<PlanSelection | null>(null);
let detailRequest = 0;

async function openDetail(row: PlanListItem): Promise<void> {
  const request = ++detailRequest;
  detailVisible.value = true;
  detailLoading.value = true;
  detailError.value = null;
  detail.value = null;
  try {
    const result = await getPlan(row.plan_code);
    if (request === detailRequest) detail.value = result;
  } catch (failure) {
    if (request === detailRequest) detailError.value = failure instanceof Error ? failure.message : "方案详情读取失败";
  } finally {
    if (request === detailRequest) detailLoading.value = false;
  }
}

function openCreateTask(row: PlanListItem): void {
  void router.push({ name: "audit-tasks", query: { createPlan: row.plan_code } });
}

/** 方案详情里启用的规则。方案自己持有规则，`enabled` 就是「这条方案跑不跑它」。 */
function planEnabledRules(detail: PlanSelection | null): Array<{ rule_key: string; name: string }> {
  return [
    ...(detail?.rules ?? []).filter((item) => item.enabled).map((item) => ({ rule_key: item.rule_key, name: item.name })),
    ...(detail?.calculation_rules ?? []).filter((item) => item.enabled).map((item) => ({ rule_key: item.rule_code, name: item.name })),
  ];
}
const detailEnabledRules = computed(() => planEnabledRules(detail.value));

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
        <el-button :loading="loading" @click="list.load()">刷新</el-button>
      </el-form-item>
    </el-form>

    <div class="app-toolbar">
      <div class="app-toolbar__left">
        <template v-if="!filters.deleted">
          <el-button type="primary" @click="goNew">新增</el-button>
          <el-button type="success" plain :disabled="editSelectedDisabled" @click="selectedRows[0] && goEdit(selectedRows[0])">编辑</el-button>
          <el-button plain :disabled="editSelectedDisabled" @click="copySelected">复制</el-button>
          <el-button plain :disabled="editSelectedDisabled" @click="regenerateSelected">重新生成</el-button>
          <el-button type="danger" plain :disabled="deleteSelectedDisabled" :loading="deleteSaving" @click="removePlans([...selectedRows])">删除</el-button>
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

    <el-table v-else :data="items" border stripe class="management-table" v-loading="loading" empty-text="暂无数据" @selection-change="selectedRows = $event">
      <el-table-column type="selection" width="50" />
      <el-table-column type="index" label="序号" width="56" />
      <el-table-column label="方案名称" min-width="180" show-overflow-tooltip>
        <template #default="{ row }">
          <el-button link type="primary" class="table-link" @click="openDetail(row)">{{ row.plan_name }}</el-button>
        </template>
      </el-table-column>
      <el-table-column prop="scenario_name" label="所属场景" min-width="140" show-overflow-tooltip />
      <el-table-column label="场景版本" width="88">
        <template #default="{ row }">
          <span v-if="row.base_scenario_version_no">第 {{ row.base_scenario_version_no }} 版</span>
          <span v-else class="text-hint">未绑定</span>
        </template>
      </el-table-column>
      <el-table-column label="范围状态" width="96">
        <template #default="{ row }">
          <el-tag :type="row.effective ? 'success' : 'warning'" size="small" effect="light" disable-transitions>
            {{ row.effective ? "已生效" : "待核对" }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column label="启用规则数" width="100">
        <template #default="{ row }">
          <span>{{ row.enabled_rule_count }}</span>
        </template>
      </el-table-column>
      <el-table-column label="启用状态" width="88">
        <template #default="{ row }">
          <el-tag :type="row.enabled ? 'success' : 'info'" size="small" effect="light" disable-transitions>
            {{ row.enabled ? "启用" : "停用" }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column label="更新时间" width="184">
        <template #default="{ row }"><span class="table-nowrap">{{ formatDate(String(row.updated_at)) }}</span></template>
      </el-table-column>
      <el-table-column label="操作" width="232" class-name="table-actions" fixed="right">
        <template #default="{ row }">
          <template v-if="!filters.deleted">
            <el-button link type="primary" size="small" @click="openDetail(row)">详情</el-button>
            <el-button link type="primary" size="small" @click="goEdit(row)">编辑</el-button>
            <el-button link type="primary" size="small" @click="toggleEnabled(row)">{{ row.enabled ? "停用" : "启用" }}</el-button>
            <el-button link type="primary" size="small" @click="openCreateTask(row)">创建任务</el-button>
            <el-button link type="danger" size="small" :disabled="deleteSaving" @click="removePlans([row])">删除</el-button>
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
            <el-descriptions-item label="所属场景">{{ detail.base_scenario_version?.scenario_name || "未记录场景名称" }}</el-descriptions-item>
            <el-descriptions-item label="绑定场景版本">
              <span v-if="detail.base_scenario_version">第 {{ detail.base_scenario_version.version_no }} 版</span>
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
            <el-descriptions-item label="启用规则">{{ detailEnabledRules.length }} 条</el-descriptions-item>
            <el-descriptions-item label="材料位 / 字段">
              {{ detail.materials.length }} 个材料位 · {{ detail.field_count }} 个字段
            </el-descriptions-item>
            <el-descriptions-item label="说明">{{ detail.description || "—" }}</el-descriptions-item>
          </el-descriptions>
          <h4 class="app-section-title block-gap-sm">启用的规则</h4>
          <el-table :data="detailEnabledRules" border size="small" max-height="320" empty-text="本方案还没有启用任何规则">
            <el-table-column prop="name" label="规则名称" min-width="200" show-overflow-tooltip />
            <el-table-column label="规则键" width="200" show-overflow-tooltip>
              <template #default="{ row }"><span class="mono">{{ row.rule_key }}</span></template>
            </el-table-column>
          </el-table>
        </template>
      </div>
    </el-drawer>

  </div>
</template>
