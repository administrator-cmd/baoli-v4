<script setup lang="ts">
import { computed, ref } from "vue";
import { Download, Eye } from "lucide-vue-next";

import type { Report } from "@/api/contracts";
import StateBlock from "@/components/StateBlock.vue";
import StatusBadge from "@/components/StatusBadge.vue";
import ReportBody from "@/components/audit/ReportBody.vue";
import ReportGenerationNotice from "@/components/audit/ReportGenerationNotice.vue";

/**
 * 审核报告。
 *
 * 任务页保持报告清单；用户主动查看后，在弹窗中读取报告状态、缺项与正文。
 *
 * 「报告已生成」不等于「审核通过」：三个信号必须分开表达，不能合并成一个「通过」。
 * 如果服务端没有给出结构化总体结论，这里如实列为后端接口缺口，不从 Markdown
 * 正文里猜结论——正文任何一次措辞调整都会让字符串匹配静默退化成「没有结论」。
 */
const props = defineProps<{
  report: Report | null;
  /** 为每份报告生成独立下载地址；失效和未生成报告由列表禁用下载。 */
  downloadUrlFor: (reportNo: string) => string;
  /** 当前任务的后台操作是否正在进行。 */
  running: boolean;
  loading: boolean;
  /** 报告读取失败的中文原因；非空时不能显示成「尚未生成」。 */
  error?: string | null;
}>();
const emit = defineEmits<{ "select-report": [reportNo: string] }>();
const detailVisible = ref(false);
const activeReportNo = ref<string | null>(null);

/** 报告对应的输入修订已过期：报告仍可查看，但不能当作当前结论，也不能下载。 */
const stale = computed(() => props.report?.current === false);

/** 失效的真实原因由服务端给出；页面不自行推断是输入变化还是重跑未完成。 */
const staleReason = computed(() => props.report?.stale_reason ?? null);

/** 报告是否已生成且有正文。 */
const generated = computed(() => Boolean(props.report?.report_generated && props.report?.content));

/** 只有报告真的生成、有正文且仍对应当前输入修订时才开放下载。 */
const canDownload = computed(
  () => Boolean(props.report?.report_generated && props.report?.content && props.report?.current),
);

/** 本次审核范围是否执行完整。 */
const scopeComplete = computed(() => Boolean(props.report?.scope_execution_complete));

/** 风险是否可判定；未配置风险评级时为 false，但不代表执行故障。 */
const riskAvailable = computed(() => Boolean(props.report?.risk_conclusion_available));

/** 报告清单用于行列表；旧的单份报告响应也保持可读。 */
const reportRows = computed(() => {
  if (!props.report) return [];
  if (props.report.reports.length) return props.report.reports;
  if (!props.report.report_no || !props.report.report_name) return [];
  return [{
    report_no: props.report.report_no,
    report_name: props.report.report_name,
    current: props.report.current,
    report_generated: props.report.report_generated,
    scope_execution_complete: props.report.scope_execution_complete,
    unresolved_count: props.report.reasons.length,
  }];
});

function canDownloadRow(row: (typeof reportRows.value)[number]): boolean {
  return Boolean(row.report_generated && row.current);
}

const activeReport = computed(() => reportRows.value.find((row) => row.report_no === activeReportNo.value));
const detailTitle = computed(() => activeReport.value?.report_name ?? props.report?.report_name ?? "报告详情");
const detailLoadFailed = computed(() => Boolean(props.error && props.report?.report_no !== activeReportNo.value));

function openReport(row: (typeof reportRows.value)[number]): void {
  activeReportNo.value = row.report_no;
  detailVisible.value = true;
  if (row.report_no !== props.report?.report_no) emit("select-report", row.report_no);
}

function reportRowClass({ row }: { row: (typeof reportRows.value)[number] }): string {
  return row.report_no === props.report?.report_no ? "report-row-active" : "";
}
</script>

<template>
  <StateBlock v-if="loading && !report" kind="loading" />
  <StateBlock
    v-else-if="error && !report"
    kind="error"
    title="报告读取失败"
    :description="`${error}。当前无法确认报告状态。`"
  />
  <StateBlock
    v-else-if="!report"
    kind="empty"
    title="尚未读取到报告"
    description="尚未取得服务端报告数据，当前无法确认报告是否生成。"
  />
  <div v-else class="stack">
    <el-table
      :data="reportRows"
      border
      stripe
      size="small"
      class="management-table report-list"
      empty-text="暂无报告记录"
      :row-class-name="reportRowClass"
    >
      <el-table-column label="报告" min-width="220" show-overflow-tooltip>
        <template #default="{ row }"><span class="report-name">{{ row.report_name }}</span></template>
      </el-table-column>
      <el-table-column label="版本" width="100">
        <template #default="{ row }">
          <el-tag :type="row.current ? 'success' : 'info'" size="small" effect="plain" disable-transitions>
            {{ row.current ? "当前" : "历史" }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column label="生成状态" width="110">
        <template #default="{ row }">
          <el-tag :type="row.report_generated ? 'success' : 'info'" size="small" effect="light" disable-transitions>
            {{ row.report_generated ? "已生成" : "未生成" }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column label="审核范围" width="120">
        <template #default="{ row }">
          <el-tag :type="row.scope_execution_complete ? 'success' : 'warning'" size="small" effect="light" disable-transitions>
            {{ row.scope_execution_complete ? "完整" : "未完整" }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column label="缺项说明" width="110">
        <template #default="{ row }">{{ row.unresolved_count ?? "—" }}</template>
      </el-table-column>
      <el-table-column label="操作" width="150" class-name="table-actions" fixed="right">
        <template #default="{ row }">
          <el-button
            link
            type="primary"
            size="small"
            @click="openReport(row)"
          >
            <template #icon><Eye :size="14" /></template>
            查看
          </el-button>
          <el-button
            link
            type="primary"
            size="small"
            tag="a"
            :href="canDownloadRow(row) ? downloadUrlFor(row.report_no) : undefined"
            :disabled="!canDownloadRow(row)"
            :title="canDownloadRow(row) ? '下载报告正文' : '报告未生成或已失效，暂不能下载'"
          >
            <template #icon><Download :size="14" /></template>
            下载
          </el-button>
        </template>
      </el-table-column>
    </el-table>
  </div>

  <el-dialog
    v-model="detailVisible"
    :title="detailTitle"
    width="min(1100px, 92vw)"
    align-center
    append-to-body
    class="report-dialog"
  >
    <StateBlock
      v-if="detailLoadFailed"
      kind="error"
      title="报告读取失败"
      :description="`${error}。可以关闭后重新点击查看。`"
    />
    <div v-else-if="report && report.report_no === activeReportNo" v-loading="loading" class="stack report-detail">
      <el-alert
        v-if="!generated"
        type="info"
        show-icon
        :closable="false"
        title="报告尚未生成"
      >
        报告将在审核流程执行到对应步骤时生成。报告未生成不代表审核通过或未通过。
      </el-alert>

      <el-alert
        v-else-if="stale"
        type="warning"
        show-icon
        :closable="false"
        :title="staleReason ? `报告当前无效：${staleReason}` : '报告当前无效（原因未记录）'"
      >
        当前报告不再对应当前执行，不能作为当前结论，也不提供下载。正文仅供查看历史结果。
      </el-alert>

      <div v-else class="report-valid-note">
        报告当前有效，对应当前任务输入修订。报告可读不代表审核通过。
      </div>

      <div class="report-status-grid">
        <div class="report-status-item">
          <span>报告是否生成</span>
          <StatusBadge :status="generated ? 'COMPLETE' : 'NOT_GENERATED'" :label="generated ? '已生成' : '未生成'" />
        </div>
        <div class="report-status-item">
          <span>审核范围是否完整</span>
          <StatusBadge :status="scopeComplete ? 'CONFIRMED' : 'INCOMPLETE'" :label="scopeComplete ? '已完整执行' : '未完整执行'" :tone="scopeComplete ? 'success' : 'warning'" />
        </div>
        <div class="report-status-item">
          <span>风险结论状态</span>
          <StatusBadge :status="riskAvailable ? 'CONFIRMED' : 'NOT_APPLICABLE'" :label="riskAvailable ? '可判定' : '不可判定'" :tone="riskAvailable ? 'success' : 'warning'" />
        </div>
        <div class="report-status-item">
          <span>业务缺项 / 未完成说明</span>
          <StatusBadge :status="report.reasons.length ? 'INCOMPLETE' : 'COMPLETE'" :label="report.reasons.length ? `${report.reasons.length} 项` : '无记录'" :tone="report.reasons.length ? 'warning' : 'success'" />
        </div>
      </div>

      <ReportGenerationNotice :generation="report.report_generation" />

      <el-alert v-if="report.reasons.length" type="warning" show-icon :closable="false" title="业务缺项与未完成说明">
        <ul class="report-reasons">
          <li v-for="reason in report.reasons" :key="reason">{{ reason }}</li>
        </ul>
      </el-alert>

      <el-card v-if="report.content" shadow="never" class="report-content-card">
        <template #header>
          <div class="report-header">
            <span>审核报告正文</span>
            <el-button v-if="canDownload" size="small" tag="a" :href="downloadUrlFor(report.report_no ?? '')">
              <template #icon><Download :size="14" /></template>
              下载报告
            </el-button>
          </div>
        </template>
        <ReportBody :content="report.content" :content-type="report.content_type" />
      </el-card>

      <el-alert
        v-if="running"
        type="info"
        show-icon
        :closable="false"
        title="任务正在执行，报告结果将在完成后更新。"
      />
    </div>
    <StateBlock v-else kind="loading" />
  </el-dialog>
</template>

<style scoped>
.report-header {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}
.report-list {
  width: 100%;
  margin-top: 8px;
}
.report-name { font-weight: 600; }
.report-status-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 10px;
}
.report-status-item {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  min-width: 0;
  padding: 12px 14px;
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 4px;
  background: var(--el-fill-color-blank);
}
.report-status-item > span {
  color: var(--el-text-color-regular);
  font-weight: 600;
}
.report-content-card :deep(.el-card__body) {
  overflow-x: auto;
}
.report-detail {
  max-height: 72vh;
  overflow-y: auto;
  padding: 2px 4px 4px 0;
}
.report-detail > * {
  flex: 0 0 auto;
}
.report-list :deep(.report-row-active td) {
  background: var(--el-fill-color-light) !important;
}

.report-reasons {
  margin: 0;
  padding-left: 18px;
  overflow-wrap: anywhere;
}

.report-valid-note {
  padding: 8px 12px;
  border-left: 3px solid var(--el-color-success);
  background: var(--el-color-success-light-9);
  color: var(--el-text-color-secondary);
  font-size: 13px;
  line-height: 1.6;
}

@media (max-width: 640px) {
  .report-status-grid { grid-template-columns: minmax(0, 1fr); }
  .report-status-item { align-items: flex-start; flex-direction: column; gap: 6px; }
}

</style>
