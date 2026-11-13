<script setup lang="ts">
import { computed } from "vue";
import type { ConfigOrigin, ProposalSupportingContent } from "@/api/contracts";
import { formatBusinessValue } from "@/constants/labels";

const props = defineProps<{
  origin?: ConfigOrigin | null;
  items: ProposalSupportingContent[];
}>();
const notes = computed(() => props.items.filter((item) => item.kind === "MATERIAL_SCOPE" || item.kind === "EXPLANATION"));
const unresolved = computed(() => [...new Set(props.items.flatMap((item) => item.unresolved_references))]);
const sources = computed(() => {
  const rows = [...(props.origin?.rows ?? []), ...props.items.flatMap((item) => item.origin.rows)];
  return [...new Map(rows.map((row) => [JSON.stringify([row.sheet_name, row.row_no]), row])).values()];
});
</script>

<template>
  <el-collapse v-if="sources.length || notes.length || unresolved.length" class="proposal-sources">
    <el-collapse-item title="原文依据与补充说明" name="source">
      <section v-for="item in notes" :key="item.support_ref" class="proposal-sources__section">
        <h4>{{ item.title }}</h4>
        <p>{{ item.content }}</p>
      </section>
      <section v-if="unresolved.length" class="proposal-sources__section">
        <h4>原文中尚未明确的关联</h4>
        <p v-for="reference in unresolved" :key="reference">{{ reference }}</p>
      </section>
      <section v-for="source in sources" :key="JSON.stringify([source.sheet_name, source.row_no])" class="proposal-sources__section">
        <h4>《{{ source.sheet_name }}》第 {{ source.row_no }} 行</h4>
        <dl class="proposal-sources__cells">
          <div v-for="(value, cell) in source.cells" :key="cell">
            <dt>{{ cell }}</dt><dd>{{ formatBusinessValue(value) }}</dd>
          </div>
        </dl>
      </section>
    </el-collapse-item>
  </el-collapse>
</template>

<style scoped>
.proposal-sources { margin-top: 16px; }
.proposal-sources__section + .proposal-sources__section { margin-top: 16px; }
.proposal-sources__section h4 { margin: 0 0 8px; font-size: 14px; overflow-wrap: anywhere; }
.proposal-sources__section p { margin: 4px 0; white-space: pre-wrap; overflow-wrap: anywhere; line-height: 1.7; }
.proposal-sources__cells { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 8px 16px; margin: 0; }
.proposal-sources__cells > div { display: flex; align-items: baseline; gap: 8px; min-width: 0; }
.proposal-sources__cells dt { color: var(--el-text-color-secondary); flex-shrink: 0; }
.proposal-sources__cells dd { min-width: 0; margin: 0; white-space: pre-wrap; overflow-wrap: anywhere; line-height: 1.7; }
@media (max-width: 640px) { .proposal-sources__cells { grid-template-columns: minmax(0, 1fr); } }
</style>
