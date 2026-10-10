<script setup lang="ts">
import { onMounted, reactive, ref } from "vue";
import { useRouter } from "vue-router";
import { ElMessage, ElMessageBox } from "element-plus";

import type { ScenarioListItem } from "@/api/contracts";
import {
  createScene,
  deleteScene,
  listScenes,
  setSceneEnabled,
  updateScene,
} from "@/api/scenes";
import { usePagedList } from "@/composables/usePagedList";
import PageHeader from "@/components/PageHeader.vue";

/**
 * 场景列表：管理场景身份（名称/业务类型/启停/实际删除）与配置入口。
 * 列表数字来自真实草稿配置；发布版本列来自已发布版本。
 * 管理员在这里创建场景，再到规则库导入和场景配置完成设置。
 */
const creating = ref(false);
const deletingCode = ref<string | null>(null);
const router = useRouter();

const list = usePagedList<ScenarioListItem, {
  name: string;
  category: string;
  enabled: string;
  published_from: string;
  published_to: string;
}>({
  key: "scenes",
  initialFilters: { name: "", category: "", enabled: "", published_from: "", published_to: "" },
  fetch: (params) =>
    listScenes({
      name: params.name || undefined,
      category: params.category || undefined,
      enabled: params.enabled === "" ? undefined : params.enabled === "true",
      published_from: params.published_from || undefined,
      published_to: params.published_to || undefined,
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

/* 编辑 */
const dialogVisible = ref(false);
const editing = ref<ScenarioListItem | null>(null);
const saving = ref(false);
const form = reactive({ scenario_name: "", category: "", description: "", enabled: true });

function openCreate(): void {
  creating.value = true;
  editing.value = null;
  form.scenario_name = "";
  form.category = "";
  form.description = "";
  form.enabled = true;
  dialogVisible.value = true;
}

function openEdit(row: ScenarioListItem): void {
  creating.value = false;
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
  saving.value = true;
  try {
    if (creating.value) {
      const created = await createScene({
        scenario_name: form.scenario_name.trim(),
        category: form.category.trim() || null,
        description: form.description.trim() || null,
        enabled: form.enabled,
      });
      ElMessage.success("场景已创建");
      dialogVisible.value = false;
      await router.push({ name: "scene-imports", query: { scenario: created.scenario_code, create: "1" } });
      return;
    }
    if (!editing.value) {
      return;
    }
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

async function deleteRow(row: ScenarioListItem): Promise<void> {
  try {
    await ElMessageBox.confirm(
      `确认永久删除场景「${row.scenario_name}」及其草稿？删除后无法恢复，已有方案和任务不受影响。`,
      "删除场景",
      { type: "warning", confirmButtonText: "删除", cancelButtonText: "取消" },
    );
  } catch {
    return;
  }
  deletingCode.value = row.scenario_code;
  try {
    await deleteScene(row.scenario_code);
    ElMessage.success("场景已删除");
    if (items.value.length === 1 && page.value > 1) {
      page.value -= 1;
    }
    await list.load();
  } catch (failure) {
    ElMessage.error(failure instanceof Error ? failure.message : "删除失败");
  } finally {
    deletingCode.value = null;
  }
}

function goDetail(row: ScenarioListItem): void {
  void router.push({ name: "scene-detail", params: { scenarioCode: row.scenario_code } });
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
      subtitle="管理场景基本资料与启用状态。"
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
      </el-form-item>
    </el-form>

    <div class="app-toolbar">
      <div class="app-toolbar__left">
        <el-button type="primary" @click="openCreate">新建场景</el-button>
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
      :header-cell-style="{ textAlign: 'center' }"
      :cell-style="{ textAlign: 'center' }"
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
          <el-tag v-if="row.sync_error" type="warning" size="small" effect="light" disable-transitions>配置待完善</el-tag>
          <el-tag v-else-if="row.published_version_no" type="success" size="small" effect="light" disable-transitions>已生效</el-tag>
          <el-tag v-else type="info" size="small" effect="light" disable-transitions>待发布</el-tag>
          <div v-if="row.sync_error" class="text-hint">{{ row.sync_error.message }}</div>
        </template>
      </el-table-column>
      <el-table-column prop="material_count" label="材料数" width="90" />
      <el-table-column prop="field_count" label="字段数" width="90" />
      <el-table-column prop="rule_count" label="业务规则数" width="110" />
      <el-table-column label="已发布版本" width="120">
        <template #default="{ row }">{{ row.published_version_no ?? "未发布" }}</template>
      </el-table-column>
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
      <el-table-column label="操作" width="200" align="center" fixed="right">
        <template #default="{ row }">
          <el-button link type="primary" size="small" @click="goDetail(row)">详情</el-button>
          <el-button link type="primary" size="small" @click="openEdit(row)">编辑</el-button>
          <el-button link type="primary" size="small" @click="toggleEnabled(row)">{{ row.enabled ? "停用" : "启用" }}</el-button>
          <el-button
            link
            type="danger"
            size="small"
            :loading="deletingCode === row.scenario_code"
            @click="deleteRow(row)"
          >删除</el-button>
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

    <el-dialog v-model="dialogVisible" :title="creating ? '新建场景' : '编辑场景'" width="600px" :close-on-click-modal="false">
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
        <el-form-item v-if="!creating" label="场景编码">
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
