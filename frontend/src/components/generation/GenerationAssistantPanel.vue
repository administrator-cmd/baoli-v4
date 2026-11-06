<script setup lang="ts">
import { computed, nextTick, onUnmounted, ref, watch } from "vue";
import type { GenerationMessage, GenerationRevision } from "@/api/contracts";
import { listGenerationMessages, listGenerationRevisions } from "@/api/rulebookGenerations";
import { formatDate } from "@/constants/labels";
import { generationAssistantFailureMessage } from "@/utils/generationDocument";

const props = defineProps<{
  jobNo: string; revision: number; refreshToken: number; modelValue: string;
  disabled?: boolean; busy?: boolean;
}>();
const emit = defineEmits<{
  "update:modelValue": [value: string]; send: [message: string, issueId?: string];
  download: [revision: number]; restore: [revision: number];
}>();
const messages = ref<GenerationMessage[]>([]);
const revisions = ref<GenerationRevision[]>([]);
const beforeId = ref<number | null>(null);
const historyStarted = ref(false);
const messageBox = ref<HTMLElement | null>(null);
const reading = ref(false);
const error = ref("");
const activePanel = ref("chat");
let sequence = 0;
const answered = computed(() => new Set(messages.value.filter(item => item.role === "ASSISTANT").map(item => item.operation.operation_no)));
const originLabels: Record<string, string> = { INITIAL: "首次编制", MANUAL: "人工修改", AI: "AI 修改", RESTORE: "历史恢复", BASELINE: "已保存版本" };

async function load(older = false): Promise<void> {
  if (older && (reading.value || !beforeId.value)) return;
  const token = ++sequence;
  const target = props.jobNo;
  reading.value = true;
  error.value = "";
  const results = await Promise.allSettled([
    listGenerationMessages(target, older ? beforeId.value ?? undefined : undefined),
    listGenerationRevisions(target),
  ]);
  if (token !== sequence || target !== props.jobNo) return;
  const [chat, history] = results;
  if (chat.status === "fulfilled") {
    const previousLatest = messages.value[messages.value.length - 1]?.id;
    const initial = !historyStarted.value;
    const oldHeight = messageBox.value?.scrollHeight ?? 0;
    const oldTop = messageBox.value?.scrollTop ?? 0;
    const merged = new Map(messages.value.map(item => [item.id, item]));
    chat.value.items.forEach(item => merged.set(item.id, item));
    messages.value = [...merged.values()].sort((a, b) => a.id - b.id);
    if (older || !historyStarted.value) beforeId.value = chat.value.next_before_id;
    historyStarted.value = true;
    await nextTick();
    if (token !== sequence || target !== props.jobNo) return;
    if (messageBox.value) {
      if (older) messageBox.value.scrollTop = oldTop + messageBox.value.scrollHeight - oldHeight;
      else if (initial || previousLatest !== messages.value[messages.value.length - 1]?.id) messageBox.value.scrollTop = messageBox.value.scrollHeight;
    }
  } else error.value = chat.reason instanceof Error ? chat.reason.message : "对话读取失败";
  if (history.status === "fulfilled") revisions.value = history.value;
  else error.value = history.reason instanceof Error ? history.reason.message : "版本读取失败";
  reading.value = false;
}
watch(() => props.jobNo, () => {
  sequence += 1;
  messages.value = []; revisions.value = []; beforeId.value = null; historyStarted.value = false;
}, { flush: "sync" });
watch(() => [props.jobNo, props.refreshToken], () => { void load(); }, { immediate: true });
onUnmounted(() => { sequence += 1; });
watch(activePanel, async (panel) => {
  if (panel !== "chat") return;
  await nextTick();
  if (messageBox.value) messageBox.value.scrollTop = messageBox.value.scrollHeight;
});

function send(value = props.modelValue, issueId?: string): void {
  if (props.disabled || props.busy || !value.trim()) return;
  emit("send", value.trim(), issueId);
}
</script>

<template>
  <aside class="generation-assistant" aria-label="AI 编制助手">
    <div class="generation-assistant__heading"><span>当前第 {{ revision }} 版</span><span v-if="busy" role="status">AI 正在处理，关闭窗口后仍会继续</span></div>
    <el-alert v-if="error" :title="error" type="error" :closable="false" show-icon>
      <el-button link :loading="reading" @click="load()">重新读取</el-button>
    </el-alert>
    <el-tabs v-model="activePanel" class="generation-assistant__tabs">
    <el-tab-pane label="对话修改" name="chat" class="generation-assistant__chat">
    <div ref="messageBox" class="generation-assistant__messages" aria-live="polite" :aria-busy="busy">
      <el-button v-if="beforeId" link :loading="reading" @click="load(true)">查看更早对话</el-button>
      <p v-if="!messages.length && !reading" class="text-hint">直接说想改哪里，AI 会修改当前规则库。也可以询问某条内容的含义。</p>
      <article v-for="item in messages" :key="item.id" class="generation-assistant__message" :class="{ 'generation-assistant__message--user': item.role === 'USER' }">
        <small>{{ item.role === 'USER' ? '你的要求' : 'AI' }} · 第 {{ item.revision }} 版</small>
        <p>{{ item.content }}</p>
        <template v-if="item.role === 'USER' && !answered.has(item.operation.operation_no)">
          <small v-if="['PENDING', 'RUNNING'].includes(item.operation.status)">正在处理，离开页面后仍会继续</small>
          <template v-else-if="['FAILED', 'INTERRUPTED'].includes(item.operation.status)">
            <p class="generation-assistant__failure">{{ generationAssistantFailureMessage(item.operation) || item.operation.failure_message || '本次处理未完成' }}</p>
            <el-button link type="primary" :disabled="disabled || busy" @click="send(item.answer ?? item.content, item.issue_id ?? undefined)">重试这条要求</el-button>
          </template>
        </template>
      </article>
    </div>
    <div class="generation-assistant__input">
      <el-input :model-value="modelValue" type="textarea" :autosize="{ minRows: 3, maxRows: 7 }" maxlength="6000" placeholder="例如：按材料分组，保留原编号" aria-label="告诉 AI 希望修改的内容" :disabled="disabled || busy" @update:model-value="emit('update:modelValue', $event)" @keydown.ctrl.enter.prevent="send()" />
      <el-button type="primary" :loading="busy" :disabled="disabled || !modelValue.trim()" @click="send()">发送要求</el-button>
    </div>
    </el-tab-pane>
    <el-tab-pane :label="`版本记录（${revisions.length}）`" name="versions" class="generation-assistant__history">
        <p v-if="!revisions.length && !reading" class="text-hint">暂无版本记录</p>
        <div v-for="item in revisions" :key="item.revision" class="generation-assistant__revision">
          <div><strong>第 {{ item.revision }} 版</strong> · {{ originLabels[item.origin] }}<span v-if="item.revision === revision"> · 当前</span></div>
          <p>{{ item.summary }}</p><small>{{ formatDate(item.created_at) }}</small>
          <div><el-button link type="primary" :disabled="disabled || busy" @click="emit('download', item.revision)">下载 Excel</el-button><el-button v-if="item.revision !== revision" link :disabled="disabled || busy" @click="emit('restore', item.revision)">恢复此版</el-button></div>
        </div>
    </el-tab-pane>
    </el-tabs>
  </aside>
</template>

<style scoped>
.generation-assistant { display: flex; flex: 1; flex-direction: column; min-width: 0; min-height: 0; }
.generation-assistant__heading { display: flex; justify-content: space-between; flex-wrap: wrap; gap: 8px 16px; align-items: center; margin-bottom: 8px; }
.generation-assistant__heading span, .generation-assistant small { font-size: 12px; color: var(--el-text-color-secondary); }
.generation-assistant__tabs { display: flex; flex: 1; flex-direction: column; min-height: 0; }
.generation-assistant__tabs :deep(.el-tabs__header) { flex-shrink: 0; }
.generation-assistant__tabs :deep(.el-tabs__content) { flex: 1; min-height: 0; }
.generation-assistant__chat { display: flex; flex-direction: column; height: 100%; }
.generation-assistant__messages { flex: 1; min-height: 0; overflow-y: auto; overflow-wrap: anywhere; padding-right: 8px; }
.generation-assistant__message { padding: 14px 18px; margin: 0 0 14px; border-left: 2px solid var(--el-color-primary-light-5); background: var(--el-fill-color-light); }
.generation-assistant__message--user { border-left-color: var(--el-border-color); background: var(--el-fill-color-lighter); }
.generation-assistant__message p, .generation-assistant__revision p { white-space: pre-wrap; margin: 8px 0; line-height: 1.8; }
.generation-assistant__failure { color: var(--el-color-danger); }
.generation-assistant__input { display: grid; flex-shrink: 0; gap: 10px; margin-top: 14px; padding-top: 14px; border-top: 1px solid var(--el-border-color-lighter); }
.generation-assistant__input > .el-button { justify-self: end; }
.generation-assistant__history { height: 100%; overflow-y: auto; overflow-wrap: anywhere; padding-right: 8px; }
.generation-assistant__revision { padding: 10px 0; border-bottom: 1px solid var(--el-border-color-lighter); }
</style>
