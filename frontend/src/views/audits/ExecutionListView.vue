<script setup lang="ts">
import { onMounted, ref } from "vue";
import { useRouter } from "vue-router";

import type { ExecutionRow } from "@/api/contracts";
import { getExecution, listExecutions } from "@/api/reviews";
import { usePagedList } from "@/composables/usePagedList";
import PageHeader from "@/components/PageHeader.vue";
import { formatDate, nodeLabel, nodeOptions } from "@/constants/labels";
import StatusBadge from "@/components/StatusBadge.vue";

/**
 * 执行记录：业务节点执行（node_execution）的跨任务列表。
 * 与操作日志（请求/命令）按编号关联但互不复制；重试是否可用由任务真实状态决定。
 */
const router = useRouter();

const list = usePagedList<ExecutionRow, { task_no: string; node_code: string; status: string; started_from: string; started_to: string }>({
  key: "executions",
  initialFilters: { task_no: "", node_code: "", status: "", started_from: "", started_to: "" },
  fetch: (params) =>
    listExecutions({
      task_no: params.task_no || undefined,
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

function durationText(ms: number | null): string {
  if (ms === null) return "—";
  if (ms < 1000) return `${ms} 毫秒`;
  return `${(ms / 1000).toFixed(1)} 秒`;
}

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
      eyebrow="审核管理"
      title="执行记录"
      :show-refresh="false"
    />
    <el-form inline class="app-query" @submit.prevent="list.search()">
      <el-form-item label="任务编号">
        <el-input v-model="filters.task_no" clearable placeholder="任务编号" style="width: 200px" @keyup.enter="list.search()" />
      </el-form-item>
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

    <el-table v-else :data="items" border stripe v-loading="loading" empty-text="暂无数据">
      <el-table-column label="执行批次" width="100">
        <template #default="{ row }">第 {{ row.sequence_no }} 次</template>
      </el-table-column>
      <el-table-column label="任务" min-width="190" show-overflow-tooltip>
        <template #default="{ row }">
          <el-button link type="primary" class="table-link mono" @click="openTask(row)">{{ row.task_no }}</el-button>
          <div class="text-hint">{{ row.task_name }}</div>
        </template>
      </el-table-column>
      <el-table-column label="执行节点" width="140">
        <template #default="{ row }">{{ row.node_name || nodeLabel(row.node_code) }}</template>
      </el-table-column>
      <el-table-column label="执行状态" width="180">
        <template #default="{ row }"><StatusBadge :status="row.status" /> <el-tag v-if="row.invalidated" type="info" size="small">已失效</el-tag></template>
      </el-table-column>
      <el-table-column label="开始时间" width="170">
        <template #default="{ row }">{{ executionTime(row.started_at) }}</template>
      </el-table-column>
      <el-table-column label="结束时间" width="170">
        <template #default="{ row }">{{ executionTime(row.finished_at) }}</template>
      </el-table-column>
      <el-table-column label="耗时" width="100">
        <template #default="{ row }">{{ durationText(row.duration_ms) }}</template>
      </el-table-column>
      <el-table-column prop="failure_message" label="失败原因" min-width="200" show-overflow-tooltip>
        <template #default="{ row }">{{ row.failure_message || "—" }}</template>
      </el-table-column>
      <el-table-column label="操作" width="130" fixed="right">
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

    <el-drawer v-model="detailVisible" title="执行详情" size="560px">
      <el-alert v-if="detailError" type="error" show-icon :closable="false" :title="detailError" />
      <el-descriptions v-else-if="detail" :column="1" border size="small">
        <el-descriptions-item label="任务">{{ detail.task_name }}（{{ detail.task_no }}）</el-descriptions-item>
        <el-descriptions-item label="执行节点">{{ detail.node_name }}（第 {{ detail.sequence_no }} 次执行）</el-descriptions-item>
        <el-descriptions-item label="执行状态"><StatusBadge :status="detail.status" /></el-descriptions-item>
        <el-descriptions-item label="结果有效性">{{ detail.invalidated ? "已被后续操作取代，原执行结局保留" : "未失效" }}</el-descriptions-item>
        <el-descriptions-item label="开始时间">{{ executionTime(detail.started_at) }}</el-descriptions-item>
        <el-descriptions-item label="结束时间">{{ executionTime(detail.finished_at) }}</el-descriptions-item>
        <el-descriptions-item label="任务输入修订">{{ detail.input_revision ?? "—" }}</el-descriptions-item>
        <el-descriptions-item label="关联操作编号">{{ detail.operation_no ?? "—" }}</el-descriptions-item>
        <el-descriptions-item label="失败码">{{ detail.failure_code ?? "—" }}</el-descriptions-item>
        <el-descriptions-item label="失败原因">{{ detail.failure_message ?? "—" }}</el-descriptions-item>
        <el-descriptions-item label="结果摘要">
          <el-collapse v-if="detail.result_summary">
            <el-collapse-item title="查看本次执行结果的技术明细" name="execution-summary">
              <pre class="execution-tech">{{ JSON.stringify(detail.result_summary, null, 2) }}</pre>
            </el-collapse-item>
          </el-collapse>
          <span v-else>—</span>
        </el-descriptions-item>
      </el-descriptions>
      <div class="app-toolbar block-gap-sm">
        <div class="app-toolbar__left">
          <el-button v-if="detail" type="primary" @click="openTask(detail)">进入任务执行</el-button>
        </div>
      </div>
    </el-drawer>
  </div>
</template>

<style scoped>
/* 技术载荷按 JSON 原样呈现，但必须有换行与折行：压成一行会把抽屉撑出横向滚动条。 */
.execution-tech {
  max-height: 420px;
  margin: 0;
  overflow: auto;
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 12px;
  line-height: 1.7;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}
</style>
