<script setup lang="ts">
import { computed, onMounted, ref } from "vue";
import { useRouter } from "vue-router";
import { ElMessage } from "element-plus";

import type { ReportDetail, ReportRow } from "@/api/contracts";
import { listPlanOptions, type PlanOption } from "@/api/plans";
import {
  getReport,
  listReports,
  reportDownloadUrl,
  setReportArchived,
  updateReportRemark,
} from "@/api/reports";
import { usePagedList } from "@/composables/usePagedList";
import PageHeader from "@/components/PageHeader.vue";
import StatusBadge from "@/components/StatusBadge.vue";

/**
 * 审核报告：真实报告输出节点生成的快照。
 * 三个信号（报告已生成 / 范围执行完整 / 风险可判定）分别展示；备注可编辑、正文不可手改；
 * 重新生成是新快照，历史快照保留；归档只改归档状态。
 */
const router = useRouter();

const list = usePagedList<ReportRow, {
  name: string;
  task_no: string;
  scenario_code: string;
  plan_code: string;
  archived: string;
  generated_from: string;
  generated_to: string;
}>(
  {
    key: "reports",
    initialFilters: { name: "", task_no: "", scenario_code: "", plan_code: "", archived: "", generated_from: "", generated_to: "" },
    fetch: (params) =>
      listReports({
        name: params.name || undefined,
        task_no: params.task_no || undefined,
        scenario_code: params.scenario_code || undefined,
        plan_code: params.plan_code || undefined,
        archived: params.archived === "" ? undefined : params.archived === "true",
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

const selectedRows = ref<ReportRow[]>([]);
const archiveDisabled = computed(() => selectedRows.value.length !== 1);

function openTask(row: ReportRow): void {
  void router.push({ name: "audit-task-detail", params: { taskNo: row.task_no }, query: { tab: "report" } });
}

/* 详情抽屉 */
const detailVisible = ref(false);
const detailLoading = ref(false);
const detailError = ref<string | null>(null);
const detail = ref<ReportDetail | null>(null);

async function openDetail(row: ReportRow): Promise<void> {
  detailVisible.value = true;
  detailLoading.value = true;
  detailError.value = null;
  detail.value = null;
  try {
    detail.value = await getReport(row.report_no);
  } catch (failure) {
    detailError.value = failure instanceof Error ? failure.message : "报告详情读取失败";
  } finally {
    detailLoading.value = false;
  }
}

/* 备注 / 归档 */
const remarkVisible = ref(false);
const remarkRow = ref<ReportRow | null>(null);
const remarkText = ref("");
const remarkSaving = ref(false);

function openRemark(row: ReportRow): void {
  remarkRow.value = row;
  remarkText.value = row.remark ?? "";
  remarkVisible.value = true;
}

async function submitRemark(): Promise<void> {
  if (!remarkRow.value) return;
  remarkSaving.value = true;
  try {
    await updateReportRemark(remarkRow.value.report_no, remarkText.value.trim() || null);
    ElMessage.success("报告备注已保存（正文与信号不受影响）");
    remarkVisible.value = false;
    await list.load();
  } catch (failure) {
    ElMessage.error(failure instanceof Error ? failure.message : "保存失败");
  } finally {
    remarkSaving.value = false;
  }
}

async function toggleArchive(row: ReportRow): Promise<void> {
  const archiving = row.archived_at === null;
  try {
    await setReportArchived(row.report_no, archiving);
    ElMessage.success(archiving ? "报告已归档" : "已取消归档");
    await list.load();
  } catch (failure) {
    ElMessage.error(failure instanceof Error ? failure.message : "操作失败");
  }
}

/** reason 字段是结构化原因集合：把非空原因转成可读列表。 */
const detailReasons = computed(() => {
  if (!detail.value) return [] as string[];
  const labels: Record<string, string> = {
    unfinished_fields: "存在未形成有效结果的字段",
    unfinished_rules: "存在未执行、证据不足或待复核的规则",
    pending_reviews: "存在待处理的人工复核项",
    material_type_mismatches: "存在材料类型待复核项",
    risk_not_configured: "当前场景未配置风险评级规则",
    risk_inconclusive: "已配置的风险规则尚未全部形成可用结论",
    missing_rule_keys: "有已启用规则尚未形成任何结果",
    missing_required_materials: "存在尚未上传的必传材料",
    failed_nodes: "存在执行失败或被阻断的审核节点",
  };
  return Object.entries(detail.value.reasons ?? {})
    .filter(([, value]) => (Array.isArray(value) ? value.length > 0 : Boolean(value)))
    .map(([key]) => labels[key] ?? key);
});

onMounted(async () => {
  void loadPlanOptions();
  void list.load();
});
</script>

<template>
  <div class="app-panel">
    <PageHeader
      eyebrow="报告管理"
      title="审核报告"
      :show-refresh="false"
    />
    <el-form inline class="app-query" @submit.prevent="list.search()">
      <el-form-item label="报告名称">
        <el-input v-model="filters.name" clearable placeholder="请输入报告名称" style="width: 200px" @keyup.enter="list.search()" />
      </el-form-item>
      <el-form-item label="任务编号">
        <el-input v-model="filters.task_no" clearable placeholder="任务编号" style="width: 190px" @keyup.enter="list.search()" />
      </el-form-item>
      <el-form-item label="所属方案">
        <el-select v-model="filters.plan_code" clearable filterable placeholder="全部方案" style="width: 200px">
          <el-option v-for="item in planOptions" :key="item.plan_code" :value="item.plan_code" :label="item.plan_name" />
        </el-select>
      </el-form-item>
      <el-form-item label="归档状态">
        <el-select v-model="filters.archived" clearable placeholder="全部" style="width: 130px">
          <el-option value="false" label="未归档" />
          <el-option value="true" label="已归档" />
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

    <div class="app-toolbar">
      <div class="app-toolbar__left">
        <el-button
          type="danger"
          plain
          :disabled="archiveDisabled"
          @click="selectedRows[0] && toggleArchive(selectedRows[0])"
        >
          {{ selectedRows[0]?.archived_at ? "取消归档" : "归档" }}
        </el-button>
      </div>
    </div>

    <el-alert v-if="planOptionsError" type="warning" show-icon :closable="false" :title="`方案选项读取失败：${planOptionsError}`">
      所属方案下拉为空，可改用任务编号筛选。
      <el-button size="small" @click="loadPlanOptions">重试</el-button>
    </el-alert>

    <el-alert v-if="error" type="error" show-icon :closable="false" :title="`读取失败：${error}`">
      <el-button size="small" @click="list.load()">重试</el-button>
    </el-alert>

    <el-table v-else :data="items" border stripe v-loading="loading" empty-text="暂无数据" @selection-change="selectedRows = $event">
      <el-table-column type="selection" width="50" />
      <el-table-column label="报告名称" min-width="220" show-overflow-tooltip>
        <template #default="{ row }">
          <el-button link type="primary" class="table-link" @click="openDetail(row)">{{ row.report_name }}</el-button>
          <el-tag v-if="row.stale" type="warning" size="small" effect="plain" disable-transitions>已失效</el-tag>
          <el-tag v-else-if="row.current" type="success" size="small" effect="plain" disable-transitions>当前</el-tag>
        </template>
      </el-table-column>
      <el-table-column label="任务" min-width="180" show-overflow-tooltip>
        <template #default="{ row }">
          <span class="mono">{{ row.task_no }}</span>
          <div class="text-hint">{{ row.task_name }}</div>
        </template>
      </el-table-column>
      <el-table-column label="场景版本" width="150">
        <template #default="{ row }">
          {{ row.scenario_name }}
          <span v-if="row.scenario_version_no" class="text-hint"> · 第 {{ row.scenario_version_no }} 版</span>
        </template>
      </el-table-column>
      <el-table-column label="方案版本" width="140">
        <template #default="{ row }">
          <span>{{ row.plan_name || "方案名称未记录" }}</span>
        </template>
      </el-table-column>
      <el-table-column label="范围完整性" width="110">
        <template #default="{ row }">
          <el-tag :type="row.scope_execution_complete ? 'success' : 'warning'" size="small" effect="light" disable-transitions>
            {{ row.scope_execution_complete ? "完整" : "未完整" }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column label="风险可判定性" width="130">
        <template #default="{ row }">
          <el-tag :type="row.risk_conclusion_available ? 'success' : 'warning'" size="small" effect="light" disable-transitions>
            {{ row.risk_conclusion_available ? "可判定" : "不可判定" }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column label="生成时间" width="170">
        <template #default="{ row }">{{ String(row.generated_at).replace("T", " ").slice(0, 19) }}</template>
      </el-table-column>
      <el-table-column label="归档状态" width="100">
        <template #default="{ row }"><StatusBadge :status="row.status" /></template>
      </el-table-column>
      <el-table-column label="操作" width="250" fixed="right">
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
          <el-button link type="primary" size="small" @click="openRemark(row)">备注</el-button>
          <el-button link type="primary" size="small" @click="toggleArchive(row)">{{ row.archived_at ? "取消归档" : "归档" }}</el-button>
          <el-button link type="primary" size="small" @click="openTask(row)">任务</el-button>
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
                {{ detail.scope_execution_complete ? "范围执行完整" : "范围未完整执行" }}
              </el-tag>
              <el-tag :type="detail.risk_conclusion_available ? 'success' : 'warning'" size="small" effect="light" disable-transitions>
                {{ detail.risk_conclusion_available ? "风险可判定" : "风险不可判定" }}
              </el-tag>
              <el-tag v-if="detail.stale" type="warning" size="small" effect="light" disable-transitions>快照已失效</el-tag>
            </div>
            <div class="app-toolbar__right">
              <el-button v-if="detail.content" size="small" tag="a" :href="reportDownloadUrl(detail.report_no)">下载 Markdown</el-button>
            </div>
          </div>
          <el-alert
            v-if="detailReasons.length"
            class="block-gap-sm"
            type="warning"
            show-icon
            :closable="false"
            title="不可判定 / 未完整的原因（按结构化原因展示，不解析正文标题）"
          >
            <ul>
              <li v-for="reason in detailReasons" :key="reason">{{ reason }}</li>
            </ul>
          </el-alert>
          <el-descriptions :column="2" border size="small" class="block-gap-sm">
            <el-descriptions-item label="任务">{{ detail.task_name }}（{{ detail.task_no }}）</el-descriptions-item>
            <el-descriptions-item label="方案版本">
              {{ detail.plan_name || "方案名称未记录" }}
            </el-descriptions-item>
            <el-descriptions-item label="生成时间">{{ String(detail.generated_at).replace("T", " ").slice(0, 19) }}</el-descriptions-item>
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

    <el-dialog v-model="remarkVisible" title="编辑报告备注" width="600px" :close-on-click-modal="false">
      <el-form label-width="80px">
        <el-form-item label="备注">
          <el-input v-model="remarkText" type="textarea" :rows="4" maxlength="4000" placeholder="备注只用于说明，不修改报告正文与三个信号" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="remarkVisible = false">取消</el-button>
        <el-button type="primary" :loading="remarkSaving" @click="submitRemark">保存备注</el-button>
      </template>
    </el-dialog>
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
