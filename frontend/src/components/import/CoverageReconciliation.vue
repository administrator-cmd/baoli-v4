<script setup lang="ts">
import type { ImportJob } from "@/api/contracts";
import StateBlock from "@/components/StateBlock.vue";
import StatusBadge from "@/components/StatusBadge.vue";
import { dispositionLabel, dispositionTone } from "@/constants/labels";

/**
 * 逐业务行处置对账。
 *
 * 每条来源业务行必须有处置：候选 / 停用保留 / 明确排除 / 待处理问题。
 * 服务端没返回 coverage 时明确说明，不能假装转换完整。
 */
type Coverage = NonNullable<ImportJob["coverage"]>;

defineProps<{
  coverage: Coverage | null;
}>();

/** 需要人工关注的行排前面。 */
function sortedRows(rows: Coverage["sheets"][number]["rows"]) {
  const weights: Record<string, number> = { QUESTION: 0, DISABLED: 1, EXCLUDED: 2, CANDIDATE: 3 };
  const weight = (disposition: string) => weights[disposition] ?? 4;
  return [...rows].sort((left, right) => weight(left.disposition) - weight(right.disposition) || left.row_no - right.row_no);
}

const totalLabels: Record<string, string> = {
  business_rows: "业务行",
  candidate_rows: "候选行",
  disabled_rows: "停用保留",
  excluded_rows: "已排除",
  question_rows: "待处理",
};

function totalsText(totals: Record<string, number>): string {
  const entries = Object.entries(totals);
  if (!entries.length) {
    return "";
  }
  return entries.map(([key, value]) => `${totalLabels[key] ?? key} ${value}`).join(" · ");
}
</script>

<template>
  <StateBlock
    v-if="!coverage"
    kind="empty"
    compact
    title="服务端未提供逐行对账结果"
    description="无法确认每一条来源业务行是否都已处置；这不代表转换完整，请重新解析或核对源文件。"
  />

  <div v-else class="coverage">
    <p class="text-muted">
      共 {{ coverage.sheets.length }} 张业务表；总计：
      {{ totalsText(coverage.totals) || "没有可汇总的处置统计" }}。
    </p>
    <section v-if="coverage.notices?.length" class="coverage__notices">
      <h3>转换说明（{{ coverage.notices.length }}）</h3>
      <ul>
        <li v-for="(notice, index) in coverage.notices" :key="`${notice.code}-${notice.sheet_name}-${notice.row_no}-${index}`">
          <strong>{{ notice.sheet_name }} · 第 {{ notice.row_no }} 行：</strong>{{ notice.message }}
        </li>
      </ul>
    </section>

    <el-collapse>
      <el-collapse-item
        v-for="sheet in coverage.sheets"
        :key="sheet.sheet_key"
        :name="sheet.sheet_key"
        :title="`${sheet.sheet_name} · 业务行 ${sheet.business_rows} 行`"
      >
        <el-table :data="sortedRows(sheet.rows)" border size="small" max-height="320">
          <el-table-column label="行号" prop="row_no" width="80" />
          <el-table-column label="处置" width="130">
            <template #default="{ row }">
              <StatusBadge
                :status="row.disposition"
                :label="dispositionLabel(row.disposition)"
                :tone="dispositionTone(row.disposition)"
              />
            </template>
          </el-table-column>
          <el-table-column label="原因" min-width="200" show-overflow-tooltip>
            <template #default="{ row }">{{ row.reason || "未提供原因" }}</template>
          </el-table-column>
          <el-table-column label="候选 / 问题" width="140">
            <template #default="{ row }">{{ row.candidate_count }} / {{ row.question_count }}</template>
          </el-table-column>
          <el-table-column label="合并去向" min-width="140" show-overflow-tooltip>
            <template #default="{ row }">{{ row.merged_into || "—" }}</template>
          </el-table-column>
          <el-table-column label="备注" min-width="160" show-overflow-tooltip>
            <template #default="{ row }">{{ row.notes?.length ? row.notes.join("；") : "—" }}</template>
          </el-table-column>
        </el-table>
      </el-collapse-item>
    </el-collapse>
  </div>
</template>

<style scoped>
.coverage {
  width: 100%;
}

.coverage__notices {
  margin: 16px 0;
}

.coverage__notices h3 {
  margin: 0 0 8px;
  font-size: 14px;
}

.coverage__notices ul {
  margin: 0;
  padding-left: 20px;
}

.coverage__notices li {
  margin: 6px 0;
  overflow-wrap: anywhere;
}
</style>
