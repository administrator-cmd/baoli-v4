<script setup lang="ts">
import { onMounted, ref } from "vue";
import { useRoute, useRouter } from "vue-router";

import type { Evidence, EvidenceRow } from "@/api/contracts";
import { getTaskEvidence } from "@/api/audits";
import { evidenceExportUrl, listEvidence } from "@/api/materials";
import { usePagedList } from "@/composables/usePagedList";
import PageHeader from "@/components/PageHeader.vue";
import { locatorStatusLabel, locatorStatusTone, toneTagType } from "@/constants/labels";
import EvidenceDrawer from "@/components/EvidenceDrawer.vue";

/**
 * 证据记录：跨任务的证据引用。
 * 定位核对状态如实展示（未核实/无法定位不写成已核实）；历史证据按历史身份查看，
 * 失效只表示不再支撑当前结论，不等于删除证据。
 */
const route = useRoute();
const router = useRouter();

const list = usePagedList<EvidenceRow, { task_no: string; reference_type: string; locator_status: string; keyword: string }>({
  key: "evidence",
  initialFilters: {
    task_no: typeof route.query.task_no === "string" ? route.query.task_no : "",
    reference_type: "",
    locator_status: "",
    keyword: "",
  },
  fetch: (params) =>
    listEvidence({
      task_no: params.task_no || undefined,
      reference_type: params.reference_type || undefined,
      locator_status: params.locator_status || undefined,
      keyword: params.keyword || undefined,
      page: params.page,
      page_size: params.page_size,
    }),
});

const { filters, page, pageSize, total, items, loading, error } = list;

const referenceTypeOptions = [
  { value: "FIELD_FACT", label: "字段事实证据" },
  { value: "RULE_RESULT", label: "规则判定证据" },
  { value: "ARTIFACT", label: "处理产物证据" },
];

const locatorOptions = [
  { value: "VERIFIED", label: "已核实定位" },
  { value: "UNVERIFIED", label: "引文未核实" },
  { value: "UNLOCATABLE", label: "无法定位" },
];

function referenceTypeText(value: string): string {
  return value === "FIELD_FACT" ? "字段事实" : value === "RULE_RESULT" ? "规则判定" : "处理产物";
}

function exportUrl(): string {
  return evidenceExportUrl({
    task_no: filters.task_no || undefined,
    reference_type: filters.reference_type || undefined,
    locator_status: filters.locator_status || undefined,
    keyword: filters.keyword || undefined,
  });
}

function openTask(row: EvidenceRow): void {
  void router.push({ name: "audit-task-detail", params: { taskNo: row.task_no }, query: { tab: "fields" } });
}

/* ---- 详情（复用任务级证据查看：核对定位、历史身份） ---- */
const evidence = ref<Evidence | null>(null);
const evidenceOpen = ref(false);
const evidenceLoading = ref(false);
const evidenceError = ref<string | null>(null);
const activeRow = ref<EvidenceRow | null>(null);

async function openEvidence(row: EvidenceRow): Promise<void> {
  activeRow.value = row;
  evidenceOpen.value = true;
  evidence.value = null;
  evidenceError.value = null;
  evidenceLoading.value = true;
  try {
    evidence.value = await getTaskEvidence(row.task_no, row.evidence_no);
  } catch (failure) {
    evidenceError.value = failure instanceof Error ? failure.message : "证据读取失败";
  } finally {
    evidenceLoading.value = false;
  }
}

onMounted(() => {
  // 从任务页带 task_no 跳转过来时强制按该任务筛选；否则沿用上次的查询条件。
  const taskFromQuery = typeof route.query.task_no === "string" ? route.query.task_no : "";
  if (taskFromQuery && taskFromQuery !== filters.task_no) {
    filters.task_no = taskFromQuery;
    list.search();
    return;
  }
  void list.load();
});
</script>

<template>
  <div class="app-panel">
    <PageHeader
      eyebrow="资料管理"
      title="证据记录"
      subtitle="字段、规则与处理产物引用的原文证据；定位核对状态如实展示，点击行查看完整证据。"
      :show-refresh="false"
    />
    <el-form inline class="app-query" @submit.prevent="list.search()">
      <el-form-item label="任务编号">
        <el-input v-model="filters.task_no" clearable placeholder="任务编号" style="width: 190px" @keyup.enter="list.search()" />
      </el-form-item>
      <el-form-item label="依据类型">
        <el-select v-model="filters.reference_type" clearable placeholder="全部" style="width: 150px">
          <el-option v-for="item in referenceTypeOptions" :key="item.value" :value="item.value" :label="item.label" />
        </el-select>
      </el-form-item>
      <el-form-item label="定位核对">
        <el-select v-model="filters.locator_status" clearable placeholder="全部" style="width: 140px">
          <el-option v-for="item in locatorOptions" :key="item.value" :value="item.value" :label="item.label" />
        </el-select>
      </el-form-item>
      <el-form-item label="引用原文">
        <el-input v-model="filters.keyword" clearable placeholder="按原文关键字检索" style="width: 200px" @keyup.enter="list.search()" />
      </el-form-item>
      <el-form-item>
        <el-button type="primary" @click="list.search()">查询</el-button>
        <el-button @click="list.reset()">重置</el-button>
        <el-button tag="a" :href="exportUrl()">导出</el-button>
        <el-button :loading="loading" @click="list.load()">刷新</el-button>
      </el-form-item>
    </el-form>

    <div class="app-toolbar">
      <div class="app-toolbar__left">
        <span class="text-hint">
          点击行可查看完整证据。后端只支持按任务、依据类型、定位核对与引用原文筛选；
          按规则/材料/页码/字段过滤，以及「置信度」，后端证据记录未提供，页面不显示。
        </span>
      </div>
    </div>

    <el-alert v-if="error" type="error" show-icon :closable="false" :title="`读取失败：${error}`">
      <el-button size="small" @click="list.load()">重试</el-button>
    </el-alert>

    <el-table
      v-else
      :data="items"
      border
      stripe
      v-loading="loading"
      empty-text="暂无数据"
      @row-click="openEvidence"
    >
      <el-table-column label="证据编号" width="170">
        <template #default="{ row }">
          <el-button link type="primary" class="table-link mono" @click.stop="openEvidence(row)">{{ row.evidence_no }}</el-button>
        </template>
      </el-table-column>
      <el-table-column label="任务" min-width="180" show-overflow-tooltip>
        <template #default="{ row }">
          <span class="mono">{{ row.task_no }}</span>
          <div class="text-hint">{{ row.task_name }}</div>
        </template>
      </el-table-column>
      <el-table-column label="材料" min-width="150" show-overflow-tooltip>
        <template #default="{ row }">{{ row.material_name || "—" }}</template>
      </el-table-column>
      <el-table-column label="来源文件" min-width="180" show-overflow-tooltip>
        <template #default="{ row }">{{ row.filename || "—" }}</template>
      </el-table-column>
      <el-table-column label="页码" width="80">
        <template #default="{ row }">{{ row.page_no ? `第 ${row.page_no} 页` : "—" }}</template>
      </el-table-column>
      <el-table-column label="定位信息" min-width="180" show-overflow-tooltip>
        <template #default="{ row }">
          <span v-if="row.locator_note">{{ row.locator_note }}</span>
          <span v-else class="text-hint">无定位说明</span>
        </template>
      </el-table-column>
      <el-table-column label="引用文本" min-width="240" show-overflow-tooltip>
        <template #default="{ row }">{{ row.quote_text || "—" }}</template>
      </el-table-column>
      <el-table-column label="关联字段或规则" min-width="180" show-overflow-tooltip>
        <template #default="{ row }">{{ row.object_name || "—" }}</template>
      </el-table-column>
      <el-table-column label="依据类型" width="110">
        <template #default="{ row }">{{ referenceTypeText(row.reference_type) }}</template>
      </el-table-column>
      <el-table-column label="定位核对" width="130">
        <template #default="{ row }">
          <el-tag :type="toneTagType(locatorStatusTone(row.locator_status))" size="small" effect="light" disable-transitions>
            {{ locatorStatusLabel(row.locator_status) }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column label="版本" width="90">
        <template #default="{ row }">
          <span v-if="row.historical" class="text-hint">历史</span>
          <span v-else>当前</span>
        </template>
      </el-table-column>
      <el-table-column label="操作" width="140" fixed="right">
        <template #default="{ row }">
          <el-button link type="primary" size="small" @click.stop="openEvidence(row)">查看证据</el-button>
          <el-button link type="primary" size="small" @click.stop="openTask(row)">查看任务</el-button>
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

    <EvidenceDrawer
      :open="evidenceOpen"
      :material-name="activeRow?.material_name ?? ''"
      :field-name="activeRow?.object_name ?? ''"
      :evidence="evidence"
      :loading="evidenceLoading"
      :error-message="evidenceError"
      @close="evidenceOpen = false"
    />
  </div>
</template>
