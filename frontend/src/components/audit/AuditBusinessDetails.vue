<script setup lang="ts">
import { computed } from "vue";
import type { JsonValue } from "@/api/contracts";
import { knownStatusLabel } from "@/constants/statusVocabulary";

const props = defineProps<{
  result: Record<string, JsonValue> | null;
  sourceFiles: string[];
  materialNames: string[];
}>();
const basis = computed(() => (props.result?.basis ?? []) as Array<{ label: string; value: JsonValue; explanation: string }>);
const files = computed(() => [...new Set(props.sourceFiles.filter(Boolean))]);
const requirements = computed(() => [...new Set(props.materialNames.filter((name) => name && !files.value.includes(name)))]);
/** 仅标注明确匹配的配置名称，保留原说明，不猜测每行与文件的对应关系。 */
function explanationParts(text: string): Array<{ text: string; requirement: boolean }> {
  text = text.replace(/\b[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+\b/g, (code) =>
    knownStatusLabel("field", code) ?? knownStatusLabel("rule", code) ?? (code === "NOT_PRESENT" ? "未抽取到" : code));
  if (!requirements.value.length) return [{ text, requirement: false }];
  // 先匹配完整文件名，避免材料要求名称恰好是文件名的一部分时把文件名拆开标注。
  const names = [...files.value, ...requirements.value].sort((left, right) => right.length - left.length);
  const pattern = new RegExp(`(${names.map((name) => name.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")).join("|")})`, "g");
  return text.split(pattern).filter(Boolean).map((part) => ({ text: part, requirement: requirements.value.includes(part) }));
}
const missing = computed(() => (props.result?.missing_items ?? []) as string[]);
function displayValue(value: JsonValue): string {
  if (value === null) return "未取得值";
  if (typeof value === "boolean") return value ? "是" : "否";
  if (typeof value === "object") return JSON.stringify(value, null, 2);
  if (typeof value === "string") return knownStatusLabel("rule", value) ?? value;
  return String(value);
}
</script>

<template>
  <div v-if="basis.length || missing.length">
    <template v-if="basis.length">
      <h4 class="app-section-title">判断依据</h4>
      <div class="basis-sources">
        <span class="text-muted">本条引用文件：</span>
        <span>{{ files.join('、') || '未记录来源文件' }}</span>
      </div>
      <el-table :data="basis" size="small" border>
        <el-table-column prop="label" label="核查项" min-width="120" />
        <el-table-column label="实际取值 / 结果" min-width="150">
          <template #default="{ row }"><div class="basis-value">{{ displayValue(row.value) }}</div></template>
        </el-table-column>
        <el-table-column label="依据说明" min-width="260" class-name="basis-explanation">
          <template #default="{ row }">
            <template v-for="(part, index) in explanationParts(row.explanation)" :key="index">
              <span v-if="part.requirement" class="basis-requirement">【材料要求：{{ part.text }}】</span>
              <template v-else>{{ part.text }}</template>
            </template>
          </template>
        </el-table-column>
      </el-table>
    </template>
    <el-alert v-if="missing.length" class="block-gap" title="缺失信息与未确认事项" type="warning" :closable="false" show-icon>
      <ul class="missing-list"><li v-for="item in missing" :key="item">{{ item }}</li></ul>
    </el-alert>
  </div>
</template>

<style scoped>
.basis-value { white-space: pre-wrap; overflow-wrap: anywhere; }
.basis-sources { margin: 0 0 10px; overflow-wrap: anywhere; }
.basis-requirement { color: var(--el-text-color-secondary); }
.missing-list { margin: 0; padding-left: 18px; }
/* 依据说明是成段文字，居中会让每行起点参差，这里单独左对齐；取值仍居中。 */
:deep(.basis-explanation .cell) { text-align: left; }
</style>
