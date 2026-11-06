<script setup lang="ts">
import type { ProposalSupportingContent } from "@/api/contracts";
import PlanConfigList from "@/components/plans/PlanConfigList.vue";
import { sourceSheetLabel } from "@/composables/useRuleSourceFilter";
import { formatBusinessValue } from "@/constants/labels";

const props = defineProps<{
  items: ProposalSupportingContent[];
  targetLabel: (target: ProposalSupportingContent["targets"][number]) => string;
}>();
const emit = defineEmits<{ select: [target: ProposalSupportingContent["targets"][number]] }>();
const kindLabels: Record<ProposalSupportingContent["kind"], string> = {
  MATERIAL_SCOPE: "材料及适用说明", DICTIONARY: "业务字典", MAPPING: "字段与规则关联", EXPLANATION: "补充说明",
};
function statusText(item: ProposalSupportingContent): string {
  if (item.unresolved_references.length) return "存在未明确关系";
  return item.targets.length ? "已记录归属" : "独立参考资料";
}
function searchText(item: ProposalSupportingContent): string {
  return [item.title, item.content, item.reason, ...item.unresolved_references,
    ...item.targets.flatMap((target) => [props.targetLabel(target), target.usage])].join(" ");
}
</script>

<template>
  <div class="support-panel">
    <p class="text-hint support-panel__hint">这些资料用于理解材料要求、业务术语和对象关联，随解析配置保留。展开可核对用途与原文，点击关联对象可查看其实际要求。</p>
    <PlanConfigList
      :items="items" row-key="support_ref" :number-of="(item: ProposalSupportingContent) => item.title"
      :search-text="searchText" :status-text="statusText" group-label="资料类型"
      :group-text="(item: ProposalSupportingContent) => kindLabels[item.kind]"
      keyword-placeholder="资料名称、业务内容或关联对象" empty-text="本次解析未识别到辅助资料"
    >
      <el-table-column type="expand">
        <template #default="{ row }">
          <div class="support-panel__detail">
            <h4>业务内容</h4><p>{{ row.content }}</p>
            <h4>识别与使用说明</h4><p>{{ row.reason }}</p>
            <template v-if="row.unresolved_references.length">
              <h4>尚未明确的关系</h4>
              <p v-for="(reference, index) in row.unresolved_references" :key="index">{{ reference }}</p>
            </template>
            <h4>原文依据</h4>
            <div v-for="(source, index) in row.origin.rows" :key="index" class="support-panel__source">
              <strong>《{{ source.sheet_name }}》第 {{ source.row_no }} 行</strong>
              <p v-for="(value, cell) in source.cells" :key="cell">{{ cell }}：{{ formatBusinessValue(value) }}</p>
            </div>
          </div>
        </template>
      </el-table-column>
      <el-table-column prop="title" label="辅助资料" min-width="180" show-overflow-tooltip />
      <el-table-column label="资料类型" width="150"><template #default="{ row }">{{ kindLabels[row.kind as ProposalSupportingContent['kind']] }}</template></el-table-column>
      <el-table-column prop="content" label="业务内容" min-width="250" show-overflow-tooltip />
      <el-table-column label="补充给哪些对象" min-width="310">
        <template #default="{ row }">
          <div v-for="(target, index) in row.targets" :key="index" class="support-panel__target">
            <el-button link type="primary" @click="emit('select', target)">{{ targetLabel(target) }}</el-button>
            <span>{{ target.usage }}</span>
          </div>
          <span v-if="!row.targets.length" class="text-hint">独立保留供参考，未指定对象</span>
        </template>
      </el-table-column>
      <el-table-column label="来源" width="210" show-overflow-tooltip><template #default="{ row }">{{ sourceSheetLabel(row.origin) }} · 第 {{ row.origin.row_no }} 行</template></el-table-column>
      <el-table-column label="处理情况" width="150"><template #default="{ row }"><el-tag :type="row.unresolved_references.length ? 'warning' : 'info'">{{ statusText(row) }}</el-tag></template></el-table-column>
    </PlanConfigList>
  </div>
</template>

<style scoped>
.support-panel__hint { margin: 0 0 16px; line-height: 1.7; }
.support-panel__target { display: flex; flex-direction: column; align-items: flex-start; gap: 4px; margin: 6px 0 12px; }
.support-panel__target span { white-space: pre-wrap; overflow-wrap: anywhere; line-height: 1.6; }
.support-panel__target :deep(.el-button) { white-space: normal; height: auto; text-align: left; line-height: 1.6; }
.support-panel__detail { padding: 8px 24px 20px; }
.support-panel__detail h4 { margin: 16px 0 8px; }
.support-panel__detail p { margin: 4px 0; white-space: pre-wrap; overflow-wrap: anywhere; line-height: 1.7; }
.support-panel__source { margin-top: 12px; }
@media (max-width: 640px) { .support-panel__detail { padding: 8px 12px 16px; } }
</style>
