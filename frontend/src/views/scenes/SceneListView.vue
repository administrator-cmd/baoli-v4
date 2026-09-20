<script setup lang="ts">
import { onMounted, reactive, ref } from "vue";
import { useRouter } from "vue-router";
import { ElMessage, ElMessageBox } from "element-plus";

import type { ScenarioListItem } from "@/api/contracts";
import {
  copyScene,
  deleteScene,
  listScenes,
  restoreScene,
  sceneExportUrl,
  setSceneEnabled,
  updateScene,
} from "@/api/scenes";
import { usePagedList } from "@/composables/usePagedList";
import PageHeader from "@/components/PageHeader.vue";

/**
 * 场景列表：管理场景身份（名称/业务类型/启停/逻辑删除）与配置入口。
 * 列表数字来自真实草稿配置；发布版本列来自已发布版本；导出使用当前筛选作用域。
 * 场景身份由规则库导入创建（见规则库导入页），本页不提供新增：逐项手工配置不是实际路径。
 */
const router = useRouter();

const list = usePagedList<ScenarioListItem, {
  name: string;
  category: string;
  enabled: string;
  published_from: string;
  published_to: string;
  deleted: boolean;
}>({
  key: "scenes",
  initialFilters: { name: "", category: "", enabled: "", published_from: "", published_to: "", deleted: false },
  fetch: (params) =>
    listScenes({
      name: params.name || undefined,
      category: params.category || undefined,
      enabled: params.enabled === "" ? undefined : params.enabled === "true",
      published_from: params.published_from || undefined,
      published_to: params.published_to || undefined,
      deleted: params.deleted,
      page: params.page,
      page_size: params.page_size,
    }),
});

const { filters, page, pageSize, total, items, loading, error } = list;

const dateRange = ref<[string, string] | null>(null);

function search(): void {
  filters.published_from = dateRange.value?.[0] ?? "";
  filters.published_to = dateRange.value?.[1] ?? "";
  list.search();
}

function reset(): void {
  dateRange.value = null;
  list.reset();
}

function exportUrl(): string {
  return sceneExportUrl({
    name: filters.name || undefined,
    category: filters.category || undefined,
    enabled: filters.enabled === "" ? undefined : filters.enabled === "true",
    published_from: filters.published_from || undefined,
    published_to: filters.published_to || undefined,
    deleted: filters.deleted,
  });
}

/* ---- 编辑 ---- */
const dialogVisible = ref(false);
const editing = ref<ScenarioListItem | null>(null);
const saving = ref(false);
const form = reactive({ scenario_name: "", category: "", description: "", enabled: true });

function openEdit(row: ScenarioListItem): void {
  editing.value = row;
  form.scenario_name = row.scenario_name;
  form.category = row.category ?? "";
  form.description = row.description ?? "";
  form.enabled = row.enabled;
  dialogVisible.value = true;
}

async function submitForm(): Promise<void> {
  if (!form.scenario_name.trim()) {
    ElMessage.warning("请填写场景名称");
    return;
  }
  if (!editing.value) {
    return;
  }
  saving.value = true;
  try {
    await updateScene(editing.value.scenario_code, {
      scenario_name: form.scenario_name.trim(),
      category: form.category.trim() || null,
      description: form.description.trim() || null,
      enabled: form.enabled,
    });
    ElMessage.success("场景资料已保存");
    dialogVisible.value = false;
    await list.load();
  } catch (failure) {
    ElMessage.error(failure instanceof Error ? failure.message : "保存失败");
  } finally {
    saving.value = false;
  }
}

async function toggleEnabled(row: ScenarioListItem): Promise<void> {
  try {
    await setSceneEnabled(row.scenario_code, !row.enabled);
    ElMessage.success(row.enabled ? "场景已停用（不影响已创建任务）" : "场景已启用");
    await list.load();
  } catch (failure) {
    ElMessage.error(failure instanceof Error ? failure.message : "操作失败");
  }
}

async function copyRow(row: ScenarioListItem): Promise<void> {
  let name = `${row.scenario_name} 副本`;
  try {
    const result = await ElMessageBox.prompt("复制场景会连同当前草稿配置一起复制（已发布版本不复制）。", "复制场景", {
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
    await copyScene(row.scenario_code, name);
    ElMessage.success("场景已复制");
    await list.load();
  } catch (failure) {
    ElMessage.error(failure instanceof Error ? failure.message : "复制失败");
  }
}

async function deleteRow(row: ScenarioListItem): Promise<void> {
  try {
    await ElMessageBox.confirm(
      `确认删除场景「${row.scenario_name}」？删除为逻辑删除：历史版本、任务、报告与证据全部保留；场景不再用于新建方案与任务。`,
      "删除场景",
      { type: "warning", confirmButtonText: "删除", cancelButtonText: "取消" },
    );
  } catch {
    return;
  }
  try {
    await deleteScene(row.scenario_code);
    ElMessage.success("场景已移入已删除列表");
    await list.load();
  } catch (failure) {
    ElMessage.error(failure instanceof Error ? failure.message : "删除失败");
  }
}

async function restoreRow(row: ScenarioListItem): Promise<void> {
  try {
    await restoreScene(row.scenario_code);
    ElMessage.success("场景已恢复");
    await list.load();
  } catch (failure) {
    ElMessage.error(failure instanceof Error ? failure.message : "恢复失败");
  }
}

function goDetail(row: ScenarioListItem): void {
  void router.push({ name: "scene-detail", params: { scenarioCode: row.scenario_code } });
}

function goConfig(row: ScenarioListItem, scope = "draft"): void {
  void router.push({ name: "scene-materials", query: { scenario: row.scenario_code, scope } });
}

function goCreatePlan(row: ScenarioListItem): void {
  void router.push({ name: "plan-new", query: { scenario: row.scenario_code } });
}

function goImport(row: ScenarioListItem): void {
  void router.push({ name: "scene-imports", query: { scenario: row.scenario_code } });
}

onMounted(() => {
  void list.load();
});
</script>

<template>
  <div class="app-panel">
    <PageHeader
      eyebrow="场景管理"
      title="场景列表"
      subtitle="管理审核场景身份与配置入口；配置规模来自真实草稿，发布版本来自已发布记录。场景由规则库导入创建。"
      :show-refresh="false"
    />
    <el-form inline class="app-query" @submit.prevent="search">
      <el-form-item label="场景名称">
        <el-input v-model="filters.name" clearable placeholder="请输入场景名称" style="width: 200px" @keyup.enter="search" />
      </el-form-item>
      <el-form-item label="业务类型">
        <el-input v-model="filters.category" clearable placeholder="请输入业务类型" style="width: 160px" @keyup.enter="search" />
      </el-form-item>
      <el-form-item label="启用状态">
        <el-select v-model="filters.enabled" clearable placeholder="全部" style="width: 120px">
          <el-option value="true" label="启用" />
          <el-option value="false" label="停用" />
        </el-select>
      </el-form-item>
      <el-form-item label="发布日期">
        <el-date-picker
          v-model="dateRange"
          type="daterange"
          value-format="YYYY-MM-DD"
          start-placeholder="开始日期"
          end-placeholder="结束日期"
          style="width: 260px"
        />
      </el-form-item>
      <el-form-item>
        <el-button type="primary" @click="search">查询</el-button>
        <el-button @click="reset">重置</el-button>
        <el-button v-if="!filters.deleted" tag="a" :href="exportUrl()">导出</el-button>
        <el-button :loading="loading" @click="list.load()">刷新</el-button>
      </el-form-item>
    </el-form>

    <div class="app-toolbar">
      <div class="app-toolbar__left">
        <span class="text-hint">场景由「规则库导入」按要求的目标模板新建，不在本页手工创建。</span>
      </div>
      <div class="app-toolbar__right">
        <el-checkbox v-model="filters.deleted" @change="search">已删除</el-checkbox>
      </div>
    </div>

    <el-alert v-if="error" type="error" show-icon :closable="false" :title="`读取失败：${error}`">
      <el-button size="small" @click="list.load()">重试</el-button>
    </el-alert>

    <el-table
      v-else
      :data="items"
      border
      stripe
      v-loading="loading"
      empty-text="暂无数据"
    >
      <el-table-column type="index" label="序号" width="60" />
      <el-table-column label="场景编码" width="170">
        <template #default="{ row }">
          <span class="mono">{{ row.scenario_code }}</span>
        </template>
      </el-table-column>
      <el-table-column label="场景名称" min-width="200" show-overflow-tooltip>
        <template #default="{ row }">
          <el-button link type="primary" class="table-link" @click="goDetail(row)">{{ row.scenario_name }}</el-button>
        </template>
      </el-table-column>
      <el-table-column label="业务类型" width="130" show-overflow-tooltip>
        <template #default="{ row }">{{ row.category || "—" }}</template>
      </el-table-column>
      <el-table-column label="配置状态" width="150">
        <template #default="{ row }">
          <el-tag v-if="row.sync_error" type="warning" size="small" effect="light" disable-transitions>
            尚未生效
          </el-tag>
          <el-tag v-else type="success" size="small" effect="light" disable-transitions>已生效</el-tag>
          <div v-if="row.sync_error" class="text-hint">{{ row.sync_error.message }}</div>
        </template>
      </el-table-column>
      <el-table-column prop="material_count" label="材料数" width="90" />
      <el-table-column prop="field_count" label="字段数" width="90" />
      <el-table-column prop="rule_count" label="业务规则数" width="110" />
      <el-table-column label="场景状态" width="100">
        <template #default="{ row }">
          <el-tag :type="row.enabled ? 'success' : 'info'" size="small" effect="light" disable-transitions>
            {{ row.enabled ? "启用" : "停用" }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column label="更新时间" width="170">
        <template #default="{ row }">{{ String(row.updated_at).replace("T", " ").slice(0, 19) }}</template>
      </el-table-column>
      <el-table-column label="操作" width="260" fixed="right">
        <template #default="{ row }">
          <template v-if="!filters.deleted">
            <el-button link type="primary" size="small" @click="goDetail(row)">详情</el-button>
            <el-button link type="primary" size="small" @click="openEdit(row)">编辑</el-button>
            <el-button link type="primary" size="small" @click="goConfig(row)">配置</el-button>
            <el-button link type="primary" size="small" @click="toggleEnabled(row)">{{ row.enabled ? "停用" : "启用" }}</el-button>
            <el-dropdown trigger="click">
              <el-button link type="primary" size="small">更多</el-button>
              <template #dropdown>
                <el-dropdown-menu>
                  <el-dropdown-item @click="copyRow(row)">复制场景</el-dropdown-item>
                  <el-dropdown-item @click="goCreatePlan(row)">创建方案</el-dropdown-item>
                  <el-dropdown-item @click="goImport(row)">导入规则</el-dropdown-item>
                  <el-dropdown-item divided @click="deleteRow(row)">删除</el-dropdown-item>
                </el-dropdown-menu>
              </template>
            </el-dropdown>
          </template>
          <template v-else>
            <el-button link type="primary" size="small" @click="restoreRow(row)">恢复</el-button>
            <el-button link type="primary" size="small" @click="goDetail(row)">详情</el-button>
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

    <el-dialog v-model="dialogVisible" title="编辑场景" width="600px" :close-on-click-modal="false">
      <el-form label-width="100px">
        <el-form-item label="场景名称" required>
          <el-input v-model="form.scenario_name" maxlength="255" placeholder="例如：采购合同与发票核验" />
        </el-form-item>
        <el-form-item label="业务类型">
          <el-input v-model="form.category" maxlength="80" placeholder="例如：保理业务 / 采购交易" />
        </el-form-item>
        <el-form-item label="场景说明">
          <el-input v-model="form.description" type="textarea" :rows="3" maxlength="4000" placeholder="说明这个场景覆盖什么业务" />
        </el-form-item>
        <el-form-item label="启用状态">
          <el-switch v-model="form.enabled" active-text="启用" inactive-text="停用" />
        </el-form-item>
        <el-form-item label="场景编码">
          <span class="text-hint mono">{{ editing?.scenario_code }}</span>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="submitForm">确定</el-button>
      </template>
    </el-dialog>
  </div>
</template>
