<script setup lang="ts">
import { computed } from "vue";
import { FileSearch } from "lucide-vue-next";

import type { ImportIssue, JsonValue } from "@/api/contracts";
import StatusBadge from "@/components/StatusBadge.vue";
import { formatBusinessValue } from "@/constants/labels";

/**
 * 一条需要业务判断的问题。
 *
 * 页面只让用户做业务选择或填业务数值，不出现 JSON、内部 key 或编码。
 * 选项数量超过 1 才真正需要判断；只有唯一选项时在界面上说明「无其他可选」。
 */
const props = defineProps<{
  issue: ImportIssue;
  selected: JsonValue | undefined;
}>();

const emit = defineEmits<{
  "update:selected": [value: JsonValue];
}>();

const inputKind = computed(() => props.issue.input_kind ?? "SELECT");
/** 只有一个选项时用户没有真正的选择空间，如实说明而不是假装在选。 */
const isDeterminate = computed(() => inputKind.value === "SELECT" && props.issue.options.length <= 1);
const isBlocking = computed(() => props.issue.severity === "BLOCKER");

const sourceText = computed(() => {
  const source = props.issue.source;
  const parts = [
    source.sheet_name ?? "未知工作表",
    source.row_no ? `第 ${source.row_no} 行` : "",
    source.cells?.length ? source.cells.join("、") : "",
  ];
  return parts.filter(Boolean).join(" · ");
});

const targetText = computed(() => {
  const target = props.issue.source.target_type;
  if (target === "MATERIAL") {
    return "材料";
  }
  if (target === "FIELD") {
    return "字段";
  }
  if (target === "RULE") {
    return "规则";
  }
  if (target === "REPORT_SECTION") {
    return "报告章节";
  }
  return "业务对象";
});

/** 已保存答案的形状是 {selected: ...}；展示时只取其中的业务值。 */
const savedAnswer = computed(() => {
  const decision = props.issue.decision;
  if (decision && typeof decision === "object" && !Array.isArray(decision) && "selected" in (decision as Record<string, JsonValue>)) {
    return (decision as Record<string, JsonValue>).selected ?? null;
  }
  return decision ?? null;
});
</script>

<template>
  <el-card shadow="never" class="issue-card">
    <template #header>
      <div class="issue-card__header">
        <div>
          <div class="issue-card__title">{{ issue.title }}</div>
          <div class="text-muted block-gap-sm">{{ issue.description }}</div>
        </div>
        <StatusBadge
          :status="isBlocking ? 'BLOCKED' : issue.status"
          :label="isBlocking ? '阻断项' : undefined"
          size="default"
        />
      </div>
    </template>

    <div class="issue-card__source">
      <el-icon><FileSearch /></el-icon>
      <span>来源：{{ sourceText }}（{{ targetText }}）</span>
    </div>

    <el-alert
      v-if="issue.resolution_notice"
      type="info"
      :closable="false"
      :title="issue.resolution_notice"
      class="block-gap"
    />
    <el-form v-else label-position="top" class="block-gap">
      <el-form-item>
        <template #label>业务结论</template>

        <div v-if="inputKind === 'SELECT'" class="issue-card__options">
          <el-alert
            v-if="isDeterminate"
            type="info"
            show-icon
            :closable="false"
            title="该问题只有唯一的可选结论，请确认后提交"
            class="block-gap-sm"
          />
          <el-radio-group
            :model-value="selected as string"
            @update:model-value="emit('update:selected', $event)"
          >
            <el-radio v-for="(option, optionIndex) in issue.options" :key="`${issue.issue_no}-${optionIndex}`" :value="option.value as string">
              {{ option.label }}
            </el-radio>
          </el-radio-group>
        </div>

        <el-input
          v-else-if="inputKind === 'DECIMAL'"
          :model-value="(selected as string) ?? ''"
          type="number"
          placeholder="请填写业务数值"
          class="issue-card__input"
          @update:model-value="emit('update:selected', $event)"
        >
          <template #prepend>业务数值</template>
          <template #append>
            <el-tooltip content="系统不会使用默认值，必须由业务确认" placement="top">
              <span>?</span>
            </el-tooltip>
          </template>
        </el-input>

        <el-input
          v-else
          :model-value="(selected as string) ?? ''"
          type="textarea"
          :rows="2"
          maxlength="500"
          show-word-limit
          placeholder="请填写业务说明（用于替换占位内容）"
          class="issue-card__input"
          @update:model-value="emit('update:selected', $event)"
        />
      </el-form-item>
    </el-form>

    <el-collapse>
      <el-collapse-item title="查看技术信息" name="tech">
        <div class="mono">问题编号：{{ issue.issue_no }}</div>
        <div class="mono">问题分类：{{ issue.category }}</div>
        <div class="mono">严重级别：{{ issue.severity }}</div>
        <div v-if="savedAnswer !== null" class="mono">已保存结论：{{ formatBusinessValue(savedAnswer) }}</div>
      </el-collapse-item>
    </el-collapse>
  </el-card>
</template>

<style scoped>
.issue-card__header {
  display: flex;
  flex-wrap: wrap;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
}

.issue-card__title {
  font-size: 15px;
  font-weight: 600;
}

.issue-card__source {
  display: flex;
  align-items: center;
  gap: 6px;
  color: var(--el-color-primary);
  font-size: 13px;
}

.issue-card__options :deep(.el-radio) {
  display: flex;
  margin-right: 0;
  height: auto;
  white-space: normal;
}

.issue-card__options {
  display: flex;
  flex-direction: column;
  gap: 4px;
  width: 100%;
}

.issue-card__input {
  max-width: 420px;
}
</style>
