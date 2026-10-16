<script setup lang="ts">
import { computed } from "vue";

import type { RuleCheckComparison } from "@/api/contracts";

/**
 * 交叉校验的实际比较过程。
 *
 * 左侧材料 / 左侧字段 / 左侧原文 / 左侧标准化值与右侧逐一并排展示，中间是本次
 * 真实执行的比较关系（方式、允许差值、实际差额、算式）。这样读一条跨材料检查
 * 不必先打开抽屉找证据，双方业务内容直接可读。
 *
 * 全部取值来自服务端本次执行的对比记录（`comparison`）。服务端没有返回的项
 * 如实写「服务端未返回」并把它列进缺口提示，绝不用字段名或材料名去猜一个值，
 * 也不重新计算差额——页面算出来的数字与执行时的不一致时无法追查。
 */
const props = defineProps<{
  comparison: RuleCheckComparison;
  /** 这条检查的名称，用于说明缺口属于哪一条检查。 */
  ruleName: string;
}>();

/** 一侧的取值：标签（材料·字段）、执行参与比较的值、抽取到的原文。 */
interface SideValue {
  label: string | null;
  value: string | null;
  rawValue: string | null;
}

const left = computed<SideValue>(() => ({
  label: props.comparison.left_label ?? null,
  value: props.comparison.left_value ?? null,
  rawValue: props.comparison.left_raw_value ?? null,
}));

const right = computed<SideValue>(() => ({
  label: props.comparison.right_label ?? null,
  value: props.comparison.right_value ?? null,
  rawValue: props.comparison.right_raw_value ?? null,
}));

/**
 * 加总比较的加数：服务端给出 addends（标准化值）与 addend_labels（业务名），
 * 两者按下标一一对应。长度不一致时按较短的显示，缺的一项在缺口里说明。
 */
const addends = computed(() => {
  const values = props.comparison.addends ?? [];
  const labels = props.comparison.addend_labels ?? [];
  const size = Math.min(values.length, labels.length);
  return Array.from({ length: size }, (_, index) => ({
    label: labels[index] || `第 ${index + 1} 项`,
    value: values[index],
  }));
});

/** 服务端没有返回的展示项：明确列出，避免读者以为「本来就没有」。 */
const missingFields = computed(() => {
  const missing: string[] = [];
  if (!left.value.label) missing.push("左侧材料/字段");
  if (!right.value.label) missing.push("右侧材料/字段");
  if (left.value.value === null) missing.push("左侧标准化值");
  if (right.value.value === null) missing.push("右侧标准化值");
  if (!props.comparison.expression) missing.push("计算表达式");
  if (props.comparison.threshold === null || props.comparison.threshold === undefined) {
    missing.push("允许差值");
  }
  if (props.comparison.actual_diff === null || props.comparison.actual_diff === undefined) {
    missing.push("实际差额");
  }
  return missing;
});

/**
 * 原文与参与比较的值不同（例如原文带千分位与币种符号）：两者都要显示，
 * 只显示一个会让「系统拿什么在比」和「材料里写了什么」对不上。
 */
function rawDiffers(side: SideValue): boolean {
  return Boolean(side.rawValue) && side.rawValue !== side.value;
}
</script>

<template>
  <div class="cross-comparison">
    <div class="cross-comparison__grid">
      <!-- 左侧：材料 / 字段 / 原文 / 标准化值 -->
      <div class="cross-side">
        <div class="cross-side__head">左侧</div>
        <dl class="cross-side__list">
          <dt>材料 · 字段</dt>
          <dd>{{ left.label || "服务端未返回左右双方的来源" }}</dd>
          <dt>原文内容</dt>
          <dd class="cross-side__quote">
            <template v-if="rawDiffers(left)">{{ left.rawValue }}</template>
            <template v-else-if="left.rawValue">{{ left.rawValue }}</template>
            <span v-else class="text-muted">服务端未返回原文内容</span>
          </dd>
          <dt>标准化结果</dt>
          <dd class="cross-side__value">
            <template v-if="left.value !== null">{{ left.value }}</template>
            <span v-else class="text-muted">服务端未返回标准化结果</span>
          </dd>
        </dl>
      </div>

      <!-- 中间：本次真实执行的比较关系 -->
      <div class="cross-middle">
        <div class="cross-middle__head">比较关系</div>
        <dl class="cross-middle__list">
          <dt>判定方式</dt>
          <dd>{{ comparison.mode_label || "服务端未返回判定方式" }}</dd>
          <dt>允许差值</dt>
          <dd>
            <template v-if="comparison.threshold !== null && comparison.threshold !== undefined">
              {{ comparison.threshold }}
            </template>
            <span v-else class="text-muted">服务端未返回允许差值</span>
          </dd>
          <dt>实际差额</dt>
          <dd>
            <template v-if="comparison.actual_diff">{{ comparison.actual_diff }}</template>
            <span v-else class="text-muted">服务端未返回实际差额</span>
          </dd>
        </dl>
        <div class="cross-middle__expression">
          <template v-if="comparison.expression">{{ comparison.expression }}</template>
          <span v-else class="text-muted">服务端未返回计算表达式</span>
        </div>
      </div>

      <!-- 右侧：材料 / 字段 / 原文 / 标准化值 -->
      <div class="cross-side">
        <div class="cross-side__head">右侧</div>
        <dl class="cross-side__list">
          <dt>材料 · 字段</dt>
          <dd>{{ right.label || "服务端未返回左右双方的来源" }}</dd>
          <dt>原文内容</dt>
          <dd class="cross-side__quote">
            <template v-if="rawDiffers(right)">{{ right.rawValue }}</template>
            <template v-else-if="right.rawValue">{{ right.rawValue }}</template>
            <span v-else class="text-muted">服务端未返回原文内容</span>
          </dd>
          <dt>标准化结果</dt>
          <dd class="cross-side__value">
            <template v-if="right.value !== null">{{ right.value }}</template>
            <span v-else class="text-muted">服务端未返回标准化结果</span>
          </dd>
        </dl>
      </div>
    </div>

    <!-- 加总比较：左侧待核对总额对右侧加数合计；加数全部列出，不只显示前两项 -->
    <div v-if="addends.length" class="cross-addends">
      <div class="cross-addends__head">加数（{{ addends.length }} 项）</div>
      <ul class="cross-addends__list">
        <li v-for="(item, index) in addends" :key="index">
          {{ item.label }} = {{ item.value }}
        </li>
      </ul>
    </div>

    <div v-if="comparison.basis" class="cross-basis">判定依据：{{ comparison.basis }}</div>

    <el-alert
      v-if="missingFields.length"
      class="block-gap-sm"
      type="warning"
      show-icon
      :closable="false"
      :title="`「${ruleName}」有 ${missingFields.length} 项对比事实没有从服务端返回`"
    >
      <div>缺失项：{{ missingFields.join("、") }}。</div>
    </el-alert>

    <slot name="conclusion" />
  </div>
</template>

<style scoped>
.cross-comparison {
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.cross-comparison__grid {
  display: grid;
  grid-template-columns: minmax(0, 1fr) minmax(0, 0.9fr) minmax(0, 1fr);
  gap: 10px;
}

@media (max-width: 1024px) {
  .cross-comparison__grid {
    grid-template-columns: minmax(0, 1fr);
  }
}

.cross-side,
.cross-middle {
  border: 1px solid var(--el-border-color);
  border-radius: 4px;
  padding: 10px 12px;
  background: var(--el-fill-color-blank);
}

.cross-middle {
  background: var(--el-fill-color-light);
}

.cross-side__head,
.cross-middle__head {
  font-weight: 600;
  margin-bottom: 6px;
}

.cross-side__list,
.cross-middle__list {
  display: grid;
  grid-template-columns: auto minmax(0, 1fr);
  gap: 4px 10px;
  margin: 0;
}

.cross-side__list dt,
.cross-middle__list dt {
  color: var(--el-text-color-secondary);
  white-space: nowrap;
}

.cross-side__list dd,
.cross-middle__list dd {
  margin: 0;
  overflow-wrap: anywhere;
}

.cross-side__quote {
  white-space: pre-wrap;
  line-height: 1.7;
}

.cross-side__value {
  font-weight: 600;
}

.cross-middle__expression {
  margin-top: 8px;
  padding: 6px 8px;
  background: var(--el-fill-color);
  border-radius: 4px;
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 13px;
  overflow-wrap: anywhere;
}

.cross-addends__head {
  font-weight: 600;
  margin-bottom: 4px;
}

.cross-addends__list {
  margin: 0;
  padding-left: 18px;
  line-height: 1.8;
}

.cross-basis {
  color: var(--el-text-color-secondary);
}
</style>
