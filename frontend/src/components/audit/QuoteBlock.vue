<script setup lang="ts">
import { computed } from "vue";

import { quoteBlockText, quoteTableRows } from "@/utils/quoteText";

/**
 * 引用原文。
 *
 * OCR 的 PDF 转 Markdown 结果里，金额、日期等常以表格出现；模型引用时会把
 * HTML 表格片段原样带回。这里按行列还原成表格，其余内容仍按普通文本展示，
 * 已保存的原文不做任何改写。
 */
const props = defineProps<{ text?: string | null }>();

/** 单格「表格」没有行列结构可还原，按普通文本展示，否则只剩一个无边框的居中单元格。 */
const rows = computed(() => {
  const parsed = quoteTableRows(props.text);
  return parsed && parsed.length === 1 && parsed[0].length === 1 ? null : parsed;
});
const header = computed(() => (rows.value && rows.value.length > 1 ? rows.value[0] : null));
const body = computed(() => (rows.value ? (header.value ? rows.value.slice(1) : rows.value) : []));
const plain = computed(() => quoteBlockText(props.text));
</script>

<template>
  <table v-if="rows" class="quote-table">
    <thead v-if="header">
      <tr>
        <th v-for="(cell, index) in header" :key="index">{{ cell }}</th>
      </tr>
    </thead>
    <tbody>
      <tr v-for="(row, rowIndex) in body" :key="rowIndex">
        <td v-for="(cell, cellIndex) in row" :key="cellIndex">{{ cell }}</td>
      </tr>
    </tbody>
  </table>
  <blockquote v-else class="evidence-quote">{{ plain || "本条原文为空" }}</blockquote>
</template>

<style scoped>
.evidence-quote {
  margin: 0;
  padding: 12px 14px;
  color: var(--el-text-color-primary);
  font-size: 14px;
  line-height: 1.75;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}

.quote-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 14px;
  line-height: 1.6;
}

.quote-table th,
.quote-table td {
  padding: 8px 10px;
  border-right: 1px solid var(--el-border-color-lighter);
  border-bottom: 1px solid var(--el-border-color-lighter);
  overflow-wrap: anywhere;
  text-align: center;
}

.quote-table th {
  background: var(--el-fill-color-lighter);
  color: var(--el-text-color-regular);
  font-weight: 600;
}

.quote-table th:last-child,
.quote-table td:last-child {
  border-right: 0;
}

.quote-table tbody tr:last-child td {
  border-bottom: 0;
}
</style>
