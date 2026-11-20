<script setup lang="ts">
import { computed, onMounted, ref, watch } from "vue";
import { useRoute, useRouter } from "vue-router";
import { ElMessage, ElMessageBox } from "element-plus";

import {
  deleteImportJob,
  importJobFileUrl,
  listImportJobsPage,
  listTemplates,
  type ImportJobRow,
  type TemplateOption,
} from "@/api/imports";
import type { GenerationJobRow, GenerationStatus } from "@/api/contracts";
import { ApiError, isOperationActive } from "@/api/http";
import {
  deleteGenerationJob,
  listGenerationJobsPage,
} from "@/api/rulebookGenerations";
import { usePagedList } from "@/composables/usePagedList";
import PageHeader from "@/components/PageHeader.vue";
import StatusBadge from "@/components/StatusBadge.vue";
import { formatDate, formatFileSize } from "@/constants/labels";
import { importJobStatusOptions } from "@/constants/imports";
import { PAGE_SIZE_OPTIONS } from "@/constants/pagination";
import { TABLE_COLUMN_WIDTHS } from "@/constants/tableColumns";
import {
  generationStatusLabel,
  generationStatusOptions,
  generationStatusTone,
} from "@/utils/generationDocument";

/**
 * 规则库管理：已导入文件与 AI 生成记录是两个真实来源。
 *
 * 生成记录不是导入作业，不能伪装成导入；两类记录各自分页、筛选和状态。
 */
const router = useRouter();
const route = useRoute();
const activeTab = ref<"files" | "generations">(route.query.tab === "generations" ? "generations" : "files");

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

const generationList = usePagedList<GenerationJobRow, { filename: string; status: GenerationStatus | "" }>({
  key: "rulebook-generations",
  initialFilters: { filename: "", status: "" },
  fetch: (params) =>
    listGenerationJobsPage({
      filename: params.filename || undefined,
      status: params.status || undefined,
      page: params.page,
      page_size: params.page_size,
    }),
});

const generationStatusWidth = computed(() => Math.max(110,
  ...generationList.items.value.map(row => generationStatusLabel(row.status).length * 14 + 40),
));

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

function openGeneration(row: GenerationJobRow): void {
  void router.push({ name: "rulebook-generation-detail", params: { jobNo: row.job_no } });
}

function openGenerationCreate(): void {
  void router.push({ name: "rulebook-generate" });
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

const deletingGeneration = ref<string | null>(null);
const generationDeleteError = ref<string | null>(null);

async function removeGeneration(row: GenerationJobRow): Promise<void> {
  if (deletingGeneration.value || isOperationActive(row.latest_operation?.status)) return;
  deletingGeneration.value = row.job_no;
  try {
    try {
      await ElMessageBox.confirm(
        `将永久删除「${row.source_filename}」（创建于 ${formatDate(row.created_at)}）的编制记录、上传原件和导出文件。已创建的场景导入不受影响。`,
        "删除编制记录",
        { type: "warning", confirmButtonText: "删除", cancelButtonText: "取消" },
      );
    } catch {
      return;
    }
    generationDeleteError.value = null;
    await deleteGenerationJob(row.job_no);
    ElMessage.success("已删除编制记录");
    await generationList.load();
  } catch (failure) {
    generationDeleteError.value = failure instanceof Error ? failure.message : "删除失败";
    if (failure instanceof ApiError && (failure.statusCode === 404 || failure.statusCode === 409 || failure.details.deleted === true)) {
      await generationList.load();
    }
  } finally {
    deletingGeneration.value = null;
  }
}

watch(activeTab, (tab) => {
  if (tab === "generations") {
    void generationList.load();
  }
}, { immediate: true });

onMounted(() => {
  void loadScenarios();
  void list.load();
});
</script>

<template>
  <div class="app-panel">
    <PageHeader title="规则库管理" :notes="['管理业务规则库，或借助 AI 整理编制 Excel']" :show-refresh="false">
      <template #actions>
        <el-button type="primary" @click="openGenerationCreate">AI辅助生成规则库</el-button>
      </template>
    </PageHeader>

    <el-tabs v-model="activeTab">
      <el-tab-pane label="已导入文件" name="files">
        <p class="text-hint">下载取的是上传时的原始 Excel 原件</p>
        <el-form inline class="app-query" @submit.prevent="list.search()">
          <el-form-item label="文件名">
            <el-input v-model="filters.filename" clearable placeholder="规则库文件名" style="width: 200px" @keyup.enter="list.search()" />
          </el-form-item>
          <el-form-item label="所属场景">
            <el-select v-model="filters.scenario_code" clearable filterable placeholder="全部" style="width: 200px">
              <el-option v-for="item in scenarios" :key="item.scenario_code" :value="item.scenario_code" :label="item.scenario_name" />
            </el-select>
          </el-form-item>
          <el-form-item label="导入状态">
            <el-select v-model="filters.status" clearable placeholder="全部" style="width: 190px">
              <el-option v-for="item in importJobStatusOptions" :key="item.value" :value="item.value" :label="item.label" />
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

        <el-table v-else :data="items" border stripe class="management-table"
          v-loading="loading" empty-text="暂无数据">
          <el-table-column label="规则库文件" width="380" show-overflow-tooltip>
            <template #default="{ row }">
              <span class="mono">{{ row.source_filename }}</span>
              <span class="text-hint rulebook-table__size">{{ formatFileSize(row.source_size) }}</span>
            </template>
          </el-table-column>
          <el-table-column label="所属场景" :width="TABLE_COLUMN_WIDTHS.scenarioName" show-overflow-tooltip>
            <template #default="{ row }">{{ scenarioText(row) }}</template>
          </el-table-column>
          <el-table-column label="上传时间" width="200">
            <template #default="{ row }">
              <span class="table-nowrap">{{ formatDate(row.created_at) }}</span>
            </template>
          </el-table-column>
          <el-table-column label="导入状态" width="190">
            <template #default="{ row }">
              <StatusBadge :status="row.status" />
              <div v-if="row.failure_message" class="status-note" :title="row.failure_message">{{ row.failure_message }}</div>
            </template>
          </el-table-column>
          <el-table-column label="发布版本" width="120">
            <template #default="{ row }">
              <span v-if="row.published_version_no">第 {{ row.published_version_no }} 版</span>
              <span v-else class="text-hint">未发布</span>
            </template>
          </el-table-column>
          <el-table-column label="操作" width="232" fixed="right">
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
            :page-sizes="PAGE_SIZE_OPTIONS"
            layout="total, sizes, prev, pager, next, jumper"
            background
            @current-change="list.setPage"
            @size-change="list.setPageSize"
          />
        </div>
      </el-tab-pane>

      <el-tab-pane label="AI编制记录" name="generations">
        <el-form inline class="app-query" @submit.prevent="generationList.search()">
          <el-form-item label="原文件名">
            <el-input v-model="generationList.filters.filename" clearable placeholder="原始 Excel 文件名" style="width: 220px" @keyup.enter="generationList.search()" />
          </el-form-item>
          <el-form-item label="编制状态">
            <el-select v-model="generationList.filters.status" clearable placeholder="全部" style="width: 180px">
              <el-option v-for="item in generationStatusOptions" :key="item.value" :value="item.value" :label="item.label" />
            </el-select>
          </el-form-item>
          <el-form-item>
            <el-button type="primary" @click="generationList.search()">查询</el-button>
            <el-button @click="generationList.reset()">重置</el-button>
            <el-button :loading="generationList.loading.value" @click="generationList.load()">刷新</el-button>
          </el-form-item>
        </el-form>

        <el-alert v-if="generationDeleteError" class="block-gap-sm" type="error" show-icon
          :title="generationDeleteError" @close="generationDeleteError = null" />
        <el-alert v-if="generationList.error.value" type="error" show-icon :closable="false"
          :title="`读取失败：${generationList.error.value}`">
          <el-button size="small" @click="generationList.load()">重试</el-button>
        </el-alert>

        <el-table v-else :data="generationList.items.value" row-key="job_no" class="management-table rulebook-generation-table"
          v-loading="generationList.loading.value" empty-text="暂无编制记录，点击右上角「AI辅助生成规则库」开始">
          <el-table-column label="原文件" min-width="250" class-name="rulebook-table__filename" show-overflow-tooltip>
            <template #default="{ row }">
              <span>{{ row.source_filename }}</span>
              <span class="text-hint rulebook-table__size">{{ formatFileSize(row.source_size) }}</span>
            </template>
          </el-table-column>
          <el-table-column label="编制状态" :width="generationStatusWidth" class-name="rulebook-table__single-line">
            <template #default="{ row }">
              <StatusBadge
                :status="row.status"
                :label="generationStatusLabel(row.status)"
                :tone="generationStatusTone(row.status)"
              />
            </template>
          </el-table-column>
          <el-table-column label="版本" width="96" class-name="rulebook-table__single-line">
            <template #default="{ row }">{{ row.revision ? `第 ${row.revision} 版` : '尚未编制' }}</template>
          </el-table-column>
          <el-table-column label="创建时间" width="196" class-name="rulebook-table__single-line rulebook-table__time">
            <template #default="{ row }">{{ formatDate(row.created_at) }}</template>
          </el-table-column>
          <el-table-column label="修改时间" width="196" class-name="rulebook-table__single-line rulebook-table__time">
            <template #default="{ row }">{{ formatDate(row.updated_at) }}</template>
          </el-table-column>
          <el-table-column label="操作" width="124" class-name="table-actions rulebook-table__single-line">
            <template #default="{ row }">
              <el-button link type="primary" size="small" @click="openGeneration(row)">打开记录</el-button>
              <el-button link type="danger" size="small" :loading="deletingGeneration === row.job_no"
                :disabled="isOperationActive(row.latest_operation?.status) || (deletingGeneration !== null && deletingGeneration !== row.job_no)"
                :title="isOperationActive(row.latest_operation?.status) ? 'AI 操作结束后可删除' : undefined"
                @click="removeGeneration(row)">删除</el-button>
            </template>
          </el-table-column>
        </el-table>

        <div class="app-pagination">
          <el-pagination
            :current-page="generationList.page.value"
            :page-size="generationList.pageSize.value"
            :total="generationList.total.value"
            :page-sizes="PAGE_SIZE_OPTIONS"
            layout="total, sizes, prev, pager, next, jumper"
            background
            @current-change="generationList.setPage"
            @size-change="generationList.setPageSize"
          />
        </div>
      </el-tab-pane>
    </el-tabs>
  </div>
</template>

<style scoped>
.status-note { display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; overflow-wrap: anywhere; }
/* 文件资产列表：文件名与大小并排展示，时间不换行。 */
.rulebook-table__size {
  margin-left: 8px;
}
.rulebook-generation-table :deep(td.rulebook-table__single-line > .cell) {
  padding-left: 12px;
  padding-right: 12px;
  white-space: nowrap;
}
.rulebook-generation-table :deep(td.el-table__cell) {
  height: 44px;
  padding-top: 6px;
  padding-bottom: 6px;
}
.rulebook-generation-table :deep(td.rulebook-table__filename > .cell) {
  padding-inline: 14px;
  text-align: left;
}
.rulebook-generation-table :deep(.rulebook-table__time > .cell) {
  font-variant-numeric: tabular-nums;
}
</style>
