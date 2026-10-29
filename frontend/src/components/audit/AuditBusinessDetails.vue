<script setup lang="ts">
import { computed } from "vue";
import type { JsonValue } from "@/api/contracts";

const props = defineProps<{ result: Record<string, JsonValue> | null }>();
const basis = computed(() => (props.result?.basis ?? []) as Array<{ label: string; value: JsonValue; explanation: string }>);
const missing = computed(() => (props.result?.missing_items ?? []) as string[]);
const action = computed(() => props.result?.next_action as string | undefined);
function displayValue(value: JsonValue): string {
  if (value === null) return "未取得值";
  if (typeof value === "boolean") return value ? "是" : "否";
  if (typeof value === "object") return JSON.stringify(value, null, 2);
  return String(value);
}
</script>

<template>
  <div v-if="basis.length || missing.length || action">
    <template v-if="basis.length">
      <h4 class="app-section-title">判断依据</h4>
      <el-table :data="basis" size="small" border>
        <el-table-column prop="label" label="核查项" min-width="120" />
        <el-table-column label="实际取值 / 结果" min-width="150">
          <template #default="{ row }"><div class="basis-value">{{ displayValue(row.value) }}</div></template>
        </el-table-column>
        <el-table-column prop="explanation" label="依据说明" min-width="260" class-name="basis-explanation" />
      </el-table>
    </template>
    <el-alert v-if="missing.length" class="block-gap" title="仍需补充或确认的信息" type="warning" :closable="false" show-icon>
      <ul class="missing-list"><li v-for="item in missing" :key="item">{{ item }}</li></ul>
    </el-alert>
    <el-alert v-if="action" class="block-gap" title="处理建议" type="info" :closable="false" show-icon>{{ action }}</el-alert>
  </div>
</template>

<style scoped>
.basis-value { white-space: pre-wrap; overflow-wrap: anywhere; }
.missing-list { margin: 0; padding-left: 18px; }
/* 依据说明是成段文字，居中会让每行起点参差，这里单独左对齐；取值仍居中。 */
:deep(.basis-explanation .cell) { text-align: left; }
</style>
