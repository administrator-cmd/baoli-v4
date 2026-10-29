<script setup lang="ts">
import { computed, onMounted, ref } from "vue";
import { useRouter } from "vue-router";

import type { RiskResultRow } from "@/api/contracts";
import { listPlanOptions, type PlanOption } from "@/api/plans";
import { listRiskResults, type RiskPage } from "@/api/reports";
import { listAdminSceneOptions, type ScenarioOption } from "@/api/scenes";
import { formatDate } from "@/constants/labels";
import { statusTextOf } from "@/constants/statusVocabulary";
import { usePagedList } from "@/composables/usePagedList";
import StatusBadge from "@/components/StatusBadge.vue";
import { PAGE_SIZE_OPTIONS } from "@/constants/pagination";

/**
 * 风险结果：只展示当前有效执行批次的风险规则结果。
 * 命中只统计 ABNORMAL；证据不足、不适用、未执行分别展示，
 * “没有风险数据”和“报告完成”都不等于审核对象无风险。
 */
const router = useRouter();

const list = usePagedList<RiskResultRow, { task_name: string; scenario_code: string; plan_code: string; risk_level: string; status: string }>({
  key: "risk-results",
  initialFilters: { task_name: "", scenario_code: "", plan_code: "", risk_level: "", status: "" },
  fetch: (params) =>
    listRiskResults({
      task_name: params.task_name || undefined,
      scenario_code: params.scenario_code || undefined,
      plan_code: params.plan_code || undefined,
      risk_level: params.risk_level || undefined,
      status: params.status || undefined,
      page: params.page,
      page_size: params.page_size,
    }),
});

const { filters, page, pageSize, total, items, loading, error, lastResponse } = list;
/**
 * 最近一次成功响应。命中数与等级集合都是分页之外的附加字段，
 * 一律取服务端返回值，不用当前页数组长度或本地常量冒充。
 */
const page_ = computed(() => lastResponse.value as RiskPage | null);
/** 命中数来自服务端返回（与当前筛选同一作用域），不能用结果总数冒充。 */
const hitCount = computed(() => page_.value?.hit_count ?? 0);

/**
 * 风险等级筛选选项来自服务端返回的真实取值，不在这里硬编码。
 *
 * 各场景使用自己的等级名称，展示与筛选都保留实际取值。
 * 当前已选中的等级即使不在返回集合里也保留，避免选定后下拉项消失。
 */
const riskLevels = computed(() => {
  const values = new Set(page_.value?.risk_levels ?? []);
  if (filters.risk_level) {
    values.add(filters.risk_level);
  }
  return Array.from(values)
    .sort()
    .map((value) => ({ value, label: value }));
});

const statusOptions = computed(() => [
  "ABNORMAL",
  "PASSED",
  "EVIDENCE_INSUFFICIENT",
  "NOT_APPLICABLE",
  "NOT_EXECUTED",
  "PENDING_REVIEW",
].map((value) => ({ value, label: statusTextOf("risk", value) })));



/** 方案筛选下拉：与任务、报告页一致，读取失败时提示但不阻塞查询。 */
const planOptions = ref<PlanOption[]>([]);
const planOptionsError = ref<string | null>(null);
const sceneOptions = ref<ScenarioOption[]>([]);
const sceneOptionsError = ref<string | null>(null);

async function loadSceneOptions(): Promise<void> {
  sceneOptionsError.value = null;
  try {
    sceneOptions.value = (await listAdminSceneOptions()).items;
  } catch (failure) {
    sceneOptions.value = [];
    sceneOptionsError.value = failure instanceof Error ? failure.message : "场景选项读取失败";
  }
}

async function loadPlanOptions(): Promise<void> {
  planOptionsError.value = null;
  try {
    planOptions.value = (await listPlanOptions()).items;
  } catch (failure) {
    planOptions.value = [];
    planOptionsError.value = failure instanceof Error ? failure.message : "方案选项读取失败";
  }
}

function openTask(row: RiskResultRow): void {
  void router.push({ name: "audit-task-detail", params: { taskNo: row.task_no }, query: { tab: "risks" } });
}

const detail = ref<RiskResultRow | null>(null);

onMounted(async () => {
  void loadPlanOptions();
  void loadSceneOptions();
  void list.load();
});
</script>

<template>
  <div class="app-panel">
    <el-form inline class="app-query" @submit.prevent="list.search()">
      <el-form-item label="任务名称">
        <el-input v-model="filters.task_name" clearable placeholder="任务名称" style="width: 180px" @keyup.enter="list.search()" />
      </el-form-item>
      <el-form-item label="所属方案">
        <el-select v-model="filters.plan_code" clearable filterable placeholder="全部方案" style="width: 200px">
          <el-option v-for="item in planOptions" :key="item.plan_code" :value="item.plan_code" :label="item.plan_name" />
        </el-select>
      </el-form-item>
      <el-form-item label="所属场景">
        <el-select v-model="filters.scenario_code" clearable filterable placeholder="全部场景" style="width: 200px">
          <el-option v-for="item in sceneOptions" :key="item.scenario_code" :value="item.scenario_code" :label="item.scenario_name" />
        </el-select>
      </el-form-item>
      <el-form-item label="风险等级">
        <el-select v-model="filters.risk_level" clearable placeholder="全部" style="width: 120px">
          <el-option v-for="item in riskLevels" :key="item.value" :value="item.value" :label="item.label" />
        </el-select>
      </el-form-item>
      <el-form-item label="判断状态">
        <el-select v-model="filters.status" clearable placeholder="全部" style="width: 150px">
          <el-option v-for="item in statusOptions" :key="item.value" :value="item.value" :label="item.label" />
        </el-select>
      </el-form-item>
      <el-form-item>
        <el-button type="primary" @click="list.search()">查询</el-button>
        <el-button @click="list.reset()">重置</el-button>
        <el-button :loading="loading" @click="list.load()">刷新</el-button>
      </el-form-item>
    </el-form>

    <el-alert v-if="sceneOptionsError" type="error" show-icon :closable="false" :title="`场景选项读取失败：${sceneOptionsError}`">
      <el-button size="small" @click="loadSceneOptions">重试</el-button>
    </el-alert>

    <el-alert v-if="planOptionsError" type="warning" show-icon :closable="false" :title="`方案选项读取失败：${planOptionsError}`">
      <el-button size="small" @click="loadPlanOptions">重试</el-button>
    </el-alert>

    <el-alert
      v-if="filters.status === 'ABNORMAL'"
      class="block-gap-sm"
      type="warning"
      show-icon
      :closable="false"
      title="当前筛选条件：仅显示已确认风险的结果"
    />

    <div class="app-toolbar">
      <div class="app-toolbar__left risk-summary">
        <span class="risk-summary__hit" :class="{ 'risk-summary__hit--empty': !hitCount }">
          <span class="risk-summary__count">{{ hitCount }}</span> 条已确认风险
        </span>
        <span class="text-hint">共 {{ total }} 条结果</span>
      </div>
    </div>

    <el-alert v-if="error" type="error" show-icon :closable="false" :title="`读取失败：${error}`">
      <el-button size="small" @click="list.load()">重试</el-button>
    </el-alert>

    <el-table v-else :data="items" border stripe class="audit-result-table" v-loading="loading" empty-text="暂无数据">
      <el-table-column label="风险编号" width="140">
        <template #default="{ row }"><span class="mono">{{ row.rule_code || "—" }}</span></template>
      </el-table-column>
      <el-table-column prop="task_name" label="任务" min-width="200" show-overflow-tooltip />
      <el-table-column label="风险名称" min-width="220" show-overflow-tooltip>
        <template #default="{ row }">
          <el-button link type="primary" class="table-link" @click="detail = row">{{ row.name }}</el-button>
        </template>
      </el-table-column>
      <el-table-column label="来源规则" min-width="112" show-overflow-tooltip>
        <template #default="{ row }">{{ row.source_rule_name || "—" }}</template>
      </el-table-column>
      <el-table-column label="风险等级" width="140">
        <template #default="{ row }">
          <el-tag v-if="row.risk_level" type="info" size="small" effect="light" disable-transitions>
            {{ row.risk_level }}
          </el-tag>
          <span v-else>—</span>
        </template>
      </el-table-column>
      <el-table-column label="当前状态" width="140">
        <template #default="{ row }"><StatusBadge domain="risk" :status="row.status" /></template>
      </el-table-column>
      <el-table-column label="处理建议" min-width="160" show-overflow-tooltip>
        <template #default="{ row }">{{ row.suggestion || "—" }}</template>
      </el-table-column>
      <el-table-column label="创建时间" width="200">
        <template #default="{ row }"><span class="table-nowrap">{{ formatDate(String(row.created_at)) }}</span></template>
      </el-table-column>
      <el-table-column label="操作" width="156" class-name="table-actions" fixed="right">
        <template #default="{ row }">
          <el-button link type="primary" size="small" @click="detail = row">详情</el-button>
          <el-button link type="primary" size="small" @click="openTask(row)">查看任务</el-button>
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

    <el-drawer :model-value="Boolean(detail)" title="风险结果详情" size="520px" @close="detail = null">
      <el-descriptions v-if="detail" :column="1" border size="small">
        <el-descriptions-item label="风险编号"><span class="mono">{{ detail.rule_code || "—" }}</span></el-descriptions-item>
        <el-descriptions-item label="任务">{{ detail.task_name }}</el-descriptions-item>
        <el-descriptions-item label="风险名称">{{ detail.name }}</el-descriptions-item>
        <el-descriptions-item label="来源规则">{{ detail.source_rule_name || "未记录来源规则" }}</el-descriptions-item>
        <el-descriptions-item label="风险等级">{{ detail.risk_level || "—" }}</el-descriptions-item>
        <el-descriptions-item label="当前状态">
          <StatusBadge domain="risk" :status="detail.status" />
          <div class="text-muted block-gap-sm">{{ detail.message }}</div>
        </el-descriptions-item>
        <el-descriptions-item label="处理建议">{{ detail.suggestion || "—" }}</el-descriptions-item>
        <el-descriptions-item label="创建时间">{{ formatDate(String(detail.created_at)) }}</el-descriptions-item>
      </el-descriptions>
      <div class="app-toolbar">
        <div class="app-toolbar__left">
          <el-button v-if="detail" type="primary" @click="openTask(detail)">进入任务风险页签</el-button>
        </div>
      </div>
    </el-drawer>
  </div>
</template>

<style scoped>
.risk-summary {
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  gap: 12px;
}

.risk-summary__hit {
  color: var(--el-color-danger);
}

.risk-summary__hit--empty {
  color: var(--el-text-color-regular);
}

.risk-summary__count {
  font-size: 18px;
  font-weight: 600;
}
</style>
