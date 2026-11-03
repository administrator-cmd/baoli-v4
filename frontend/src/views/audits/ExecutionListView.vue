<script setup lang="ts">
import { onMounted } from "vue";
import { useRouter } from "vue-router";

import type { ExecutionRow } from "@/api/contracts";
import { listExecutions } from "@/api/executions";
import { usePagedList } from "@/composables/usePagedList";
import { nodeOptions } from "@/constants/labels";
import ExecutionRecordsTable from "@/components/audit/ExecutionRecordsTable.vue";
import { executionStatusOptions } from "@/utils/executionPresentation";
import { PAGE_SIZE_OPTIONS } from "@/constants/pagination";

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

const statusOptions = executionStatusOptions;

function openTask(row: ExecutionRow): void {
  void router.push({ name: "audit-task-detail", params: { taskNo: row.task_no }, query: { tab: "executions" } });
}

onMounted(() => {
  void list.load();
});
</script>

<template>
  <div class="app-panel">
    <el-form inline class="app-query" @submit.prevent="list.search()">
      <el-form-item label="执行节点">
        <el-select v-model="filters.node_code" clearable placeholder="全部节点" style="width: 160px">
          <el-option v-for="item in nodeOptions" :key="item.value" :value="item.value" :label="item.label" />
        </el-select>
      </el-form-item>
      <el-form-item label="执行状态">
        <el-select v-model="filters.status" clearable placeholder="全部" style="width: 150px">
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

    <ExecutionRecordsTable
      v-else
      :rows="items"
      :loading="loading"
      show-task
      empty-text="暂无执行记录"
      @open-task="openTask"
    />

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
  </div>
</template>
