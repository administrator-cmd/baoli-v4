<script setup lang="ts">
import { onMounted, ref } from "vue";
import { useRoute } from "vue-router";

import type { OperationLogRow } from "@/api/contracts";
import { listOperations } from "@/api/system";
import PageHeader from "@/components/PageHeader.vue";
import StatusBadge from "@/components/StatusBadge.vue";
import { usePagedList } from "@/composables/usePagedList";
import { objectTypeLabel, operationTypeLabel } from "@/constants/labels";

/**
 * 操作日志：业务写操作与后台命令并列展示。
 * 业务日志只记录已成功提交的写操作；后台命令的 PENDING/RUNNING 显示为“执行中”，
 * 既不当作成功也不当作失败；两者按编号独立可查。
 */
const route = useRoute();

const list = usePagedList<OperationLogRow, { object_type: string; operation_type: string; result: string; keyword: string; created_from: string; created_to: string }>({
  key: "system-operations",
  initialFilters: {
    object_type: "",
    operation_type: "",
    result: "",
    // 任务页可带对象编号跳转过来，直接定位该对象的操作记录。
    keyword: typeof route.query.keyword === "string" ? route.query.keyword : "",
    created_from: "",
    created_to: "",
  },
  fetch: (params) =>
    listOperations({
      object_type: params.object_type || undefined,
      operation_type: params.operation_type || undefined,
      result: params.result || undefined,
      keyword: params.keyword || undefined,
      created_from: params.created_from || undefined,
      created_to: params.created_to || undefined,
      page: params.page,
      page_size: params.page_size,
    }),
});

const { filters, page, pageSize, total, items, loading, error } = list;

const objectTypeOptions = [
  { value: "SCENARIO", label: "审核场景" },
  { value: "AUDIT_PLAN", label: "审核方案" },
  { value: "AUDIT_TASK", label: "审核任务" },
  { value: "IMPORT_JOB", label: "规则库导入" },
  { value: "AUDIT_REPORT", label: "审核报告" },
];

const resultOptions = [
  { value: "SUCCEEDED", label: "已完成" },
  { value: "ACCEPTED", label: "执行中" },
  { value: "FAILED", label: "失败" },
];

const operationTypeOptions = [
  { value: "CREATE", label: "新增" },
  { value: "UPDATE", label: "编辑" },
  { value: "PUBLISH", label: "发布" },
  { value: "DELETE", label: "删除" },
  { value: "RESTORE", label: "恢复" },
  { value: "COPY", label: "复制" },
  { value: "TASK_CREATE", label: "创建审核任务" },
  { value: "DRAFT_SAVE", label: "保存方案草稿" },
  { value: "NODE_EXECUTE", label: "业务节点执行" },
  { value: "MANUAL_REVIEW_RESUME", label: "人工复核恢复" },
  { value: "IMPORT_CONVERT", label: "规则解析与转换" },
  { value: "REPORT_ARCHIVE", label: "归档报告" },
];

function durationText(ms: number | null): string {
  if (ms === null) return "—";
  if (ms < 1000) return `${ms} 毫秒`;
  return `${(ms / 1000).toFixed(1)} 秒`;
}

const detail = ref<OperationLogRow | null>(null);

onMounted(() => {
  // 从任务页带 keyword 跳转过来时强制按该编号筛选；否则沿用上次的查询条件。
  const keywordFromQuery = typeof route.query.keyword === "string" ? route.query.keyword : "";
  if (keywordFromQuery && keywordFromQuery !== filters.keyword) {
    filters.keyword = keywordFromQuery;
    list.search();
    return;
  }
  void list.load();
});
</script>

<template>
  <div class="app-panel">
    <PageHeader
      eyebrow="系统管理"
      title="操作日志"
      subtitle="业务写操作与后台命令的追加记录；失败保留错误码与原因，命令可按编号继续查询。"
      :show-refresh="false"
    />
    <el-form inline class="app-query" @submit.prevent="list.search()">
      <el-form-item label="对象类型">
        <el-select v-model="filters.object_type" clearable placeholder="全部" style="width: 150px">
          <el-option v-for="item in objectTypeOptions" :key="item.value" :value="item.value" :label="item.label" />
        </el-select>
      </el-form-item>
      <el-form-item label="操作类型">
        <el-select v-model="filters.operation_type" clearable filterable placeholder="全部" style="width: 170px">
          <el-option v-for="item in operationTypeOptions" :key="item.value" :value="item.value" :label="item.label" />
        </el-select>
      </el-form-item>
      <el-form-item label="结果">
        <el-select v-model="filters.result" clearable placeholder="全部" style="width: 130px">
          <el-option v-for="item in resultOptions" :key="item.value" :value="item.value" :label="item.label" />
        </el-select>
      </el-form-item>
      <el-form-item label="对象/编号">
        <el-input v-model="filters.keyword" clearable placeholder="对象名称或编号" style="width: 190px" @keyup.enter="list.search()" />
      </el-form-item>
      <el-form-item label="时间">
        <el-date-picker v-model="filters.created_from" type="date" value-format="YYYY-MM-DD" placeholder="开始日期" style="width: 150px" />
        <span style="margin: 0 6px">至</span>
        <el-date-picker v-model="filters.created_to" type="date" value-format="YYYY-MM-DD" placeholder="结束日期" style="width: 150px" />
      </el-form-item>
      <el-form-item>
        <el-button type="primary" @click="list.search()">查询</el-button>
        <el-button @click="list.reset()">重置</el-button>
        <el-button :loading="loading" @click="list.load()">刷新</el-button>
      </el-form-item>
    </el-form>

    <div class="app-toolbar">
      <div class="app-toolbar__left">
        <span class="text-hint">
          业务写操作与后台命令并列展示；命令编号与操作编号都可以独立查询。
          系统当前没有登录用户体系，操作日志不记录操作人，也没有逐条请求编号。
        </span>
      </div>
    </div>

    <el-alert v-if="error" type="error" show-icon :closable="false" :title="`读取失败：${error}`">
      <el-button size="small" @click="list.load()">重试</el-button>
    </el-alert>

    <el-table v-else :data="items" border stripe v-loading="loading" empty-text="暂无数据">
      <el-table-column label="时间" width="180">
        <template #default="{ row }">{{ String(row.created_at).replace("T", " ").slice(0, 19) }}</template>
      </el-table-column>
      <el-table-column label="来源" width="100">
        <template #default="{ row }">
          <el-tag :type="row.source === 'COMMAND' ? 'warning' : 'primary'" size="small" effect="plain" disable-transitions>
            {{ row.source === "COMMAND" ? "后台命令" : "业务操作" }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column label="对象类型" width="120">
        <template #default="{ row }">{{ objectTypeLabel(row.object_type) }}</template>
      </el-table-column>
      <el-table-column label="对象名称" min-width="200" show-overflow-tooltip>
        <template #default="{ row }">
          <el-button link type="primary" class="table-link" @click="detail = row">{{ row.object_name }}</el-button>
        </template>
      </el-table-column>
      <el-table-column label="操作类型" width="160">
        <template #default="{ row }">{{ operationTypeLabel(row.operation_type) }}</template>
      </el-table-column>
      <el-table-column label="结果" width="110">
        <template #default="{ row }"><StatusBadge :status="row.result" /></template>
      </el-table-column>
      <el-table-column label="耗时" width="100">
        <template #default="{ row }">{{ durationText(row.duration_ms) }}</template>
      </el-table-column>
      <el-table-column label="操作编号 / 命令编号" width="190">
        <template #default="{ row }">
          <span class="mono">{{ row.command_no ?? row.entry_no }}</span>
        </template>
      </el-table-column>
      <el-table-column label="错误信息" min-width="220" show-overflow-tooltip>
        <template #default="{ row }">
          <template v-if="row.failure_code || row.failure_message">
            <el-tag type="danger" size="small" effect="plain" disable-transitions>{{ row.failure_code || "失败" }}</el-tag>
            <span v-if="row.failure_message" class="text-danger"> {{ row.failure_message }}</span>
          </template>
          <span v-else class="text-hint">—</span>
        </template>
      </el-table-column>
      <el-table-column label="操作" width="90" fixed="right">
        <template #default="{ row }">
          <el-button link type="primary" size="small" @click="detail = row">详情</el-button>
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

    <el-drawer :model-value="Boolean(detail)" title="操作详情" size="520px" @close="detail = null">
      <el-descriptions v-if="detail" :column="1" border size="small">
        <el-descriptions-item label="编号">{{ detail.command_no ?? detail.entry_no }}</el-descriptions-item>
        <el-descriptions-item label="来源">{{ detail.source === "COMMAND" ? "后台命令" : "业务写操作" }}</el-descriptions-item>
        <el-descriptions-item label="对象">
          {{ objectTypeLabel(detail.object_type) }} · {{ detail.object_name }}（{{ detail.object_no }}）
        </el-descriptions-item>
        <el-descriptions-item label="操作">{{ operationTypeLabel(detail.operation_type) }}</el-descriptions-item>
        <el-descriptions-item label="结果"><StatusBadge :status="detail.result" /></el-descriptions-item>
        <el-descriptions-item label="时间">{{ String(detail.created_at).replace("T", " ").slice(0, 19) }}</el-descriptions-item>
        <el-descriptions-item label="耗时">{{ durationText(detail.duration_ms) }}</el-descriptions-item>
        <el-descriptions-item v-if="detail.failure_code" label="失败码">{{ detail.failure_code }}</el-descriptions-item>
        <el-descriptions-item v-if="detail.failure_message" label="失败原因">{{ detail.failure_message }}</el-descriptions-item>
        <el-descriptions-item label="详情">
          <pre v-if="detail.detail" class="mono">{{ JSON.stringify(detail.detail, null, 2) }}</pre>
          <span v-else>—</span>
        </el-descriptions-item>
      </el-descriptions>
      <p class="text-hint block-gap-sm">
        操作日志为追加记录，不支持修改或删除；后台命令的完整阶段可通过操作编号在任务页继续查询。
      </p>
    </el-drawer>
  </div>
</template>
