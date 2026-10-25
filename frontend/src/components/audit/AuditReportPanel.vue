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
 * 首屏只回答五个业务问题，顺序固定：
 *   1. 报告当前是否有效；2. 本次审核范围是否执行完整；3. 风险是否可判定；
 *   4. 服务端给出的结构化审核结论；5. 用户下一步需要做什么。
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
  /** 本次触发报告输出的操作是否正在进行。 */
  running: boolean;
  loading: boolean;
  /** 报告读取失败的中文原因；非空时不能显示成「尚未生成」。 */
  error?: string | null;
  /** 场景是否配置了风险规则；null 表示尚未读到风险结果。 */
  riskConfigured?: boolean | null;
  /** 当前风险结果中无法判断的条数。 */
  riskUndecidedCount?: number;
}>();
const emit = defineEmits<{ "select-report": [reportNo: string] }>();

/** 报告对应的输入修订已过期：报告仍可查看，但不能当作当前结论，也不能下载。 */
const stale = computed(() => props.report?.current === false);

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

/**
 * 用户在报告页的下一步动作。
 * 只给真实可执行的动作：重新执行报告节点、补材料、处理复核、查看逐条结论。
 */
const nextAction = computed(() => {
  if (stale.value) {
    return {
      title: "下一步：重新生成报告",
      description: "当前报告对应的任务输入已变化。请在任务详情的报告输出节点重新执行，生成对应最新输入的报告。",
    };
  }
  if (!scopeComplete.value) {
    return {
      title: "下一步：补齐未完成的审核范围",
      description: "已配置的审核范围没有全部执行完，报告只覆盖了已完成的部分。请按下方说明补材料或处理待复核项后重新执行。",
    };
  }
  if (!riskAvailable.value) {
    if (props.riskConfigured === false) {
      return {
        title: "下一步：配置风险规则",
        description: "当前场景未配置风险评级，报告不给出「无风险」的判断。如需风险结论，请先配置风险规则。",
      };
    }
    if (props.riskConfigured === null || props.riskConfigured === undefined) {
      return {
        title: "下一步：确认风险结论",
        description: "报告没有可用的风险结论；请到风险页确认风险规则是否配置、是否已完成评定。",
      };
    }
    const undecided = props.riskUndecidedCount ?? 0;
    if (undecided > 0) {
      return {
        title: "下一步：处理无法判断的风险项",
        description: `已配置风险规则，但仍有 ${undecided} 项证据不足、待复核或未执行；报告不给出「无风险」的判断。请到风险页点击「只看这 ${undecided} 项」查看具体缺项。`,
      };
    }
    return {
      title: "下一步：执行风险评定",
      description: "已配置风险规则，但本次没有形成可用的风险结论；请先执行风险评定节点，再生成报告。",
    };
  }
  return {
    title: "下一步：按逐条检查结论处理后续业务",
    description: "报告已生成且范围完整。报告本身不是「审核通过」的结论，请回到规则结果与风险页查看逐条判断与原因。",
  };
});

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
    :description="`${error}。这不代表报告尚未生成，请重新查询后再判断。`"
  />
  <StateBlock
    v-else-if="!report"
    kind="empty"
    title="尚未读取到报告"
    description="还没有取得服务端返回的报告；请刷新后重试。这不代表审核通过或未通过。"
  />
  <div v-else class="stack">
    <el-select v-if="report.reports.length > 1" :model-value="report.report_no" class="report-picker" @change="(value: string) => emit('select-report', value)">
      <el-option v-for="item in report.reports" :key="item.report_no" :value="item.report_no" :label="`${item.report_name}${item.current ? '（当前）' : '（历史）'}`" />
    </el-select>
    <!-- 1. 首屏业务结论：报告有效性 → 范围完整性 → 风险可判定性 -->
    <el-alert
      v-if="!generated"
      type="info"
      show-icon
      :closable="false"
      title="报告尚未生成"
    >
      报告输出节点执行后即可生成报告。报告未生成不代表审核通过或未通过。
      <div v-for="reason in report.reasons" :key="reason" class="text-muted">{{ reason }}</div>
    </el-alert>

    <el-alert
      v-else-if="stale"
      type="warning"
      show-icon
      :closable="false"
      title="报告当前无效：对应的任务输入已变化"
    >
      报告生成后任务输入（材料或人工有效值）发生了变化，当前入口返回的报告不再对应当前修订，不能作为当前结论，也不提供下载。
      请重新执行报告输出节点生成新报告；下方正文只作为历史内容查看。
    </el-alert>

    <div v-else class="report-valid-note">
      报告当前有效，对应当前任务输入修订。报告可读不代表审核通过。
    </div>

    <el-descriptions :column="3" border>
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
    </el-descriptions>

    <!-- 2. 下一步动作 -->
    <el-alert type="info" show-icon :closable="false" :title="nextAction.title">
      {{ nextAction.description }}
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

        <el-alert
          v-if="report.reasons.length"
          class="block-gap-sm"
          type="warning"
          show-icon
          :closable="false"
          title="未完整执行或证据不足的说明"
        >
          <div v-for="reason in report.reasons" :key="reason">{{ reason }}</div>
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
      title="报告输出节点正在执行，完成后可在这里下载 Markdown 报告。"
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

.report-valid-note {
  padding: 8px 12px;
  border-left: 3px solid var(--el-color-success);
  background: var(--el-color-success-light-9);
  color: var(--el-text-color-secondary);
  font-size: 13px;
  line-height: 1.6;
}

</style>
