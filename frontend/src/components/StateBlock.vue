<script setup lang="ts">
import { computed } from "vue";

/**
 * 统一状态块：加载 / 无数据 / 未开始 / 失败 / 被阻断 / 已完成。
 *
 * 只负责呈现，不做业务判断：显示哪一种状态由调用方按真实数据决定，
 * 请求失败必须走 error，不能用「暂无数据」掩盖。
 */
const props = withDefaults(
  defineProps<{
    kind: "loading" | "empty" | "blocked" | "error" | "success";
    title?: string;
    description?: string;
    /** 技术细节：错误码、编号等，只在这里出现。 */
    details?: Array<{ label: string; value: string }>;
    /** 紧凑模式，用于卡片内部或小区域。 */
    compact?: boolean;
  }>(),
  { title: "", description: "", details: () => [], compact: false },
);

const resultIcon = computed(() => {
  if (props.kind === "error") {
    return "error";
  }
  if (props.kind === "blocked") {
    return "warning";
  }
  return "success";
});
</script>

<template>
  <div class="state-block" :class="{ 'state-block--compact': compact }">
    <!-- 加载中：骨架屏，避免把「还没读到」显示成空数据 -->
    <el-skeleton v-if="kind === 'loading'" :rows="compact ? 2 : 4" animated />

    <template v-else-if="kind === 'empty'">
      <el-empty :description="title || '暂无数据'" :image-size="compact ? 60 : 100">
        <p v-if="description" class="text-muted">{{ description }}</p>
      </el-empty>
    </template>

    <template v-else>
      <el-result :icon="resultIcon" :title="title" :sub-title="description">
        <template v-if="details.length" #extra>
          <el-descriptions :column="1" size="small" border>
            <el-descriptions-item v-for="detail in details" :key="detail.label" :label="detail.label">
              <span class="mono">{{ detail.value }}</span>
            </el-descriptions-item>
          </el-descriptions>
        </template>
      </el-result>
    </template>

    <div v-if="$slots.default" class="state-block__extra">
      <slot />
    </div>
  </div>
</template>

<style scoped>
.state-block__extra {
  margin-top: 12px;
}

.state-block :deep(.el-result) {
  padding: 16px 0;
}

/* 紧凑模式：卡片内部的小区域，减少上下留白 */
.state-block--compact :deep(.el-empty),
.state-block--compact :deep(.el-result) {
  padding: 8px 0;
}
</style>
