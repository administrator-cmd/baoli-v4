<script setup lang="ts">
import { computed, onMounted, ref } from "vue";
import { useRoute, useRouter } from "vue-router";
import { ExternalLink, FileSearch2 } from "lucide-vue-next";

import {
  listAuditTasksPage,
  listFieldReviewHistory,
  type FieldReviewHistoryTaskItem,
} from "@/api/audits";
import type { AuditTaskRow, Paged } from "@/api/contracts";
import StateBlock from "@/components/StateBlock.vue";
import { formatDate, statusLabel } from "@/constants/labels";
import { PAGE_SIZE_OPTIONS } from "@/constants/pagination";

const route = useRoute();
const router = useRouter();
const activeTab = computed({
  get: () => route.query.tab === "history" ? "history" : "pending",
  set: (tab: string | number) => {
    void router.replace({ query: { ...route.query, tab: tab === "history" ? "history" : undefined } });
  },
});
const pendingRows = ref<AuditTaskRow[]>([]);
const pendingTotal = ref<number | null>(null);
const pendingPage = ref(1);
const pendingPageSize = ref(20);
const pendingLoading = ref(false);
const pendingError = ref<string | null>(null);
const historyRows = ref<FieldReviewHistoryTaskItem[]>([]);
const historyTotal = ref<number | null>(null);
const historyPage = ref(1);
const historyPageSize = ref(20);
const historyLoading = ref(false);
const historyError = ref<string | null>(null);

async function loadPending(): Promise<void> {
  pendingLoading.value = true;
  pendingError.value = null;
  try {
    const result = await listAuditTasksPage({
      name: undefined,
      plan_code: undefined,
      status: "WAITING_FIELD_REVIEW",
      created_from: undefined,
      created_to: undefined,
      page: pendingPage.value,
      page_size: pendingPageSize.value,
    });
    pendingRows.value = result.items;
    pendingTotal.value = result.total;
  } catch (error) {
    pendingError.value = error instanceof Error ? error.message : "待核对任务读取失败";
  } finally {
    pendingLoading.value = false;
  }
}

async function loadHistory(): Promise<void> {
  historyLoading.value = true;
  historyError.value = null;
  try {
    const result: Paged<FieldReviewHistoryTaskItem> = await listFieldReviewHistory(historyPage.value, historyPageSize.value);
    historyRows.value = result.items;
    historyTotal.value = result.total;
  } catch (error) {
    historyError.value = error instanceof Error ? error.message : "审核记录读取失败";
  } finally {
    historyLoading.value = false;
  }
}

function openHistory(row: FieldReviewHistoryTaskItem): void {
  void router.push({ name: "manual-review-history", params: { taskNo: row.task_no } });
}

function openTask(row: AuditTaskRow): void {
  void router.push({ name: "audit-task-detail", params: { taskNo: row.task_no }, query: { tab: "fields" } });
}

onMounted(() => {
  void loadPending();
  void loadHistory();
});
</script>

<template>
  <div class="page-container manual-review-page">
    <div class="page-header">
      <div>
        <h1 class="page-title">人工审核</h1>
        <p class="page-subtitle">按任务核对字段，并回看每次确认时的依据。</p>
      </div>
    </div>

    <el-tabs v-model="activeTab" class="review-tabs">
      <el-tab-pane name="pending">
        <template #label>待核对 <span v-if="pendingTotal !== null" class="tab-count">{{ pendingTotal }}</span></template>
        <el-alert v-if="pendingError !== null" type="error" :closable="false" :title="pendingError" show-icon>
          <el-button size="small" @click="loadPending">重试</el-button>
        </el-alert>
        <StateBlock v-else-if="pendingTotal === null" kind="loading" role="status" aria-label="正在加载待核对任务" />
        <el-table v-else :data="pendingRows" v-loading="pendingLoading" border stripe>
          <el-table-column prop="task_name" label="任务" min-width="240" show-overflow-tooltip />
          <el-table-column prop="task_no" label="任务编号" min-width="190" />
          <el-table-column prop="scenario_name" label="场景" min-width="160" show-overflow-tooltip />
          <el-table-column label="任务状态" width="150">
            <template #default="{ row }">{{ statusLabel(row.status) }}</template>
          </el-table-column>
          <el-table-column label="最近更新" width="180">
            <template #default="{ row }">{{ formatDate(row.updated_at) }}</template>
          </el-table-column>
          <el-table-column label="操作" width="130" fixed="right" align="center">
            <template #default="{ row }">
              <el-button type="primary" link @click="openTask(row)">核对字段 <ExternalLink :size="14" /></el-button>
            </template>
          </el-table-column>
          <template #empty><el-empty description="当前没有等待核对的任务" /></template>
        </el-table>
        <div v-if="pendingError === null && pendingTotal" class="app-pagination">
          <el-pagination
            v-model:current-page="pendingPage"
            v-model:page-size="pendingPageSize"
            :total="pendingTotal"
            :page-sizes="PAGE_SIZE_OPTIONS"
            layout="total, sizes, prev, pager, next, jumper"
            background
            @current-change="loadPending"
            @size-change="() => { pendingPage = 1; void loadPending(); }"
          />
        </div>
      </el-tab-pane>

      <el-tab-pane name="history">
        <template #label>已确认 <span v-if="historyTotal !== null" class="tab-count">{{ historyTotal }}</span></template>
        <el-alert v-if="historyError !== null" type="error" :closable="false" :title="historyError" show-icon>
          <el-button size="small" @click="loadHistory">重试</el-button>
        </el-alert>
        <StateBlock v-else-if="historyTotal === null" kind="loading" role="status" aria-label="正在加载已确认记录" />
        <el-table v-else :data="historyRows" v-loading="historyLoading" border stripe>
          <el-table-column prop="task_name" label="任务" min-width="240" show-overflow-tooltip />
          <el-table-column prop="task_no" label="任务编号" min-width="190" />
          <el-table-column label="确认批次" width="120" align="center">
            <template #default="{ row }">{{ row.confirmation_count }} 次</template>
          </el-table-column>
          <el-table-column label="最近确认缺项" width="140" align="center">
            <template #default="{ row }">{{ row.latest_missing_field_count }} 项</template>
          </el-table-column>
          <el-table-column label="确认时间" width="180">
            <template #default="{ row }">{{ formatDate(row.latest_confirmed_at) }}</template>
          </el-table-column>
          <el-table-column label="操作" width="130" fixed="right" align="center">
            <template #default="{ row }">
              <el-button type="primary" link @click="openHistory(row)">查看记录 <FileSearch2 :size="14" /></el-button>
            </template>
          </el-table-column>
          <template #empty><el-empty description="暂无确认记录" /></template>
        </el-table>
        <div v-if="historyError === null && historyTotal" class="app-pagination">
          <el-pagination
            v-model:current-page="historyPage"
            v-model:page-size="historyPageSize"
            :total="historyTotal"
            :page-sizes="PAGE_SIZE_OPTIONS"
            layout="total, sizes, prev, pager, next, jumper"
            background
            @current-change="loadHistory"
            @size-change="() => { historyPage = 1; void loadHistory(); }"
          />
        </div>
      </el-tab-pane>
    </el-tabs>
  </div>
</template>

<style scoped>
.manual-review-page { min-width: 0; }
.page-subtitle { margin: 6px 0 0; color: var(--el-text-color-secondary); font-size: 13px; }
.review-tabs { margin-top: 14px; }
.review-tabs :deep(.el-table) { width: 100%; }
.tab-count { margin-left: 4px; color: var(--el-text-color-secondary); font-size: 12px; }
</style>
