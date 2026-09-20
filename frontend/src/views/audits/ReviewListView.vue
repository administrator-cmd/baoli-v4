<script setup lang="ts">
import { onMounted, ref } from "vue";
import { useRouter } from "vue-router";

import type { ReviewRow } from "@/api/contracts";
import { listReviews, reviewExportUrl } from "@/api/reviews";
import { usePagedList } from "@/composables/usePagedList";
import PageHeader from "@/components/PageHeader.vue";
import { basisTypeLabel, reviewDecisionLabel, reviewTypeLabel, statusLabel } from "@/constants/labels";
import StatusBadge from "@/components/StatusBadge.vue";

/**
 * 人工复核：跨任务的复核记录（待办与已完成）。
 * 裁决在对应任务的复核阶段提交（先字段事实、重算后再裁决规则），
 * 本页不跨任务批量裁决；操作列直接进入该任务的复核页签。
 */
const router = useRouter();

const list = usePagedList<ReviewRow, { task_no: string; review_type: string; status: string; updated_from: string; updated_to: string }>({
  key: "reviews",
  initialFilters: { task_no: "", review_type: "", status: "", updated_from: "", updated_to: "" },
  fetch: (params) =>
    listReviews({
      task_no: params.task_no || undefined,
      review_type: params.review_type || undefined,
      status: params.status || undefined,
      updated_from: params.updated_from || undefined,
      updated_to: params.updated_to || undefined,
      page: params.page,
      page_size: params.page_size,
    }),
});

const { filters, page, pageSize, total, items, loading, error } = list;

const typeOptions = [
  { value: "FIELD_CONFIRMATION", label: "字段事实" },
  { value: "RULE_RESULT", label: "规则判断" },
  { value: "MATERIAL_TYPE_MISMATCH", label: "材料类型" },
];

const statusOptions = [
  { value: "PENDING", label: "待复核" },
  { value: "RESOLVED", label: "已复核" },
  { value: "INVALIDATED", label: "已失效" },
];

function typeText(reviewType: string | null): string {
  return reviewTypeLabel(reviewType);
}

function openTask(row: ReviewRow): void {
  void router.push({ name: "audit-task-detail", params: { taskNo: row.task_no }, query: { tab: "reviews" } });
}

function exportUrl(): string {
  return reviewExportUrl({
    task_no: filters.task_no || undefined,
    review_type: filters.review_type || undefined,
    status: filters.status || undefined,
    updated_from: filters.updated_from || undefined,
    updated_to: filters.updated_to || undefined,
  });
}

const detail = ref<ReviewRow | null>(null);

onMounted(() => {
  void list.load();
});
</script>

<template>
  <div class="app-panel">
    <PageHeader
      eyebrow="审核管理"
      title="人工复核"
      subtitle="跨任务的复核记录总览；复核结论在对应任务内按批次提交，先字段事实、重算后再裁决规则。"
      :show-refresh="false"
    />
    <el-form inline class="app-query" @submit.prevent="list.search()">
      <el-form-item label="任务编号">
        <el-input v-model="filters.task_no" clearable placeholder="任务编号" style="width: 200px" @keyup.enter="list.search()" />
      </el-form-item>
      <el-form-item label="复核类型">
        <el-select v-model="filters.review_type" clearable placeholder="全部" style="width: 140px">
          <el-option v-for="item in typeOptions" :key="item.value" :value="item.value" :label="item.label" />
        </el-select>
      </el-form-item>
      <el-form-item label="状态">
        <el-select v-model="filters.status" clearable placeholder="全部" style="width: 130px">
          <el-option v-for="item in statusOptions" :key="item.value" :value="item.value" :label="item.label" />
        </el-select>
      </el-form-item>
      <el-form-item label="更新时间">
        <el-date-picker v-model="filters.updated_from" type="date" value-format="YYYY-MM-DD" placeholder="开始日期" style="width: 150px" />
        <span style="margin: 0 6px">至</span>
        <el-date-picker v-model="filters.updated_to" type="date" value-format="YYYY-MM-DD" placeholder="结束日期" style="width: 150px" />
      </el-form-item>
      <el-form-item>
        <el-button type="primary" @click="list.search()">查询</el-button>
        <el-button @click="list.reset()">重置</el-button>
        <el-button tag="a" :href="exportUrl()">导出</el-button>
        <el-button :loading="loading" @click="list.load()">刷新</el-button>
      </el-form-item>
    </el-form>

    <el-alert v-if="error" type="error" show-icon :closable="false" :title="`读取失败：${error}`">
      <el-button size="small" @click="list.load()">重试</el-button>
    </el-alert>

    <el-table v-else :data="items" border stripe v-loading="loading" empty-text="暂无数据">
      <el-table-column label="任务" min-width="190" show-overflow-tooltip>
        <template #default="{ row }">
          <span class="mono">{{ row.task_no }}</span>
          <div class="text-hint">{{ row.task_name }}</div>
        </template>
      </el-table-column>
      <el-table-column prop="scenario_name" label="场景" min-width="150" show-overflow-tooltip />
      <el-table-column label="复核对象" min-width="200" show-overflow-tooltip>
        <template #default="{ row }">
          <el-button link type="primary" class="table-link" @click="detail = row">{{ row.object_name }}</el-button>
        </template>
      </el-table-column>
      <el-table-column label="类型" width="110">
        <template #default="{ row }">{{ typeText(row.review_type) }}</template>
      </el-table-column>
      <el-table-column label="系统原判断" min-width="220" show-overflow-tooltip>
        <template #default="{ row }">
          {{ statusLabel(row.system_judgment_status) }}
          <span v-if="row.system_judgment_message" class="text-hint">· {{ row.system_judgment_message }}</span>
        </template>
      </el-table-column>
      <el-table-column label="复核后有效状态" width="140">
        <template #default="{ row }">
          <StatusBadge v-if="row.effective_status" :status="row.effective_status" />
          <span v-else>—</span>
        </template>
      </el-table-column>
      <el-table-column label="状态" width="100">
        <template #default="{ row }"><StatusBadge :status="row.status" /></template>
      </el-table-column>
      <el-table-column label="更新时间" width="170">
        <template #default="{ row }">{{ String(row.updated_at).replace("T", " ").slice(0, 19) }}</template>
      </el-table-column>
      <el-table-column label="操作" width="130" fixed="right">
        <template #default="{ row }">
          <el-button link type="primary" size="small" @click="openTask(row)">
            {{ row.status === "PENDING" ? "进入复核" : "查看任务" }}
          </el-button>
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

    <el-drawer :model-value="Boolean(detail)" title="复核详情" size="520px" @close="detail = null">
      <el-descriptions v-if="detail" :column="1" border size="small">
        <el-descriptions-item label="任务">{{ detail.task_name }}（{{ detail.task_no }}）</el-descriptions-item>
        <el-descriptions-item label="复核对象">{{ detail.object_name }}</el-descriptions-item>
        <el-descriptions-item label="类型">{{ typeText(detail.review_type) }}</el-descriptions-item>
        <el-descriptions-item label="系统原判断">
          {{ statusLabel(detail.system_judgment_status) }}
          <div v-if="detail.system_judgment_message" class="text-muted">{{ detail.system_judgment_message }}</div>
        </el-descriptions-item>
        <el-descriptions-item label="人工结论">{{ reviewDecisionLabel(detail.decision) }}</el-descriptions-item>
        <el-descriptions-item label="改动">
          <span v-if="detail.before_value !== null || detail.after_value !== null">
            {{ detail.before_value ?? "（空）" }} → {{ detail.after_value ?? "（空）" }}
          </span>
          <span v-else>—</span>
        </el-descriptions-item>
        <el-descriptions-item label="依据类型">{{ detail.basis_type ? basisTypeLabel(detail.basis_type) : "—" }}</el-descriptions-item>
        <el-descriptions-item label="复核后有效状态">
          <StatusBadge v-if="detail.effective_status" :status="detail.effective_status" />
          <span v-else>—</span>
        </el-descriptions-item>
        <el-descriptions-item label="状态"><StatusBadge :status="detail.status" /></el-descriptions-item>
      </el-descriptions>
      <div class="app-toolbar block-gap-sm">
        <div class="app-toolbar__left">
          <el-button type="primary" @click="detail && openTask(detail)">进入任务复核阶段</el-button>
        </div>
      </div>
      <p class="text-hint">复核按任务批次提交：先字段事实、重算后再裁决规则；草稿与事实版本绑定，不跨任务批量裁决。</p>
    </el-drawer>
  </div>
</template>
