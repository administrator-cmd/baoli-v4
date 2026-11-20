<script setup lang="ts">
import { computed, ref, shallowRef } from "vue";

import type { RiskList } from "@/api/contracts";
import StateBlock from "@/components/StateBlock.vue";
import StatusBadge from "@/components/StatusBadge.vue";
import DrawerPanel from "@/components/DrawerPanel.vue";
import AuditBusinessDetails from "@/components/audit/AuditBusinessDetails.vue";
import QuoteBlock from "@/components/audit/QuoteBlock.vue";
import { auditResultSummary } from "@/constants/auditStatus";
import { statusTextOf } from "@/constants/statusVocabulary";
import { compareNaturalLabels } from "@/utils/naturalSort";

/**
 * 风险结果。
 *
 * 首屏直接展示逐条风险结果，原文与引用保留在详情中。
 *
 * 读取失败、未配置、未执行、已执行但未命中、结果已失效是五种不同的状态，
 * 页面必须分开显示；把读取失败显示成「未命中」会让人以为系统查过了。
 */
const props = defineProps<{
  risks: RiskList | null;
  /** 风险结果读取中。 */
  loading?: boolean;
  /** 风险结果读取失败的中文原因；非空时绝不显示成未命中。 */
  error?: string | null;
}>();

const riskItems = computed(() => [...(props.risks?.items ?? [])]
  .sort((left, right) => compareNaturalLabels(left.rule_code, right.rule_code)));
type RiskItem = RiskList["items"][number];
const search = ref("");
const statusFilter = ref("ALL");
const levelFilter = ref<string | null>(null);
const sheetFilter = ref<string | null>(null);
const statusOptions = computed(() => {
  const counts = new Map<string, number>();
  for (const item of riskItems.value) {
    counts.set(item.status, (counts.get(item.status) ?? 0) + 1);
  }
  return Array.from(counts, ([status, count]) => ({
    value: status,
    label: `${statusTextOf("risk", status)}（${count}）`,
  }));
});
function countedOptions(valueOf: (item: RiskItem) => string, emptyLabel: string) {
  const counts = new Map<string, number>();
  for (const item of riskItems.value) {
    const value = valueOf(item);
    counts.set(value, (counts.get(value) ?? 0) + 1);
  }
  return Array.from(counts, ([value, count]) => ({
    value,
    label: `${value || emptyLabel}（${count}）`,
  })).sort((left, right) => compareNaturalLabels(left.value, right.value));
}
const levelOptions = computed(() => countedOptions((item) => item.risk_level ?? "", "未标注等级"));
const sheetOptions = computed(() => countedOptions((item) => item.source_sheet ?? "", "未记录工作表"));
const hasFilters = computed(() => Boolean(search.value) || statusFilter.value !== "ALL"
  || levelFilter.value !== null || sheetFilter.value !== null);
const filteredItems = computed(() => {
  const query = search.value.trim().toLocaleLowerCase();
  return riskItems.value.filter((item) => {
    if (statusFilter.value !== "ALL" && item.status !== statusFilter.value) return false;
    if (levelFilter.value !== null && (item.risk_level ?? "") !== levelFilter.value) return false;
    if (sheetFilter.value !== null && (item.source_sheet ?? "") !== sheetFilter.value) return false;
    if (!query) return true;
    return [
      item.rule_code, item.name, item.risk_level, item.source_sheet,
      statusTextOf("risk", item.status), riskSummary(item), item.message,
      ...item.evidence.flatMap((source) => [source.material_name, source.document_name, source.quote_label, source.quote_header, source.quote_period, source.quote_text]),
    ].join(" ").toLocaleLowerCase().includes(query);
  });
});
function clearFilters(): void {
  search.value = "";
  statusFilter.value = "ALL";
  levelFilter.value = null;
  sheetFilter.value = null;
}
const detailRisk = shallowRef<RiskItem | null>(null);
function hasBusinessResult(item: RiskItem): boolean { return item.details?.presentation_version === 2; }
function riskSummary(item: RiskItem): string {
  if (hasBusinessResult(item)) return auditResultSummary(item.status, item.message, item.details);
  if (item.status === "PASSED") return "未命中风险条件";
  if (item.status === "ABNORMAL") return "已命中风险条件";
  return auditResultSummary(item.status, item.message, item.details);
}
/** 原文在本次风险判断中的角色，决定标题和说明。 */
function quoteTitle(item: RiskItem): string {
  if (item.status === "ABNORMAL") return "风险依据原文";
  if (item.status === "PASSED") return "检查到的原文";
  if (item.status === "NOT_APPLICABLE") return "检查片段";
  return "已读取的原文";
}

function quoteNote(item: RiskItem): string {
  if (item.status === "PASSED") return "以下为判断风险未触发时检查到的原文，不构成风险结论。";
  if (item.status === "NOT_APPLICABLE") return "本条风险规则不适用于当前业务范围，以下片段不作为风险结论依据。";
  if (item.status === "EVIDENCE_INSUFFICIENT" || item.status === "PENDING_REVIEW") {
    return "以下为系统读取到的原文，但不足以判定风险。";
  }
  return "";
}

function noQuoteText(item: RiskItem): string {
  return item.status === "NOT_APPLICABLE" ? "本次不适用，无风险依据" : "未记录引用原文";
}
const sourceRules = computed(() => (detailRisk.value?.details?.source_rules ?? []) as Array<{ rule_name: string; status: string }>);

/** 无结果时说明实际状态，避免空列表被误读为没有风险。 */
const emptyState = computed(() => {
  if (!props.risks?.risk_configured) {
    return {
      title: "当前场景未配置风险规则",
      description: "本次审核没有风险判断项。",
    };
  }
  if (props.risks.risk_results_stale) {
    return {
      title: "风险结果已失效",
      description: "已有风险结果不再对应当前输入，不能作为当前风险结论。",
    };
  }
  if (!props.risks.risk_executed) {
    return {
      title: "风险评定尚未执行",
      description: "执行后显示各条风险规则的判断结果。",
    };
  }
  return {
    title: "暂无风险结果",
    description: "当前未返回风险规则的判断结果。",
  };
});
</script>

<template>
  <StateBlock v-if="loading" kind="loading" />
  <StateBlock
    v-else-if="error"
    kind="error"
    title="风险结果读取失败"
    :description="`${error}。当前无法确认风险结果。`"
  />
  <StateBlock
    v-else-if="!risks"
    kind="empty"
    title="尚未读取到风险结果"
    description="尚未取得服务端风险数据，不能据此判断没有风险。"
  />
  <div v-else class="stack">
    <div v-if="riskItems.length" class="risk-toolbar">
      <el-input v-model="search" clearable placeholder="搜索编号、风险、理由或原文" aria-label="搜索风险结果" class="risk-search" />
      <el-select v-model="statusFilter" aria-label="按风险结论筛选" class="risk-status-filter">
        <el-option value="ALL" :label="`全部结论（${riskItems.length}）`" />
        <el-option v-for="option in statusOptions" :key="option.value" :value="option.value" :label="option.label" />
      </el-select>
      <el-select v-model="levelFilter" placeholder="全部等级" aria-label="按风险等级筛选" clearable :empty-values="[null, undefined]" :value-on-clear="null" class="risk-level-filter">
        <el-option v-for="option in levelOptions" :key="option.value" :value="option.value" :label="option.label" />
      </el-select>
      <el-select v-model="sheetFilter" placeholder="全部工作表" aria-label="按来源工作表筛选" clearable filterable :empty-values="[null, undefined]" :value-on-clear="null" class="risk-sheet-filter">
        <el-option v-for="option in sheetOptions" :key="option.value" :value="option.value" :label="option.label" />
      </el-select>
      <el-button v-if="hasFilters" @click="clearFilters">清除筛选</el-button>
      <span class="text-hint risk-count">显示 {{ filteredItems.length }} / {{ riskItems.length }} 项</span>
    </div>
    <el-table v-if="filteredItems.length" :data="filteredItems" border stripe class="audit-result-table" @row-click="(row: RiskItem) => detailRisk = row">
      <el-table-column label="风险编号" width="140" align="center">
        <template #default="{ row }">{{ row.rule_code || "—" }}</template>
      </el-table-column>
      <el-table-column label="风险" width="180">
        <template #default="{ row }">
          <span class="cell-strong">{{ row.name }}</span>
          <div v-if="row.risk_level" class="table-note">风险等级：{{ row.risk_level }}</div>
        </template>
      </el-table-column>
      <el-table-column label="风险结论" width="160">
        <template #default="{ row }">
          <StatusBadge domain="risk" :status="row.status" />
        </template>
      </el-table-column>
      <el-table-column label="判断理由" min-width="340">
        <template #default="{ row }">
          <div class="risk-message">{{ riskSummary(row) }}</div>
        </template>
      </el-table-column>
      <el-table-column label="操作" width="124" fixed="right" align="center">
        <template #default="{ row }">
          <el-button link type="primary" @click.stop="detailRisk = row">查看详情</el-button>
        </template>
      </el-table-column>
    </el-table>
    <el-empty v-else-if="riskItems.length" description="没有符合筛选条件的风险结果">
      <el-button @click="clearFilters">清除筛选</el-button>
    </el-empty>
    <StateBlock v-else kind="empty" :title="emptyState.title" :description="emptyState.description" />
  </div>
  <DrawerPanel :open="Boolean(detailRisk)" :title="detailRisk?.name ?? '风险详情'" size="min(900px, 100%)" @close="detailRisk = null">
    <template v-if="detailRisk">
      <StatusBadge domain="risk" :status="detailRisk.status" />
      <span v-if="detailRisk.rule_code" class="risk-code">{{ detailRisk.rule_code }}</span>
      <span v-if="detailRisk.risk_level" class="risk-level">规则等级：{{ detailRisk.risk_level }}</span>
      <p>{{ riskSummary(detailRisk) }}</p>
      <AuditBusinessDetails
        :result="detailRisk.details"
        :source-files="detailRisk.evidence.map((item) => item.document_name).filter((name): name is string => Boolean(name))"
        :material-names="detailRisk.evidence.map((item) => item.material_name).filter((name): name is string => Boolean(name))"
      />
      <template v-if="sourceRules.length">
        <h4 class="app-section-title">来源审核项</h4>
        <el-table :data="sourceRules" border size="small">
          <el-table-column prop="rule_name" label="审核项" min-width="220" />
          <!-- 来源审核项沿用规则状态文案，列宽按「已产出（无结论）」留足。 -->
          <el-table-column label="结论" width="184"><template #default="{ row }"><StatusBadge domain="rule" :status="row.status" /></template></el-table-column>
        </el-table>
      </template>
      <template v-if="detailRisk.evidence.length && detailRisk.status !== 'NOT_APPLICABLE'">
        <h4 class="app-section-title">{{ quoteTitle(detailRisk) }}</h4>
        <p v-if="quoteNote(detailRisk)" class="risk-quote-note">{{ quoteNote(detailRisk) }}</p>
        <div v-for="(source, index) in detailRisk.evidence" :key="index" class="risk-source">
          <div class="text-muted">
            {{ source.document_name || '来源文件未记录' }}
            <template v-if="!source.document_name && source.material_name"> · 材料位：{{ source.material_name }}</template>
            <template v-if="source.field_path"> · {{ source.field_path }}</template>
          </div>
          <QuoteBlock
            v-if="source.quote_text"
            :text="source.quote_text"
            :label="source.quote_label"
            :header="source.quote_header"
            :period="source.quote_period"
            :statement-scope="source.quote_statement_scope"
            :entity-scope="source.quote_entity_scope"
            :unit="source.quote_unit"
            :page="source.artifact_page"
          />
          <div v-else class="text-muted">该引用记录产物来源，未提供具体原文。</div>
        </div>
      </template>
      <p v-else class="text-muted">{{ noQuoteText(detailRisk) }}</p>
      <el-collapse v-if="!hasBusinessResult(detailRisk)" class="block-gap">
        <el-collapse-item title="原执行说明" name="technical">
          <div class="original-message">{{ detailRisk.message || "未记录" }}</div>
        </el-collapse-item>
      </el-collapse>
    </template>
  </DrawerPanel>
</template>

<style scoped>
.risk-toolbar {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
}
.risk-search {
  width: min(100%, 300px);
}
.risk-status-filter {
  width: 230px;
}
.risk-level-filter {
  width: 180px;
}
.risk-sheet-filter {
  width: 220px;
}
.risk-count {
  margin-left: auto;
  font-size: var(--el-font-size-base);
}
.cell-strong {
  font-weight: 600;
}
.stack :deep(.el-table__cell) {
  padding: 6px 0;
}
.stack :deep(.el-table .cell) {
  line-height: 1.45;
}
.risk-code, .risk-level { margin-left: 12px; color: var(--el-text-color-secondary); }
.risk-source { margin: 12px 0; padding: 12px; background: var(--el-fill-color-light); }
.original-message { white-space: pre-wrap; overflow-wrap: anywhere; }
.risk-message {
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  line-height: 1.45;
}

.risk-quote-note {
  margin: 0 0 8px;
  color: var(--el-text-color-secondary);
  font-size: 13px;
  line-height: 1.6;
}

</style>
