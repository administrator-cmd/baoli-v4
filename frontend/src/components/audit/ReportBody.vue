<script setup lang="ts">
import { computed } from "vue";
import MarkdownIt from "markdown-it";
import DOMPurify from "dompurify";

const props = defineProps<{ content: string; contentType: string }>();
const markdown = new MarkdownIt({ html: true, linkify: false, breaks: true });
// 模型正文与 OCR 引文都属于外部内容；允许排版，移除可执行 HTML。
const rendered = computed(() => DOMPurify.sanitize(
  props.contentType === "text/markdown" ? markdown.render(props.content) : props.content,
  { USE_PROFILES: { html: true } },
));
</script>

<template>
  <div v-if="contentType === 'text/plain'" class="report-body plain">{{ content }}</div>
  <div v-else class="report-body" v-html="rendered" />
</template>

<style scoped>
.report-body { color: var(--el-text-color-regular); line-height: 1.8; overflow-wrap: anywhere; overflow-x: auto; }
.plain { white-space: pre-wrap; }
.report-body :deep(h1), .report-body :deep(h2), .report-body :deep(h3) { margin: 1.2em 0 .6em; line-height: 1.4; }
.report-body :deep(h1) { font-size: 23px; }
.report-body :deep(h2) { font-size: 19px; }
.report-body :deep(h3) { font-size: 16px; }
.report-body :deep(table) { border-collapse: collapse; width: 100%; margin: 14px 0; font-size: 13px; }
.report-body :deep(th), .report-body :deep(td) { border: 1px solid var(--el-border-color); padding: 8px 12px; text-align: left; }
.report-body :deep(th) { background: var(--el-fill-color-light); }
.report-body :deep(blockquote) { margin: 12px 0; padding-left: 14px; border-left: 3px solid var(--el-border-color); }
.report-body :deep(pre) { overflow-x: auto; background: var(--el-fill-color-light); padding: 12px; }
</style>
