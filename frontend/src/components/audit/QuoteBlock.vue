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
const props = defineProps<{
  text?: string | null;
  /** 目标值对应的科目/行标签真实原文。 */
  label?: string | null;
  /** 目标值所在列/期间的表头真实原文。 */
  header?: string | null;
  /** 目标值对应的期间原文。 */
  period?: string | null;
  /** OCR 产物内页码；只表示产物位置。 */
  page?: number | null;
}>();

const hasContext = computed(() => Boolean(props.label || props.header || props.period || (props.page !== null && props.page !== undefined)));

/** 单格「表格」没有行列结构可还原，按普通文本展示，否则只剩一个无边框的居中单元格。 */
const rows = computed(() => {
  const parsed = quoteTableRows(props.text);
  return parsed && parsed.length === 1 && parsed[0].length === 1 ? null : parsed;
});
const plain = computed(() => quoteBlockText(props.text));
</script>

<template>
  <div class="quote-shell">
    <dl v-if="hasContext" class="quote-context">
      <template v-if="label"><dt>科目 / 行</dt><dd>{{ label }}</dd></template>
      <template v-if="header"><dt>表头 / 列</dt><dd>{{ header }}</dd></template>
      <template v-if="period"><dt>期间</dt><dd>{{ period }}</dd></template>
      <template v-if="page !== null && page !== undefined"><dt>产物位置</dt><dd>第 {{ page }} 页</dd></template>
    </dl>
    <div v-if="rows" class="quote-table-scroll" tabindex="0" aria-label="引用原文表格，可横向滚动">
      <table class="quote-table">
        <tbody>
          <tr v-for="(row, rowIndex) in rows" :key="rowIndex">
            <td v-for="(cell, cellIndex) in row" :key="cellIndex">{{ cell }}</td>
          </tr>
        </tbody>
      </table>
    </div>
    <blockquote v-else class="evidence-quote">{{ plain || "本条原文为空" }}</blockquote>
  </div>
</template>

<style scoped>
.quote-shell {
  display: flex;
  flex-direction: column;
  gap: 8px;
  min-width: 0;
}

.quote-context {
  display: grid;
  grid-template-columns: max-content minmax(0, 1fr);
  gap: 4px 10px;
  margin: 0;
  padding: 8px 12px;
  border-radius: 4px;
  background: var(--el-fill-color-lighter);
  color: var(--el-text-color-regular);
  font-size: 12px;
  line-height: 1.5;
}

.quote-context dt {
  color: var(--el-text-color-secondary);
}

.quote-context dd {
  margin: 0;
  overflow-wrap: anywhere;
}

.evidence-quote {
  margin: 0;
  padding: 12px 14px;
  color: var(--el-text-color-primary);
  font-size: 14px;
  line-height: 1.75;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}
.quote-table-scroll {
  overflow-x: auto;
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
