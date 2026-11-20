<script setup lang="ts">
import { computed, ref, watch } from "vue";
import { useRouter } from "vue-router";

import { listTaskFieldReviewHistory, type FieldReviewHistoryItem } from "@/api/audits";
import BackButton from "@/components/BackButton.vue";
import PageHeader from "@/components/PageHeader.vue";
import StateBlock from "@/components/StateBlock.vue";
import { formatDate } from "@/constants/labels";
import { statusTextOf } from "@/constants/statusVocabulary";

const props = defineProps<{ taskNo: string }>();
const router = useRouter();
const records = ref<FieldReviewHistoryItem[]>([]);
const selectedConfirmationNo = ref("");
const loading = ref(true);
const loadError = ref<string | null>(null);
const reload = ref(0);

const selectedRecord = computed(() => records.value.find((item) => item.confirmation_no === selectedConfirmationNo.value));
const snapshotFields = computed(() => selectedRecord.value?.snapshot?.fields ?? []);
const snapshotMissing = computed(() => selectedRecord.value?.snapshot?.missing_fields ?? []);

watch([() => props.taskNo, reload], async ([taskNo], _, onCleanup) => {
  let stale = false;
  onCleanup(() => { stale = true; });
  records.value = [];
  selectedConfirmationNo.value = "";
  loading.value = true;
  loadError.value = null;
  try {
    const result = await listTaskFieldReviewHistory(taskNo);
    if (stale) return;
    records.value = result;
    selectedConfirmationNo.value = result[0]?.confirmation_no ?? "";
  } catch (error) {
    if (!stale) loadError.value = error instanceof Error ? error.message : "该任务的审核记录读取失败";
  } finally {
    if (!stale) loading.value = false;
  }
}, { immediate: true });

function backToHistory(): void {
  const target = { name: "manual-reviews", query: { tab: "history" } };
  if (router.options.history.state.back === router.resolve(target).fullPath) {
    router.back();
  } else {
    void router.replace(target);
  }
}

function formatValue(value: unknown): string {
  if (value === null || value === undefined || value === "") return "未取得值";
  return typeof value === "string" ? value : JSON.stringify(value);
}

function evidenceLabel(evidence: Record<string, unknown>): string {
  const parts = [evidence.document_name, evidence.material_name].filter((value) => typeof value === "string");
  if (typeof evidence.artifact_page === "number") parts.push(`OCR 文本第 ${evidence.artifact_page + 1} 页`);
  return parts.join(" · ") || "原文依据";
}

const missingReasonLabels: Record<string, string> = {
  MATERIAL_NOT_UPLOADED: "材料未上传",
  EXTRACTION_NOT_ENABLED: "方案未启用字段抽取",
  EXTRACTION_NOT_COMPLETED: "字段抽取未完成",
  NOT_PRESENT: "材料中未载明",
  EVIDENCE_INSUFFICIENT: "原文依据不足",
};

function missingReasonLabel(reason: unknown): string {
  return typeof reason === "string" ? missingReasonLabels[reason] ?? "需核实" : "需核实";
}
</script>

<template>
  <div class="app-panel history-page">
    <PageHeader title="字段核对记录" :notes="selectedRecord ? [selectedRecord.task_name] : []" :show-refresh="false">
      <template #leading><BackButton label="返回已确认列表" @click="backToHistory" /></template>
    </PageHeader>

    <StateBlock v-if="loading" kind="loading" />
    <StateBlock v-else-if="loadError !== null" kind="error" title="审核记录读取失败" :description="loadError">
      <el-button @click="reload++">重试</el-button>
    </StateBlock>
    <template v-else-if="selectedRecord">
      <div class="history-toolbar">
        <label for="confirmation-batch">确认批次（{{ records.length }} 次）</label>
        <el-select id="confirmation-batch" v-model="selectedConfirmationNo" class="history-batch-select">
          <el-option
            v-for="item in records"
            :key="item.confirmation_no"
            :value="item.confirmation_no"
            :label="`${formatDate(item.created_at)} · 缺项 ${item.missing_field_count}`"
          />
        </el-select>
      </div>

      <el-descriptions :column="2" border class="history-meta">
        <el-descriptions-item label="输入版本">第 {{ selectedRecord.input_revision }} 版</el-descriptions-item>
        <el-descriptions-item label="确认时缺项">{{ selectedRecord.missing_field_count }} 项</el-descriptions-item>
        <el-descriptions-item label="确认说明" :span="2">{{ selectedRecord.note || "未填写" }}</el-descriptions-item>
      </el-descriptions>

      <el-alert
        v-if="!selectedRecord.snapshot"
        type="warning"
        :closable="false"
        show-icon
        title="该批次未保存确认时的字段与原文，不能用当前数据还原。"
      />
      <template v-else>
        <h2 class="app-section-title">确认时字段 <span class="text-muted">{{ snapshotFields.length }} 项</span></h2>
        <el-table :data="snapshotFields" border stripe empty-text="该批次没有记录字段">
          <el-table-column prop="field_name" label="字段" min-width="170" fixed="left" />
          <el-table-column prop="material_name" label="材料" min-width="170" />
          <el-table-column label="确认值" min-width="210">
            <template #default="{ row }">{{ formatValue(row.value) }}<span v-if="row.unit"> {{ row.unit }}</span></template>
          </el-table-column>
          <el-table-column label="抽取原文" min-width="200">
            <template #default="{ row }">{{ row.raw_value || "—" }}</template>
          </el-table-column>
          <el-table-column label="字段状态" width="135">
            <template #default="{ row }">{{ row.status ? statusTextOf("field", row.status) : "未取得值" }}</template>
          </el-table-column>
          <el-table-column label="材料版本" min-width="190">
            <template #default="{ row }">{{ row.upload_filename || "未上传" }}<span v-if="row.upload_no"> · {{ row.upload_no }}</span></template>
          </el-table-column>
          <el-table-column label="原文依据" min-width="260">
            <template #default="{ row }">
              <div v-if="row.evidence.length" class="evidence-list">
                <div v-for="(evidence, index) in row.evidence" :key="index" class="evidence-item">
                  <strong>{{ evidenceLabel(evidence) }}</strong>
                  <span>{{ evidence.quote_text || "未记录引用片段" }}</span>
                </div>
              </div>
              <span v-else class="text-hint">未记录原文片段</span>
            </template>
          </el-table-column>
        </el-table>

        <template v-if="snapshotMissing.length">
          <h2 class="app-section-title">确认时保留缺项 <span class="text-muted">{{ snapshotMissing.length }} 项</span></h2>
          <el-table :data="snapshotMissing" border size="small">
            <el-table-column prop="field_name" label="字段" min-width="180" />
            <el-table-column prop="material_name" label="材料" min-width="180" />
            <el-table-column label="原因" min-width="180">
              <template #default="{ row }">{{ missingReasonLabel(row.reason) }}</template>
            </el-table-column>
          </el-table>
        </template>
      </template>
    </template>
    <StateBlock v-else kind="empty" title="该任务暂无确认记录" />
  </div>
</template>

<style scoped>
.history-page { min-width: 0; }
.history-toolbar { display: flex; flex-wrap: wrap; align-items: center; gap: 8px 12px; margin-bottom: 14px; }
.history-batch-select { width: min(100%, 340px); }
.history-meta { margin-bottom: 18px; overflow-wrap: anywhere; }
.app-section-title .text-muted { margin-left: 4px; font-weight: 400; }
.evidence-list { display: grid; gap: 8px; }
.evidence-item { display: grid; gap: 3px; line-height: 1.5; overflow-wrap: anywhere; }
.evidence-item strong { color: var(--el-text-color-secondary); font-size: 12px; font-weight: 500; }
.evidence-item span { white-space: pre-wrap; }
</style>
