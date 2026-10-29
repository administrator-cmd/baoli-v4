<script setup lang="ts">
import { computed } from "vue";

import { isOperationFinished, operationToneClass, type OperationView } from "@/api/http";
import { operationPhaseLabel, operationTypeLabel, parseServerTime, statusLabel } from "@/constants/labels";
import StatusBadge from "./StatusBadge.vue";

/**
 * 后台操作状态面板。
 *
 * 只显示服务端返回的真实状态：受理中/执行中就是执行中，失败就是失败。
 * 只有拿到真实可数总量时才显示百分比，否则显示「处理中」而不是编一个进度。
 * 查询停止 ≠ 操作结束，文案必须区分开。
 */
const props = withDefaults(
  defineProps<{
    operation: OperationView | null;
    /** 正在按 operation_no 查询服务端状态。 */
    watching: boolean;
    /** 本次操作的业务名称（例如节点名）；缺省时用操作类型的中文名。 */
    label?: string | null;
    /** 查询间隔秒数，用于说明轮询行为。 */
    pollSeconds?: number;
    /** 是否显示「重新执行」按钮（操作失败或被中断时由页面决定）。 */
    canRetry?: boolean;
    /** 重新执行的按钮文案；页面应写明这是全量重跑还是原操作重试。 */
    retryLabel?: string;
    /**
     * 是否显示「继续执行」按钮：只对可原地继续的批次提供。
     * 继续执行复用原批次与已完成的规则结果；重新执行会另建批次重跑全部规则。
     */
    canResume?: boolean;
    /**
     * 是否显示「中止」按钮；由页面按「是否有尚未结束的后台操作」决定。
     * 中止只释放服务端的活跃占用，不改写业务状态。
     */
    canAbort?: boolean;
    /** 正在提交中止请求。 */
    aborting?: boolean;
    /** 真实可计算的完成百分比；没有真实总量时传 null，不显示百分比。 */
    percent?: number | null;
    /** 标题上方的分组说明。 */
    eyebrow?: string;
    /** 业务页面使用简洁布局，内部操作信息默认折叠。 */
    compact?: boolean;
  }>(),
  { label: null, pollSeconds: 2, canRetry: false, retryLabel: "重新执行", canResume: false, canAbort: false, aborting: false, percent: null, eyebrow: "后台操作", compact: false },
);

defineEmits<{
  requery: [];
  /** 重试必须带上原操作类型，页面据此按原操作入口重试（例如复核失败仍走复核接口）。 */
  retry: [operationType: string];
  /** 继续执行：复用中断批次，只跑没有结果的规则。 */
  resume: [];
  abort: [];
}>();

const title = computed(() => props.label ?? (props.operation ? operationTypeLabel(props.operation.operation_type) : "后台操作"));
const tone = computed(() => operationToneClass(props.operation?.status));
const phaseText = computed(() => operationPhaseLabel(props.operation?.phase));
/** 操作已结束、但页面没有再查询：这时允许再次查询，绝不自动重新提交。 */
const queryStopped = computed(
  () => Boolean(props.operation) && !props.watching && !isOperationFinished(props.operation?.status),
);
const hasPercent = computed(() => typeof props.percent === "number" && props.percent >= 0);

/**
 * 已运行时长：由服务端给出的开始时间计算。
 * 服务端时间不带时区后缀，必须按 UTC 解析；轮询每 2 秒触发一次重新渲染，
 * 因此不需要额外定时器。
 */
const elapsedText = computed(() => {
  const startedAt = props.operation?.started_at;
  if (!startedAt || tone.value !== "running") return null;
  const started = parseServerTime(startedAt);
  if (started === null) return null;
  const seconds = Math.max(0, Math.floor((Date.now() - started.getTime()) / 1000));
  if (seconds < 60) return `已运行 ${seconds} 秒`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `已运行 ${minutes} 分钟`;
  return `已运行 ${Math.floor(minutes / 60)} 小时 ${minutes % 60} 分钟`;
});
</script>

<template>
  <el-card v-if="operation" shadow="never" class="operation-panel" :class="{ 'operation-panel--compact': compact }">
    <template #header>
      <div class="operation-panel__header">
        <div>
          <div v-if="!compact" class="text-hint">{{ eyebrow }}</div>
          <div class="operation-panel__title">{{ title }}</div>
        </div>
        <div class="card-actions">
          <StatusBadge :status="operation.status" />
          <!-- 终态记录不会变化，重读只会得到同一份状态；只在轮询仍在进行或已中断时提供查询。 -->
          <el-button v-if="watching || queryStopped" size="small" :loading="watching" @click="$emit('requery')">再次查询</el-button>
          <el-button
            v-if="canAbort"
            size="small"
            type="warning"
            :loading="aborting"
            @click="$emit('abort')"
          >
            中止
          </el-button>
          <el-button
            v-if="canResume"
            size="small"
            type="primary"
            @click="$emit('resume')"
          >
            继续执行
          </el-button>
          <el-button
            v-if="canRetry"
            size="small"
            :type="canResume ? 'default' : 'primary'"
            @click="$emit('retry', operation.operation_type)"
          >
            {{ retryLabel }}
          </el-button>
        </div>
      </div>
    </template>

    <p v-if="compact && operation.message && !operation.failure_message" class="operation-message">{{ operation.message }}</p>

    <details :open="!compact" class="operation-details">
      <summary v-if="compact">技术信息</summary>
      <el-descriptions :column="2" size="small" border>
        <el-descriptions-item label="当前步骤">{{ operationTypeLabel(operation.operation_type) }}</el-descriptions-item>
        <el-descriptions-item label="状态">
          {{ statusLabel(operation.status) }} · {{ operation.message }}
        </el-descriptions-item>
        <el-descriptions-item v-if="phaseText" label="命令阶段">{{ phaseText }}</el-descriptions-item>
        <el-descriptions-item v-if="operation.external_job_ref" label="外部作业引用">
          <span class="mono">{{ operation.external_job_ref }}</span>
        </el-descriptions-item>
        <el-descriptions-item label="受理方式">
          {{ operation.reused ? "复用了同一次操作的受理记录，没有重复执行" : "本次新提交的后台操作" }}
        </el-descriptions-item>
        <el-descriptions-item label="操作编号">
          <span class="mono">{{ operation.operation_no }}</span>
        </el-descriptions-item>
        <el-descriptions-item v-if="operation.failure_code" label="错误码">{{ operation.failure_code }}</el-descriptions-item>
        <el-descriptions-item v-if="operation.request_id" label="请求编号"><span class="mono">{{ operation.request_id }}</span></el-descriptions-item>
      </el-descriptions>
    </details>

    <!-- 有真实总量才画进度条；否则只说明「处理中」，不编百分比 -->
    <div v-if="tone === 'running'" class="block-gap">
      <p v-if="elapsedText" class="operation-message">{{ elapsedText }}</p>
      <el-progress
        v-if="hasPercent"
        :percentage="percent as number"
        :status="undefined"
      />
      <el-progress v-else :percentage="100" :show-text="false" indeterminate :duration="3" />
    </div>


    <el-alert v-if="queryStopped" class="block-gap" type="warning" show-icon :closable="false" title="状态查询已停止">
      本次操作在服务端仍在进行。可点「再次查询」查看真实状态；系统不会自动重新提交。
    </el-alert>

    <el-alert
      v-if="operation.status === 'SUCCEEDED' && !compact"
      class="block-gap"
      type="success"
      show-icon
      :closable="false"
      title="服务端已确认本次操作完成"
    />

    <el-alert
      v-if="operation.failure_code || operation.failure_message"
      class="block-gap"
      type="error"
      show-icon
      :closable="false"
      :title="operation.failure_message || '本次操作未完成'"
    >
      <template v-if="!compact">
        <div>错误码：{{ operation.failure_code ?? "未提供错误码" }}</div>
        <div class="mono">操作编号：{{ operation.operation_no }}</div>
        <div v-if="operation.request_id" class="mono">请求编号：{{ operation.request_id }}</div>
      </template>
    </el-alert>
  </el-card>
</template>

<style scoped>
.operation-panel__header {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}

.operation-panel__title {
  font-size: 15px;
  font-weight: 600;
}

.operation-panel--compact :deep(.el-card__header) {
  padding: 12px 16px;
}

.operation-panel--compact :deep(.el-card__body) {
  padding: 12px 16px;
}

.operation-message {
  margin: 0 0 8px;
  overflow-wrap: anywhere;
}

.operation-details summary {
  width: fit-content;
  color: var(--el-text-color-secondary);
  font-size: 13px;
  cursor: pointer;
}

.operation-details[open] summary {
  margin-bottom: 12px;
}
</style>
