<script setup lang="ts">
import { onMounted, ref } from "vue";
import { useRouter } from "vue-router";

import type { ExecutionRow } from "@/api/contracts";
import { getExecution, listExecutions } from "@/api/executions";
import { usePagedList } from "@/composables/usePagedList";
import PageHeader from "@/components/PageHeader.vue";
import { formatDate, nodeOptions } from "@/constants/labels";
import StatusBadge from "@/components/StatusBadge.vue";
import ExecutionDetailPanel from "@/components/audit/ExecutionDetailPanel.vue";
import { executionDuration, executionSummaryText } from "@/utils/executionPresentation";

/**
 * 执行记录：业务节点执行（node_execution）的跨任务列表。
 * 与操作日志（请求/命令）按编号关联但互不复制；重试是否可用由任务真实状态决定。
 */
const router = useRouter();

const list = usePagedList<ExecutionRow, { node_code: string; status: string; started_from: string; started_to: string }>({
  key: "executions",
  initialFilters: { node_code: "", status: "", started_from: "", started_to: "" },
  fetch: (params) =>
    listExecutions({
      node_code: params.node_code || undefined,
      status: params.status || undefined,
      started_from: params.started_from || undefined,
      started_to: params.started_to || undefined,
      page: params.page,
      page_size: params.page_size,
    }),
});

const { filters, page, pageSize, total, items, loading, error } = list;

const statusOptions = [
  { value: "RUNNING", label: "执行中" },
  { value: "SUCCEEDED", label: "已完成" },
  { value: "FAILED", label: "失败" },
  { value: "BLOCKED", label: "已阻断" },
  { value: "SKIPPED", label: "已跳过" },
  { value: "INVALIDATED", label: "已失效" },
];

function executionTime(value: string | null | undefined): string {
  return value ? formatDate(value) : "—";
}

function openTask(row: ExecutionRow): void {
  void router.push({ name: "audit-task-detail", params: { taskNo: row.task_no }, query: { tab: "executions" } });
}

const detail = ref<Awaited<ReturnType<typeof getExecution>> | null>(null);
const detailVisible = ref(false);
const detailError = ref<string | null>(null);

async function openDetail(row: ExecutionRow): Promise<void> {
  detailVisible.value = true;
  detail.value = null;
  detailError.value = null;
  try {
    detail.value = await getExecution(row.execution_id);
  } catch (failure) {
    detailError.value = failure instanceof Error ? failure.message : "执行详情读取失败";
  }
}

onMounted(() => {
  void list.load();
});
</script>

<template>
  <div class="app-panel">
    <PageHeader
      eyebrow="任务管理"
      title="执行记录"
      :show-refresh="false"
    />
    <el-form inline class="app-query" @submit.prevent="list.search()">
      <el-form-item label="执行节点">
        <el-select v-model="filters.node_code" clearable placeholder="全部节点" style="width: 160px">
          <el-option v-for="item in nodeOptions" :key="item.value" :value="item.value" :label="item.label" />
        </el-select>
      </el-form-item>
      <el-form-item label="执行状态">
        <el-select v-model="filters.status" clearable placeholder="全部" style="width: 130px">
          <el-option v-for="item in statusOptions" :key="item.value" :value="item.value" :label="item.label" />
        </el-select>
      </el-form-item>
      <el-form-item label="开始时间">
        <el-date-picker v-model="filters.started_from" type="date" value-format="YYYY-MM-DD" placeholder="开始日期" style="width: 150px" />
        <span style="margin: 0 6px">至</span>
        <el-date-picker v-model="filters.started_to" type="date" value-format="YYYY-MM-DD" placeholder="结束日期" style="width: 150px" />
      </el-form-item>
      <el-form-item>
        <el-button type="primary" @click="list.search()">查询</el-button>
        <el-button @click="list.reset()">重置</el-button>
        <el-button :loading="loading" @click="list.load()">刷新</el-button>
      </el-form-item>
    </el-form>

    <el-alert v-if="error" type="error" show-icon :closable="false" :title="`读取失败：${error}`">
      <el-button size="small" @click="list.load()">重试</el-button>
    </el-alert>

    <el-table v-else :data="items" border stripe class="management-table" v-loading="loading" empty-text="暂无数据">
      <el-table-column label="执行批次" width="88">
        <template #default="{ row }">第 {{ row.sequence_no }} 次</template>
      </el-table-column>
      <el-table-column label="任务" min-width="150" show-overflow-tooltip>
        <template #default="{ row }">
          <el-button link type="primary" class="table-link" @click="openTask(row)">{{ row.task_name }}</el-button>
        </template>
      </el-table-column>
      <el-table-column label="执行状态" width="112">
        <template #default="{ row }"><StatusBadge :status="row.status" /> <el-tag v-if="row.invalidated" type="info" size="small">已失效</el-tag></template>
      </el-table-column>
      <el-table-column label="开始时间" width="184">
        <template #default="{ row }"><span class="table-nowrap">{{ executionTime(row.started_at) }}</span></template>
      </el-table-column>
      <el-table-column label="结束时间" width="184">
        <template #default="{ row }"><span class="table-nowrap">{{ executionTime(row.finished_at) }}</span></template>
      </el-table-column>
      <el-table-column label="耗时" width="112">
        <template #default="{ row }">{{ executionDuration(row.duration_ms) }}</template>
      </el-table-column>
      <el-table-column label="执行结果" min-width="240">
        <template #default="{ row }">
          <div class="execution-result-summary"><span v-for="part in executionSummaryText(row).split(' · ')" :key="part">{{ part }}</span></div>
          <div v-if="row.failure_message" class="text-danger">{{ row.failure_message }}</div>
        </template>
      </el-table-column>
      <el-table-column label="操作" width="136" class-name="table-actions" fixed="right">
        <template #default="{ row }">
          <el-button link type="primary" size="small" @click="openDetail(row)">详情</el-button>
          <el-button link type="primary" size="small" @click="openTask(row)">查看任务</el-button>
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

    <el-drawer v-model="detailVisible" title="执行详情" size="min(960px, 100vw)">
      <ExecutionDetailPanel :detail="detail" :error="detailError" />
      <div class="app-toolbar block-gap-sm">
        <div class="app-toolbar__left">
          <el-button v-if="detail" type="primary" @click="openTask(detail)">进入任务执行</el-button>
        </div>
      </div>
    </el-drawer>
  </div>
</template>
