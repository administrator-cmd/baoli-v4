<script setup lang="ts">
import { computed } from "vue";
import { Download } from "lucide-vue-next";

import type { Report } from "@/api/contracts";
import StateBlock from "@/components/StateBlock.vue";
import StatusBadge from "@/components/StatusBadge.vue";
import ReportBody from "@/components/audit/ReportBody.vue";

/**
 * 审核报告。
 *
 * 首屏展示报告有效性、审核范围、风险结论与缺项，不推断用户可以执行的后续操作。
 * 技术诊断（输出格式未实现、能力缺口、业务章节未配置、历史报告说明）统一收进
 * 下方「执行说明 / 报告诊断」折叠区，不占首屏。
 *
 * 「报告已生成」不等于「审核通过」：三个信号必须分开表达，不能合并成一个「通过」。
 * 如果服务端没有给出结构化总体结论，这里如实列为后端接口缺口，不从 Markdown
 * 正文里猜结论——正文任何一次措辞调整都会让字符串匹配静默退化成「没有结论」。
 */
const props = defineProps<{
  report: Report | null;
  /** 报告下载地址；服务端没有内容时为 null。 */
  downloadUrl: string | null;
  /** 当前任务的后台操作是否正在进行。 */
  running: boolean;
  loading: boolean;
  /** 报告读取失败的中文原因；非空时不能显示成「尚未生成」。 */
  error?: string | null;
}>();
const emit = defineEmits<{ "select-report": [reportNo: string] }>();

/** 报告对应的输入修订已过期：报告仍可查看，但不能当作当前结论，也不能下载。 */
const stale = computed(() => props.report?.current === false);

/** 失效的真实原因由服务端给出；页面不自行推断是输入变化还是重跑未完成。 */
const staleReason = computed(() => props.report?.stale_reason ?? null);

/** 报告是否已生成且有正文。 */
const generated = computed(() => Boolean(props.report?.report_generated && props.report?.content));

/** 只有报告真的生成、有正文且仍对应当前输入修订时才开放下载。 */
const canDownload = computed(
  () => Boolean(props.report?.report_generated && props.report?.content && props.report?.current && props.downloadUrl),
);

/** 本次审核范围是否执行完整。 */
const scopeComplete = computed(() => Boolean(props.report?.scope_execution_complete));

/** 风险是否可判定；未配置风险评级时为 false，但不代表执行故障。 */
const riskAvailable = computed(() => Boolean(props.report?.risk_conclusion_available));

/** 模板声明但当前没有文件生成实现的输出格式（如 PDF / Word / Excel）。 */
const unsupportedFormats = computed(() => (props.report?.format_support ?? []).filter((item) => !item.supported));

/** 本次报告里系统固定事实章节之外的业务附加章节数量；为零时如实说明。 */
const businessSectionCount = computed(() => props.report?.business_section_count ?? 0);
</script>

<template>
  <StateBlock v-if="loading" kind="loading" />
  <StateBlock
    v-else-if="error"
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
    <el-select v-if="report.reports.length > 1" :model-value="report.report_no" class="report-picker" aria-label="选择报告" @change="(value: string) => emit('select-report', value)">
      <el-option
        v-for="item in report.reports"
        :key="item.report_no"
        :value="item.report_no"
        :label="`${item.report_name}${item.current ? '（当前）' : '（历史）'}${item.report_generated === false ? ' · 未生成' : ''}${item.scope_execution_complete === false ? ' · 未完整' : ''}${item.unresolved_count !== undefined ? ` · 说明 ${item.unresolved_count} 项` : ''}`"
      />
    </el-select>
    <!-- 1. 首屏业务结论：报告有效性 → 范围完整性 → 风险可判定性 -->
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
      当前入口返回的报告不再对应当前执行，不能作为当前结论，也不提供下载。
      下方正文仅供查看历史结果。
    </el-alert>

    <div v-else class="report-valid-note">
      报告当前有效，对应当前任务输入修订。报告可读不代表审核通过。
    </div>

    <el-descriptions :column="4" border>
      <el-descriptions-item label="报告是否生成">
        <StatusBadge :status="generated ? 'COMPLETE' : 'NOT_GENERATED'" :label="generated ? '已生成' : '未生成'" />
      </el-descriptions-item>
      <el-descriptions-item label="审核范围是否完整">
        <StatusBadge
          :status="scopeComplete ? 'CONFIRMED' : 'INCOMPLETE'"
          :label="scopeComplete ? '已完整执行' : '未完整执行'"
          :tone="scopeComplete ? 'success' : 'warning'"
        />
      </el-descriptions-item>
      <el-descriptions-item label="风险结论状态">
        <StatusBadge
          :status="riskAvailable ? 'CONFIRMED' : 'NOT_APPLICABLE'"
          :label="riskAvailable ? '可判定' : '不可判定'"
          :tone="riskAvailable ? 'success' : 'warning'"
        />
      </el-descriptions-item>
      <el-descriptions-item label="业务缺项 / 未完成说明">
        <StatusBadge
          :status="report.reasons.length ? 'INCOMPLETE' : 'COMPLETE'"
          :label="report.reasons.length ? `${report.reasons.length} 项` : '无记录'"
          :tone="report.reasons.length ? 'warning' : 'success'"
        />
      </el-descriptions-item>
    </el-descriptions>

    <el-alert v-if="report.reasons.length" type="warning" show-icon :closable="false" title="业务缺项与未完成说明">
      <ul class="report-reasons">
        <li v-for="reason in report.reasons" :key="reason">{{ reason }}</li>
      </ul>
    </el-alert>

    <!-- 4. 报告正文 -->
    <el-card v-if="report.content" shadow="never">
      <template #header>
        <div class="report-header">
          <span>审核报告正文</span>
          <a v-if="canDownload" :href="downloadUrl as string">
            <el-button type="primary" size="small">
              <template #icon><Download :size="14" /></template>
              下载 Markdown 报告
            </el-button>
          </a>
          <span v-else class="text-hint">报告未生成或已过期，暂不提供下载。</span>
        </div>
      </template>
      <el-collapse>
        <el-collapse-item title="在页面中查看报告全文" name="content">
          <ReportBody :content="report.content" :content-type="report.content_type" />
        </el-collapse-item>
      </el-collapse>
    </el-card>

    <!-- 5. 执行说明 / 报告诊断：技术信息统一收在这里 -->
    <el-collapse class="block-gap">
      <el-collapse-item title="执行说明 / 报告诊断" name="diagnostics">
        <el-alert
          v-if="unsupportedFormats.length"
          type="warning"
          show-icon
          :closable="false"
          title="模板声明的输出格式尚未实现"
        >
          <div v-for="item in unsupportedFormats" :key="`${item.report_code}-${item.format}`">
            {{ item.report_name || item.report_code || "本报告" }} 要求 {{ item.format }}：{{ item.message }}
          </div>
        </el-alert>


        <el-alert
          v-if="!businessSectionCount"
          class="block-gap-sm"
          type="info"
          show-icon
          :closable="false"
          title="本次报告只有系统固定事实章节"
        >
          当前场景未配置业务报告章节。零业务章节不等于零报告，也不等于审核范围完整；
          审核范围是否完整以上方的「审核范围是否完整」为准。
        </el-alert>

        <el-descriptions :column="1" size="small" border class="block-gap-sm">
          <el-descriptions-item label="报告状态">
            <StatusBadge :status="report.status" />
          </el-descriptions-item>
          <el-descriptions-item label="结果范围">
            只对应当前有效事实与执行结果（服务端标记 {{ report.results_scope }}）
          </el-descriptions-item>
          <el-descriptions-item label="报告输入修订">
            <span class="mono">{{ report.snapshot?.task_input_revision ?? report.input_revision }}</span>
          </el-descriptions-item>
          <el-descriptions-item label="当前任务输入修订">
            <span class="mono">{{ report.input_revision }}</span>
          </el-descriptions-item>
        </el-descriptions>
      </el-collapse-item>
    </el-collapse>

    <el-alert
      v-if="running"
      type="info"
      show-icon
      :closable="false"
      title="任务正在执行，报告结果将在完成后更新。"
    />
  </div>
</template>

<style scoped>
.report-header {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}
.report-picker { width: min(100%, 360px); }

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

</style>
