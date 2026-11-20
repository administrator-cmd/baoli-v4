<script setup lang="ts">
import { computed, onMounted, ref } from "vue";
import { useRouter } from "vue-router";

import type { ReportDetail, ReportRow } from "@/api/contracts";
import { listPlanOptions, type PlanOption } from "@/api/plans";
import {
  getReport,
  listReports,
  reportDownloadUrl,
} from "@/api/reports";
import { formatDate } from "@/constants/labels";
import { usePagedList } from "@/composables/usePagedList";
import ReportGenerationNotice from "@/components/audit/ReportGenerationNotice.vue";
import { PAGE_SIZE_OPTIONS } from "@/constants/pagination";
import { REPORT_EXECUTION_NOTE, reportRiskState } from "@/utils/reportPresentation";

/**
 * 审核报告：真实报告生成项产生的快照。
 * 三个信号（报告已生成 / 范围执行完整 / 风险可判定）分别展示；正文不可手改；
 * 重新生成是新快照，历史快照保留。
 */
const router = useRouter();

const list = usePagedList<ReportRow, {
  name: string;
  task_name: string;
  scenario_code: string;
  plan_code: string;
  generated_from: string;
  generated_to: string;
}>(
  {
    key: "reports",
    initialFilters: { name: "", task_name: "", scenario_code: "", plan_code: "", generated_from: "", generated_to: "" },
    fetch: (params) =>
      listReports({
        name: params.name || undefined,
        task_name: params.task_name || undefined,
        scenario_code: params.scenario_code || undefined,
        plan_code: params.plan_code || undefined,
        generated_from: params.generated_from || undefined,
        generated_to: params.generated_to || undefined,
        page: params.page,
        page_size: params.page_size,
      }),
  },
);

const { filters, page, pageSize, total, items, loading, error } = list;

/** 方案筛选下拉：与任务、风险结果页一致，读取失败时提示但不阻塞查询。 */
const planOptions = ref<PlanOption[]>([]);
const planOptionsError = ref<string | null>(null);

async function loadPlanOptions(): Promise<void> {
  planOptionsError.value = null;
  try {
    planOptions.value = (await listPlanOptions()).items;
  } catch (failure) {
    planOptions.value = [];
    planOptionsError.value = failure instanceof Error ? failure.message : "方案选项读取失败";
  }
}

function openTask(row: ReportRow): void {
  void router.push({ name: "audit-task-detail", params: { taskNo: row.task_no }, query: { tab: "report" } });
}

/* 详情抽屉 */
const detailVisible = ref(false);
const detailLoading = ref(false);
const detailError = ref<string | null>(null);
const detail = ref<ReportDetail | null>(null);
const nonRiskNotes = computed(() => {
  const report = detail.value;
  return report ? report.execution_notes.filter((note) => !report.risk_notes.includes(note)) : [];
});
/** 详情请求代次：连续打开不同报告时，先发的请求晚返回不能覆盖后选中的那一份。 */
let detailRequest = 0;

async function openDetail(row: ReportRow): Promise<void> {
  const request = ++detailRequest;
  detailVisible.value = true;
  detailLoading.value = true;
  detailError.value = null;
  detail.value = null;
  try {
    const payload = await getReport(row.report_no);
    if (request !== detailRequest) return;
    detail.value = payload;
  } catch (failure) {
    if (request !== detailRequest) return;
    detailError.value = failure instanceof Error ? failure.message : "报告详情读取失败";
  } finally {
    if (request === detailRequest) detailLoading.value = false;
  }
}

/** 按后端结构化原因契约读取，避免把内部字段名当作面向用户的原因。 */
const detailReasons = computed(() => {
  if (!detail.value) return [] as string[];
  const reasons = detail.value.reasons ?? {};
  const messages: string[] = [];
  const unresolved = reasons.unresolved;
  let unresolvedFormat = false;
  if (Array.isArray(unresolved)) {
    for (const item of unresolved) {
      const reason = item && typeof item === "object" && !Array.isArray(item) ? item : null;
      if (reason?.status !== "NOT_GENERATED") continue;
      const message = reason?.message;
      unresolvedFormat = true;
      if (typeof message === "string" && message.trim()) {
        messages.push(message.trim());
      } else {
        messages.push("要求的报告格式文件尚未生成，具体原因未记录");
      }
    }
  }

  const formatSupport = reasons.format_support;
  if (!unresolvedFormat && Array.isArray(formatSupport)) {
    for (const item of formatSupport) {
      const formatReason = item && typeof item === "object" && !Array.isArray(item) ? item : null;
      if (formatReason?.supported !== false) continue;
      const message = typeof formatReason.message === "string" ? formatReason.message.trim() : "";
      const format = typeof formatReason.format === "string" ? formatReason.format.trim() : "";
      messages.push(message || (format ? `尚未生成 ${format} 格式文件` : "尚未生成要求的报告格式文件"));
    }
  }
  return [...new Set(messages)];
});

onMounted(async () => {
  void loadPlanOptions();
  void list.load();
});
</script>

<template>
  <div class="app-panel">
    <el-form inline class="app-query" @submit.prevent="list.search()">
      <el-form-item label="报告名称">
        <el-input v-model="filters.name" clearable placeholder="请输入报告名称" style="width: 200px" @keyup.enter="list.search()" />
      </el-form-item>
      <el-form-item label="任务名称">
        <el-input v-model="filters.task_name" clearable placeholder="任务名称" style="width: 180px" @keyup.enter="list.search()" />
      </el-form-item>
      <el-form-item label="所属方案">
        <el-select v-model="filters.plan_code" clearable filterable placeholder="全部方案" style="width: 200px">
          <el-option v-for="item in planOptions" :key="item.plan_code" :value="item.plan_code" :label="item.plan_name" />
        </el-select>
      </el-form-item>
      <el-form-item label="生成时间">
        <el-date-picker v-model="filters.generated_from" type="date" value-format="YYYY-MM-DD" placeholder="开始日期" style="width: 150px" />
        <span style="margin: 0 6px">至</span>
        <el-date-picker v-model="filters.generated_to" type="date" value-format="YYYY-MM-DD" placeholder="结束日期" style="width: 150px" />
      </el-form-item>
      <el-form-item>
        <el-button type="primary" @click="list.search()">查询</el-button>
        <el-button @click="list.reset()">重置</el-button>
        <el-button :loading="loading" @click="list.load()">刷新</el-button>
      </el-form-item>
    </el-form>

    <el-alert v-if="planOptionsError" type="warning" show-icon :closable="false" :title="`方案选项读取失败：${planOptionsError}`">
      <el-button size="small" @click="loadPlanOptions">重试</el-button>
    </el-alert>

    <el-alert v-if="error" type="error" show-icon :closable="false" :title="`读取失败：${error}`">
      <el-button size="small" @click="list.load()">重试</el-button>
    </el-alert>

    <el-table v-else :data="items" border stripe class="management-table" v-loading="loading" empty-text="暂无数据">
      <el-table-column label="报告名称" min-width="200" show-overflow-tooltip>
        <template #default="{ row }">
          <el-button link type="primary" class="table-link" @click="openDetail(row)">{{ row.report_name }}</el-button>
        </template>
      </el-table-column>
      <el-table-column label="任务" min-width="200" show-overflow-tooltip>
        <template #default="{ row }">
          <el-button link type="primary" class="table-link" @click="openTask(row)">{{ row.task_name }}</el-button>
        </template>
      </el-table-column>
      <el-table-column label="所属方案" width="140" show-overflow-tooltip>
        <template #default="{ row }">
          <span>{{ row.plan_name || "方案名称未记录" }}</span>
        </template>
      </el-table-column>
      <el-table-column label="审核项执行" width="150">
        <template #default="{ row }">
          <el-tag :type="row.scope_execution_complete ? 'success' : 'warning'" :title="REPORT_EXECUTION_NOTE" size="small" effect="light" disable-transitions>
            {{ row.scope_execution_complete ? "已全部执行" : "有未执行项" }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column label="风险结论" width="160">
        <template #default="{ row }">
          <el-tag :type="reportRiskState(row).type" size="small" effect="light" disable-transitions>
            {{ reportRiskState(row).label }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column label="生成时间" width="200">
        <template #default="{ row }"><span class="table-nowrap">{{ formatDate(String(row.generated_at)) }}</span></template>
      </el-table-column>
      <el-table-column label="操作" width="124" class-name="table-actions" fixed="right">
        <template #default="{ row }">
          <el-button link type="primary" size="small" @click="openDetail(row)">查看</el-button>
          <el-button
            link
            type="primary"
            size="small"
            tag="a"
            :href="row.report_generated ? reportDownloadUrl(row.report_no) : undefined"
            :disabled="!row.report_generated"
            :title="row.report_generated ? '下载报告正文' : '该快照没有可下载的正文（服务端会拒绝）'"
          >
            下载
          </el-button>
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

    <el-drawer v-model="detailVisible" title="报告详情" size="720px">
      <el-alert v-if="detailError" type="error" show-icon :closable="false" :title="detailError" />
      <div v-else v-loading="detailLoading">
        <template v-if="detail">
          <div class="app-toolbar">
            <div class="app-toolbar__left">
              <el-tag :type="detail.report_generated ? 'success' : 'warning'" size="small" effect="light" disable-transitions>
                {{ detail.report_generated ? "报告已生成" : "报告未生成" }}
              </el-tag>
              <el-tag :type="detail.scope_execution_complete ? 'success' : 'warning'" size="small" effect="light" disable-transitions>
                {{ detail.scope_execution_complete ? "审核项已全部执行" : "存在未执行审核项" }}
              </el-tag>
              <el-tag :type="reportRiskState(detail).type" size="small" effect="light" disable-transitions>
                {{ reportRiskState(detail).label }}
              </el-tag>
              <el-tag type="info" size="small" effect="plain" disable-transitions>{{ detail.current ? "最新版本" : "历史版本" }}</el-tag>
              <el-tag :type="detail.stale ? 'warning' : 'success'" size="small" effect="light" disable-transitions>{{ detail.stale ? "已过期" : "当前有效" }}</el-tag>
            </div>
            <div v-if="detail.stale" class="text-muted block-gap-sm">
              过期原因：{{ detail.stale_reason ?? "未记录" }}
            </div>
            <div class="app-toolbar__right">
              <el-button v-if="detail.content" size="small" tag="a" :href="reportDownloadUrl(detail.report_no)">下载 Markdown</el-button>
            </div>
          </div>
          <p class="text-hint">{{ REPORT_EXECUTION_NOTE }}风险结论已齐全表示各风险项都有明确结论，也可能包含已发现的风险。</p>
          <el-alert v-if="nonRiskNotes.length" class="block-gap-sm" type="warning" show-icon :closable="false" title="其他审核项待处理说明">
            <ul><li v-for="note in nonRiskNotes" :key="note">{{ note }}</li></ul>
          </el-alert>
          <el-alert v-if="detail.risk_notes.length" class="block-gap-sm" type="warning" show-icon :closable="false" title="尚未形成明确结论的风险项">
            <ul><li v-for="note in detail.risk_notes" :key="note">{{ note }}</li></ul>
          </el-alert>
          <ReportGenerationNotice :generation="detail.report_generation" class="block-gap-sm" />
          <el-alert
            v-if="detailReasons.length"
            class="block-gap-sm"
            type="warning"
            show-icon
            :closable="false"
            title="其他报告缺项说明"
          >
            <ul>
              <li v-for="reason in detailReasons" :key="reason">{{ reason }}</li>
            </ul>
          </el-alert>
          <el-descriptions :column="2" border size="small" class="block-gap-sm">
            <el-descriptions-item label="任务">{{ detail.task_name }}</el-descriptions-item>
            <el-descriptions-item label="所属方案">
              {{ detail.plan_name || "方案名称未记录" }}
            </el-descriptions-item>
            <el-descriptions-item label="生成时间">{{ formatDate(String(detail.generated_at)) }}</el-descriptions-item>
            <el-descriptions-item label="输入修订">
              {{ detail.input_revision ?? "修订未知" }}（任务当前 {{ detail.task_input_revision ?? "—" }}）
            </el-descriptions-item>
            <el-descriptions-item label="备注" :span="2">{{ detail.remark || "—" }}</el-descriptions-item>
          </el-descriptions>
          <el-card shadow="never" class="block-gap">
            <template #header>报告正文（Markdown）</template>
            <pre v-if="detail.content" class="report-content">{{ detail.content }}</pre>
            <span v-else class="text-muted">该快照没有可展示的正文。</span>
          </el-card>
        </template>
      </div>
    </el-drawer>
  </div>
</template>

<style scoped>
.report-content {
  margin: 0;
  max-height: 520px;
  overflow: auto;
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 12px;
  line-height: 1.7;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}
</style>
