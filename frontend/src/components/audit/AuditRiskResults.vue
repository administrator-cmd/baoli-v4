<script setup lang="ts">
import { computed } from "vue";

import type { RiskList } from "@/api/contracts";
import StateBlock from "@/components/StateBlock.vue";
import StatGrid, { type StatItem } from "@/components/StatGrid.vue";
import StatusBadge from "@/components/StatusBadge.vue";
import { formatBusinessValue, statusLabel, statusTone } from "@/constants/labels";

/**
 * 风险结果。
 *
 * 「没有风险记录」不能写成「无风险」。首屏先给统计，把这几件事分开说清：
 *   已成立风险数（只统计 ABNORMAL）/ 未命中规则数 / 无法判断数 /
 *   是否已执行 / 是否配置风险规则。
 *
 * 读取失败、未配置、未执行、已执行但未命中、结果已失效是五种不同的状态，
 * 页面必须分开显示；把读取失败显示成「未命中」会让人以为系统查过了。
 */
const props = defineProps<{
  risks: RiskList | null;
  /** 风险评定节点是否执行过（状态来自服务端节点列表）。 */
  riskNodeExecuted: boolean;
  /** 当前场景是否配置了风险评级规则。 */
  riskConfigured: boolean;
  /** 风险结果读取中。 */
  loading?: boolean;
  /** 风险结果读取失败的中文原因；非空时绝不显示成未命中。 */
  error?: string | null;
}>();

const riskItems = computed(() => props.risks?.items ?? []);

/** 命中数只取服务端给定的 hit_count（只算 ABNORMAL），不用 items 长度顶替。 */
const hitCount = computed(() => props.risks?.hit_count ?? 0);

/** 未命中：本次执行过、结果是合格的风险规则条数。 */
const passedCount = computed(() => props.risks?.status_counts?.PASSED ?? 0);

/**
 * 无法判断数：规则执行了但没有形成结论的条数。
 * 它不等于「没有风险」，也不等于「命中」。
 */
const undecidedCount = computed(() =>
  Object.entries(props.risks?.status_counts ?? {})
    .filter(([status]) => status === "EVIDENCE_INSUFFICIENT" || status === "PENDING_REVIEW" || status === "NOT_EXECUTED")
    .reduce((sum, [, count]) => sum + count, 0),
);

/** 不适用数：来源检查不成立时该风险本次就不该判，单独一档。 */
const notApplicableCount = computed(() => props.risks?.status_counts?.NOT_APPLICABLE ?? 0);

const executing = computed(() => props.risks?.risk_executed ?? props.riskNodeExecuted);

/** 结果已失效：节点执行过、但当前有效批次里没有风险结果。 */
const resultsStale = computed(() => props.riskNodeExecuted && props.risks !== null && !props.risks.risk_executed);

const stats = computed<StatItem[]>(() => [
  {
    label: "已成立风险",
    value: hitCount.value,
    hint: "只统计判定为异常的规则",
    tone: hitCount.value > 0 ? "danger" : "neutral",
  },
  {
    label: "未命中风险规则",
    value: passedCount.value,
    hint: "已执行且合格的规则",
    tone: "neutral",
  },
  {
    label: "无法判断",
    value: undecidedCount.value,
    hint: "证据不足 / 待人工复核 / 未执行",
    tone: undecidedCount.value > 0 ? "warning" : "neutral",
  },
  {
    label: "不适用",
    value: notApplicableCount.value,
    hint: "来源检查不成立，本次不判",
    tone: "neutral",
  },
]);

const statusCountEntries = computed(() =>
  Object.entries(props.risks?.status_counts ?? {}).filter(([, count]) => count > 0),
);

/** 风险结论是否可用；不可用时必须说明是「未配置」「未执行」还是「结论尚未闭合」。 */
const conclusionState = computed(() => {
  if (!props.riskConfigured) {
    return {
      tone: "info" as const,
      title: "当前场景未配置风险评级",
      description: "模板没有配置风险规则，本次审核不会形成风险结论。这不等于没有风险，也不等于审核通过。",
    };
  }
  if (!executing.value) {
    return {
      tone: "info" as const,
      title: "风险评定尚未执行",
      description: "风险评定节点执行后，这里会显示命中风险规则的真实结果。当前不能据此认为没有风险。",
    };
  }
  if (props.risks && !props.risks.risk_conclusion_available) {
    return {
      tone: "warning" as const,
      title: "风险结论尚不可判定",
      description:
        "已配置的风险规则没有全部形成「合格 / 异常」的结论（存在无法判断或未执行）。当前不能据此认为没有风险。",
    };
  }
  return {
    tone: "success" as const,
    title: "风险评定已执行，未命中任何风险规则",
    description: "这是已配置风险规则范围内的结论，不代表审核对象整体没有风险。",
  };
});
</script>

<template>
  <StateBlock v-if="loading" kind="loading" />
  <StateBlock
    v-else-if="error"
    kind="error"
    title="风险结果读取失败"
    :description="`${error}。这不代表没有风险，请重新查询后再判断。`"
  />
  <StateBlock
    v-else-if="!risks"
    kind="empty"
    title="尚未读取到风险结果"
    description="还没有取得服务端返回的风险结果；请刷新后重试。这不代表没有风险。"
  />
  <div v-else class="stack">
    <!-- 首屏统计：把「命中 / 未命中 / 无法判断 / 不适用」分开，数量全部来自服务端 -->
    <StatGrid :items="stats" />

    <el-descriptions :column="1" size="small" border>
      <el-descriptions-item label="风险评定是否执行">
        <StatusBadge
          :status="executing ? 'SUCCEEDED' : 'NOT_EXECUTED'"
          :label="executing ? '已执行' : '未执行'"
          :tone="executing ? 'success' : 'neutral'"
        />
      </el-descriptions-item>
      <el-descriptions-item label="是否配置风险规则">
        <StatusBadge
          :status="riskConfigured ? 'CONFIGURED' : 'NOT_CONFIGURED'"
          :label="riskConfigured ? '已配置' : '未配置'"
          :tone="riskConfigured ? 'success' : 'neutral'"
        />
      </el-descriptions-item>
      <el-descriptions-item label="风险是否可判定">
        <StatusBadge
          :status="risks.risk_conclusion_available ? 'CONFIRMED' : 'NOT_APPLICABLE'"
          :label="risks.risk_conclusion_available ? '可判定' : '不可判定'"
          :tone="risks.risk_conclusion_available ? 'success' : 'warning'"
        />
      </el-descriptions-item>
      <el-descriptions-item label="规则结果分布">
        <template v-if="statusCountEntries.length">
          <StatusBadge
            v-for="[status, count] in statusCountEntries"
            :key="status"
            class="tag-gap"
            :status="status"
            :label="`${statusLabel(status)} ${count}`"
          />
        </template>
        <span v-else class="text-muted">服务端没有返回规则结果分布。</span>
      </el-descriptions-item>
    </el-descriptions>

    <el-alert v-if="hitCount === 0" :type="conclusionState.tone" show-icon :closable="false" :title="conclusionState.title">
      {{ conclusionState.description }}
    </el-alert>

    <el-alert
      v-if="resultsStale"
      type="warning"
      show-icon
      :closable="false"
      title="风险结果已失效"
    >
      风险评定节点执行过，但当前有效执行批次里没有它的结果（结果随材料或人工有效值变化被失效）。
      请重新执行风险评定节点；当前不能据此认为没有风险。
    </el-alert>

    <el-table v-if="riskItems.length" :data="riskItems" border stripe size="small">
      <el-table-column label="风险" min-width="200" show-overflow-tooltip>
        <template #default="{ row }">
          <span class="cell-strong">{{ row.name }}</span>
          <div v-if="row.risk_level" class="table-note">风险等级：{{ row.risk_level }}</div>
        </template>
      </el-table-column>
      <el-table-column label="判断" width="120">
        <template #default="{ row }">
          <StatusBadge :status="row.status" :tone="statusTone(row.status)" />
        </template>
      </el-table-column>
      <el-table-column label="判断理由" min-width="280" show-overflow-tooltip>
        <template #default="{ row }">{{ row.message || "服务端没有返回风险说明" }}</template>
      </el-table-column>
      <el-table-column label="风险明细" min-width="200" show-overflow-tooltip>
        <template #default="{ row }">
          <span v-if="row.details && Object.keys(row.details).length">{{ formatBusinessValue(row.details) }}</span>
          <span v-else class="text-muted">服务端没有返回明细</span>
        </template>
      </el-table-column>
    </el-table>
  </div>
</template>

<style scoped>
.cell-strong {
  font-weight: 600;
}
</style>
