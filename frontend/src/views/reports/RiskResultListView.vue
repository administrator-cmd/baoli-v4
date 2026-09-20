<script setup lang="ts">
import { computed, onMounted, ref } from "vue";
import { useRouter } from "vue-router";

import type { RiskResultRow } from "@/api/contracts";
import { listPlanOptions, type PlanOption } from "@/api/plans";
import { listRiskResults, riskExportUrl, type RiskPage } from "@/api/reports";
import { usePagedList } from "@/composables/usePagedList";
import PageHeader from "@/components/PageHeader.vue";
import StatusBadge from "@/components/StatusBadge.vue";

/**
 * 风险结果：只展示当前有效执行批次的风险规则结果。
 * 命中只统计 ABNORMAL；证据不足、不适用、未执行分别展示，
 * “没有风险数据”和“报告完成”都不等于合同无风险。
 */
const router = useRouter();

const list = usePagedList<RiskResultRow, { task_no: string; scenario_code: string; plan_code: string; risk_level: string; status: string }>({
  key: "risk-results",
  initialFilters: { task_no: "", scenario_code: "", plan_code: "", risk_level: "", status: "" },
  fetch: (params) =>
    listRiskResults({
      task_no: params.task_no || undefined,
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
 * 两套等级标签并存：旧格式「高/中/低/待确认」与节点版「最高/一级/二级/三级」。
 * 硬编码任何一份都会让另一套一条都筛不出来，而页面看起来一切正常。
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

const statusOptions = [
  { value: "ABNORMAL", label: "异常（命中）" },
  { value: "PASSED", label: "正常（未命中）" },
  { value: "EVIDENCE_INSUFFICIENT", label: "证据不足" },
  { value: "NOT_APPLICABLE", label: "不适用" },
  { value: "NOT_EXECUTED", label: "未执行" },
  { value: "PENDING_REVIEW", label: "待复核" },
];

function levelTagType(level: string | null): "danger" | "warning" | "info" {
  if (level === "高") return "danger";
  if (level === "中") return "warning";
  return "info";
}

function exportUrl(): string {
  return riskExportUrl({
    task_no: filters.task_no || undefined,
    scenario_code: filters.scenario_code || undefined,
    plan_code: filters.plan_code || undefined,
    risk_level: filters.risk_level || undefined,
    status: filters.status || undefined,
  });
}

/** 方案筛选下拉：与任务、报告页一致，读取失败时提示但不阻塞查询。 */
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

function openTask(row: RiskResultRow): void {
  void router.push({ name: "audit-task-detail", params: { taskNo: row.task_no }, query: { tab: "risks" } });
}

const detail = ref<RiskResultRow | null>(null);

onMounted(async () => {
  void loadPlanOptions();
  void list.load();
});
</script>

<template>
  <div class="app-panel">
    <PageHeader
      eyebrow="报告管理"
      title="风险结果"
      subtitle="当前有效执行批次的风险结论；命中只统计异常，证据不足与不适用分别展示。"
      :show-refresh="false"
    />
    <el-form inline class="app-query" @submit.prevent="list.search()">
      <el-form-item label="任务编号">
        <el-input v-model="filters.task_no" clearable placeholder="任务编号" style="width: 190px" @keyup.enter="list.search()" />
      </el-form-item>
      <el-form-item label="所属方案">
        <el-select v-model="filters.plan_code" clearable filterable placeholder="全部方案" style="width: 200px">
          <el-option v-for="item in planOptions" :key="item.plan_code" :value="item.plan_code" :label="item.plan_name" />
        </el-select>
      </el-form-item>
      <el-form-item label="场景编码">
        <el-input v-model="filters.scenario_code" clearable placeholder="场景编码" style="width: 170px" @keyup.enter="list.search()" />
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
        <el-button tag="a" :href="exportUrl()">导出</el-button>
        <el-button :loading="loading" @click="list.load()">刷新</el-button>
      </el-form-item>
    </el-form>

    <el-alert v-if="planOptionsError" type="warning" show-icon :closable="false" :title="`方案选项读取失败：${planOptionsError}`">
      所属方案下拉为空，可改用任务编号筛选。
      <el-button size="small" @click="loadPlanOptions">重试</el-button>
    </el-alert>

    <el-alert
      v-if="filters.status === 'ABNORMAL'"
      class="block-gap-sm"
      type="warning"
      show-icon
      :closable="false"
      title="当前筛选条件：仅显示命中（异常）的风险结果"
    />

    <div class="app-toolbar">
      <div class="app-toolbar__left">
        <span class="text-muted">当前筛选作用域命中（异常）{{ hitCount }} 条；命中只统计异常，不含证据不足与不适用。</span>
      </div>
    </div>

    <el-alert v-if="error" type="error" show-icon :closable="false" :title="`读取失败：${error}`">
      <el-button size="small" @click="list.load()">重试</el-button>
    </el-alert>

    <el-table v-else :data="items" border stripe v-loading="loading" empty-text="暂无数据">
      <el-table-column label="风险编号" width="100">
        <template #default="{ row }"><span class="mono">{{ row.result_id }}</span></template>
      </el-table-column>
      <el-table-column prop="task_name" label="任务" min-width="180" show-overflow-tooltip />
      <el-table-column label="风险名称" min-width="200" show-overflow-tooltip>
        <template #default="{ row }">
          <el-button link type="primary" class="table-link" @click="detail = row">{{ row.name }}</el-button>
        </template>
      </el-table-column>
      <el-table-column label="来源规则" min-width="180" show-overflow-tooltip>
        <template #default="{ row }">{{ row.source_rule_name || "—" }}</template>
      </el-table-column>
      <el-table-column label="风险等级" width="100">
        <template #default="{ row }">
          <el-tag v-if="row.risk_level" :type="levelTagType(row.risk_level)" size="small" effect="light" disable-transitions>
            {{ row.risk_level }}
          </el-tag>
          <span v-else>—</span>
        </template>
      </el-table-column>
      <el-table-column label="当前状态" width="130">
        <template #default="{ row }"><StatusBadge :status="row.status" /></template>
      </el-table-column>
      <el-table-column label="处理建议" min-width="220" show-overflow-tooltip>
        <template #default="{ row }">{{ row.suggestion || "—" }}</template>
      </el-table-column>
      <el-table-column label="创建时间" width="170">
        <template #default="{ row }">{{ String(row.created_at).replace("T", " ").slice(0, 19) }}</template>
      </el-table-column>
      <el-table-column label="操作" width="140" fixed="right">
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
        :page-sizes="[10, 20, 50, 100]"
        layout="total, sizes, prev, pager, next, jumper"
        background
        @current-change="list.setPage"
        @size-change="list.setPageSize"
      />
    </div>

    <el-drawer :model-value="Boolean(detail)" title="风险结果详情" size="520px" @close="detail = null">
      <el-descriptions v-if="detail" :column="1" border size="small">
        <el-descriptions-item label="风险编号"><span class="mono">{{ detail.result_id }}</span></el-descriptions-item>
        <el-descriptions-item label="任务">{{ detail.task_name }}（{{ detail.task_no }}）</el-descriptions-item>
        <el-descriptions-item label="风险名称">{{ detail.name }}</el-descriptions-item>
        <el-descriptions-item label="规则键"><span class="mono">{{ detail.rule_key }}</span></el-descriptions-item>
        <el-descriptions-item label="来源规则">{{ detail.source_rule_name || "未记录来源规则" }}</el-descriptions-item>
        <el-descriptions-item label="风险等级">{{ detail.risk_level || "—" }}</el-descriptions-item>
        <el-descriptions-item label="当前状态">
          <StatusBadge :status="detail.status" />
          <div class="text-muted block-gap-sm">{{ detail.message }}</div>
        </el-descriptions-item>
        <el-descriptions-item label="处理建议">{{ detail.suggestion || "—" }}</el-descriptions-item>
        <el-descriptions-item label="创建时间">{{ String(detail.created_at).replace("T", " ").slice(0, 19) }}</el-descriptions-item>
      </el-descriptions>
      <p class="text-hint block-gap-sm">
        风险结果只来自当前有效执行批次；证据不足与不适用表示风险尚不能形成结论，不等于无风险。
      </p>
      <div class="app-toolbar">
        <div class="app-toolbar__left">
          <el-button v-if="detail" type="primary" @click="openTask(detail)">进入任务风险页签</el-button>
        </div>
      </div>
    </el-drawer>
  </div>
</template>
