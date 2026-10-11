<script setup lang="ts">
import { computed } from "vue";
import { Download } from "lucide-vue-next";

import type { Report } from "@/api/contracts";
import StateBlock from "@/components/StateBlock.vue";
import StatusBadge from "@/components/StatusBadge.vue";

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
}>();

/** 服务端结构化结论；旧报告可能没有。 */
const conclusion = computed(() => props.report?.conclusion ?? null);

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

const scopeSummary = computed(() => {
  if (conclusion.value) {
    return conclusion.value.scope_execution_note;
  }
  return scopeComplete.value
    ? "服务端标记已配置审核范围已完整执行。"
    : "服务端标记已配置审核范围未完整执行，具体原因见下方「未完整执行或证据不足的说明」。";
});

const riskSummary = computed(() => {
  if (conclusion.value) {
    return conclusion.value.risk_note;
  }
  return riskAvailable.value
    ? "服务端标记风险可判定。"
    : "服务端标记风险不可判定，原因见下方执行说明。";
});

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
    return {
      title: "下一步：确认风险结论是否需要在系统外判断",
      description: "当前场景未配置风险评级，或风险结论尚未闭合；报告不给出「无风险」的判断。如需风险结论，请先配置风险规则。",
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

    <el-alert v-else type="success" show-icon :closable="false" title="报告当前有效，对应当前任务输入修订">
      报告已生成且仍对应当前输入。这只是「报告可读」，不代表审核通过。
    </el-alert>

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
      <el-descriptions-item label="风险是否可判定">
        <StatusBadge
          :status="riskAvailable ? 'CONFIRMED' : 'NOT_APPLICABLE'"
          :label="riskAvailable ? '可判定' : '不可判定'"
          :tone="riskAvailable ? 'success' : 'warning'"
        />
      </el-descriptions-item>
    </el-descriptions>

    <!-- 2. 服务端结构化审核结论（没有就不猜，列为接口缺口） -->
    <el-alert v-if="generated" type="info" show-icon :closable="false" title="审核结论（服务端返回）">
      <div>审核范围：{{ scopeSummary }}</div>
      <div>风险结论：{{ riskSummary }}</div>
      <div class="text-hint">
        报告是否生成只表示报告已产出，不代表审核通过；逐条判断请到「规则结果」与「风险」页签查看。
      </div>
    </el-alert>

    <el-alert
      v-if="generated"
      type="warning"
      show-icon
      :closable="false"
      title="服务端没有返回本次审核的总体结论字段"
    >
      当前报告结论只包含「是否生成 / 范围是否完整 / 风险是否可判定 / 未实现格式 / 能力缺口」，
      <strong>没有</strong>「本次审核总体是否通过」这一业务字段。页面不会从 Markdown 正文里提取或推断该结论。
      <div class="text-hint">
        需要后端接口支持：在报告结论里补充总体审核结论字段（例如总体判定与判定依据），页面才能显示。
      </div>
    </el-alert>

    <!-- 3. 下一步动作 -->
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
          <pre class="report-content">{{ report.content }}</pre>
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
          <div class="text-hint">
            当前只生成 Markdown 正文。系统不会按这些格式产出文件，也不会改扩展名冒充。
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

.report-content {
  max-height: 420px;
  margin: 0;
  overflow: auto;
  color: var(--el-text-color-regular);
  font: 13px/1.7 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}
</style>
