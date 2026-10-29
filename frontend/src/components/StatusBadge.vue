<script setup lang="ts">
import { computed } from "vue";

import { statusLabel, statusTagType, toneTagType, type StatusTone } from "@/constants/labels";
import { statusNoteOf, statusTextOf, statusToneOf, type StatusDomain } from "@/constants/statusVocabulary";

/**
 * 状态标签。审核状态必须传 domain，文案、说明与颜色从后端词表按语境取；
 * 其他业务状态仍可用 label/tone 显式覆盖。
 */
const props = withDefaults(
  defineProps<{
    status: string | null | undefined;
    /** 审核状态语境；传了它就不再用通用状态表解释文案。 */
    domain?: StatusDomain | null;
    /** 非审核状态或确有业务特例时的显式文案。 */
    label?: string | null;
    /** 非审核状态或确有业务特例时的显式配色。 */
    tone?: StatusTone | null;
    size?: "large" | "default" | "small";
    effect?: "dark" | "light" | "plain";
  }>(),
  { domain: null, label: null, tone: null, size: "small", effect: "light" },
);

const vocabularyTone = computed(() => (
  props.domain ? statusToneOf(props.domain, props.status) : null
));
const tagType = computed(() => {
  const tone = props.tone ?? vocabularyTone.value;
  return tone ? toneTagType(tone) : statusTagType(props.status);
});
const text = computed(() => (
  props.label
  ?? (props.domain ? statusTextOf(props.domain, props.status) : statusLabel(props.status))
));
const note = computed(() => (
  props.domain ? statusNoteOf(props.domain, props.status) : null
));
</script>

<template>
  <el-tag
    :type="tagType"
    :size="size"
    :effect="effect"
    :title="note ?? undefined"
    disable-transitions
  >
    {{ text }}
  </el-tag>
</template>
