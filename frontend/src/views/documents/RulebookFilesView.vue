<script setup lang="ts">
import { onMounted, ref } from "vue";
import { useRouter } from "vue-router";
import { ElMessage, ElMessageBox } from "element-plus";

import {
  deleteImportJob,
  importJobFileUrl,
  listImportJobsPage,
  listTemplates,
  type ImportJobRow,
  type TemplateOption,
} from "@/api/imports";
import { usePagedList } from "@/composables/usePagedList";
import PageHeader from "@/components/PageHeader.vue";
import StatusBadge from "@/components/StatusBadge.vue";
import { formatDate, formatFileSize, statusLabel } from "@/constants/labels";

/**
 * 规则库管理：已上传的规则库 Excel 文件资产。
 *
 * 与「场景管理 → 规则库导入」的分工：那边的入口负责上传与解析转换流程；
 * 这里按文件管理已上传的原件——查大小、看发布出了哪个场景版本、下载原件、删除。
 * 两者的数据来源都是导入作业，因此不另建一套上传记录。
 */
const router = useRouter();

const list = usePagedList<ImportJobRow, { filename: string; scenario_code: string; status: string }>({
  key: "rulebook-files",
  initialFilters: { filename: "", scenario_code: "", status: "" },
  fetch: (params) =>
    listImportJobsPage({
      filename: params.filename || undefined,
      scenario_code: params.scenario_code || undefined,
      status: params.status || undefined,
      page: params.page,
      page_size: params.page_size,
    }),
});

const { filters, page, pageSize, total, items, loading, error } = list;

const statusOptions = ["PARSED", "PROPOSED", "CONFIRMED", "APPLIED"];

/* 场景下拉：按场景筛选已上传的规则库。 */
const scenarios = ref<TemplateOption[]>([]);

async function loadScenarios(): Promise<void> {
  try {
    scenarios.value = await listTemplates();
  } catch (failure) {
    // 场景下拉读不到只影响筛选，不清空已经在显示的文件列表。
    ElMessage.warning(failure instanceof Error ? `场景列表读取失败：${failure.message}` : "场景列表读取失败");
  }
}

function scenarioText(row: ImportJobRow): string {
  if (row.scenario_name) return row.scenario_name;
  return row.scenario_code ? row.scenario_code : "未记录场景";
}

function downloadUrl(row: ImportJobRow): string {
  return importJobFileUrl(row.job_no);
}

function openImport(row: ImportJobRow): void {
  void router.push({ name: "scene-import-detail", params: { jobNo: row.job_no } });
}

/* 删除：服务端对已发布的作业会明确拒绝，页面照实显示原因。 */
const deleting = ref<string[]>([]);
const deleteError = ref<string | null>(null);

async function remove(row: ImportJobRow): Promise<void> {
  try {
    await ElMessageBox.confirm(
      `删除「${row.source_filename}」这条规则库导入记录后，原件文件、解析结果与后台命令一并移除；已发布的场景版本保留，只解除来源引用。确认删除？`,
      "删除规则库",
      { type: "warning", confirmButtonText: "删除", cancelButtonText: "取消" },
    );
  } catch {
    return;
  }
  deleting.value = [...deleting.value, row.job_no];
  deleteError.value = null;
  try {
    await deleteImportJob(row.job_no);
    ElMessage.success("已删除该规则库导入记录");
    await list.load();
  } catch (failure) {
    deleteError.value = failure instanceof Error ? failure.message : "删除失败";
  } finally {
    deleting.value = deleting.value.filter((item) => item !== row.job_no);
  }
}

onMounted(() => {
  void loadScenarios();
  void list.load();
});
</script>

<template>
  <div class="app-panel">
    <PageHeader
      eyebrow="资料管理"
      title="规则库管理"
      :notes="['下载取的是上传时的原始 Excel 原件']"
      :show-refresh="false"
    />
    <el-form inline class="app-query" @submit.prevent="list.search()">
      <el-form-item label="文件名">
        <el-input v-model="filters.filename" clearable placeholder="规则库文件名" style="width: 200px" @keyup.enter="list.search()" />
      </el-form-item>
      <el-form-item label="所属场景">
        <el-select v-model="filters.scenario_code" clearable filterable placeholder="全部" style="width: 200px">
          <el-option v-for="item in scenarios" :key="item.scenario_code" :value="item.scenario_code" :label="item.scenario_name" />
        </el-select>
      </el-form-item>
      <el-form-item label="状态">
        <el-select v-model="filters.status" clearable placeholder="全部" style="width: 160px">
          <el-option v-for="item in statusOptions" :key="item" :value="item" :label="statusLabel(item)" />
        </el-select>
      </el-form-item>
      <el-form-item>
        <el-button type="primary" @click="list.search()">查询</el-button>
        <el-button @click="list.reset()">重置</el-button>
        <el-button :loading="loading" @click="list.load()">刷新</el-button>
      </el-form-item>
    </el-form>

    <el-alert v-if="deleteError" class="block-gap-sm" type="error" show-icon :closable="false"
      :title="deleteError" @close="deleteError = null" />
    <el-alert v-if="error" type="error" show-icon :closable="false" :title="`读取失败：${error}`">
      <el-button size="small" @click="list.load()">重试</el-button>
    </el-alert>

    <el-table v-else :data="items" border stripe size="small" class="rulebook-table"
      style="width: 1180px; max-width: 100%"
      v-loading="loading" empty-text="暂无数据">
      <el-table-column label="规则库文件" width="380" show-overflow-tooltip class-name="cell-align-left">
        <template #default="{ row }">
          <span class="mono">{{ row.source_filename }}</span>
          <span class="text-hint rulebook-table__size">{{ formatFileSize(row.source_size) }}</span>
        </template>
      </el-table-column>
      <el-table-column label="所属场景" width="180" show-overflow-tooltip class-name="cell-align-left">
        <template #default="{ row }">{{ scenarioText(row) }}</template>
      </el-table-column>
      <el-table-column label="上传时间" width="190">
        <template #default="{ row }">
          <span class="table-nowrap">{{ formatDate(row.created_at) }}</span>
        </template>
      </el-table-column>
      <el-table-column label="处理状态" width="130">
        <template #default="{ row }">
          <StatusBadge :status="row.status" />
          <div v-if="row.failure_message" class="text-hint">{{ row.failure_message }}</div>
        </template>
      </el-table-column>
      <el-table-column label="发布版本" width="110">
        <template #default="{ row }">
          <span v-if="row.published_version_no">第 {{ row.published_version_no }} 版</span>
          <span v-else class="text-hint">未发布</span>
        </template>
      </el-table-column>
      <el-table-column label="操作" width="190" fixed="right">
        <template #default="{ row }">
          <el-button link type="primary" size="small" tag="a" :href="downloadUrl(row)" :download="row.source_filename">
            下载原件
          </el-button>
          <el-button link type="primary" size="small" @click="openImport(row)">查看导入</el-button>
          <el-button link type="danger" size="small"
            :loading="deleting.includes(row.job_no)" @click="remove(row)">删除</el-button>
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
  </div>
</template>

<style scoped>
/* 文件资产列表：单行紧凑展示，文件名与大小并排，时间不换行。 */
.rulebook-table__size {
  margin-left: 8px;
  font-size: 12px;
}
</style>
