<script setup lang="ts">
import { computed } from "vue";

import { statusLabel, statusTagType, toneTagType, type StatusTone } from "@/constants/labels";

/**
 * 状态标签。颜色与中文口径统一来自 constants/labels.ts，
 * 页面只传状态码，不自己判断颜色，也不直接显示英文状态值。
 */
const props = withDefaults(
  defineProps<{
    status: string | null | undefined;
    /** 同一编码在不同语境下有不同中文口径时（例如候选的 PENDING 是「待补充」）显式传入。 */
    label?: string | null;
    /** 候选、节点等有自己的配色表时显式传入。 */
    tone?: StatusTone | null;
    size?: "large" | "default" | "small";
    effect?: "dark" | "light" | "plain";
  }>(),
  { label: null, tone: null, size: "small", effect: "light" },
);

const tagType = computed(() => (props.tone ? toneTagType(props.tone) : statusTagType(props.status)));
const text = computed(() => props.label ?? statusLabel(props.status));
</script>

<template>
  <el-tag :type="tagType" :size="size" :effect="effect" disable-transitions>{{ text }}</el-tag>
</template>
