<script setup lang="ts">
import { computed, ref, watch } from "vue";
import { Search } from "lucide-vue-next";

import type { RetrievalCandidate, RetrievalDecision } from "@/api/contracts";
import StateBlock from "@/components/StateBlock.vue";
import { retrievalDecisionOptions } from "@/constants/labels";

/**
 * 印章主体检索结果面板。
 *
 * 三条限制必须在页面上如实呈现，不能被相似度分数掩盖：
 *   1. 相似度只用于候选排序，不构成主体一致的结论；
 *   2. 候选都是识别结果，不会自动合并，也不会自动勾选；
 *   3. 没有候选时分别说明原因（尚未识别主体 / 尚未建索引 / 索引失效 / 技术失败）。
 *
 * 结论由工作人员显式选择「一致 / 不一致 / 证据不足」后提交；面板不提供
 * “采纳前几条候选”这类入口，也不把 adopted 当成人已确认。
 */
const props = defineProps<{
  retrievalNo: string;
  status: string;
  message: string;
  candidates: RetrievalCandidate[];
  /** 有上传但没有可用主体索引的材料及原因；与「检索不到候选」分开显示。 */
  uncoveredMaterials?: Array<{ material_key?: string; reason?: string }>;
  /** 读取/检索失败时的真实原因；与「没有检索到候选」分开显示。 */
  errorMessage?: string | null;
  /** 提交后的结论（回显用）；未提交时为 null。 */
  decision?: string | null;
  decisionNote?: string | null;
  /** 已保存结论所依据的候选。 */
  decisionSelected?: RetrievalCandidate | null;
  /** 提交时的任务输入修订；用于识别结论是否基于过期候选。 */
  decisionInputRevision?: number | null;
  currentInputRevision?: number | null;
  loading?: boolean;
  submitting?: boolean;
}>();

const emit = defineEmits<{
  submit: [payload: { decision: RetrievalDecision; note: string; selected: RetrievalCandidate | null }];
}>();

const hasCandidates = computed(() => props.candidates.length > 0);

/** 当前选中的候选。默认不选：候选必须由工作人员显式点选。 */
const selectedChunkId = ref<number | null>(null);
const draftDecision = ref<RetrievalDecision | "">("");
const note = ref("");

// 换了检索结果就清空草稿，避免把上一次的选择带到新的候选上。
watch(
  () => props.retrievalNo,
  () => {
    selectedChunkId.value = null;
    draftDecision.value = "";
    note.value = "";
  },
);

const selectedCandidate = computed(
  () => props.candidates.find((item) => item.chunk_id === selectedChunkId.value) ?? null,
);

/** 结论已保存但任务输入在之后变了：候选已过期，不能继续沿用该结论。 */
const decisionStale = computed(
  () =>
    props.decision != null &&
    props.decisionInputRevision != null &&
    props.currentInputRevision != null &&
    props.decisionInputRevision !== props.currentInputRevision,
);

/** 「一致 / 不一致」必须指明依据的候选；「证据不足」不需要。 */
const canSubmit = computed(() => {
  if (props.status !== "OK") return false;
  if (!draftDecision.value) return false;
  if (draftDecision.value === "EVIDENCE_INSUFFICIENT") return true;
  return selectedCandidate.value !== null;
});

function submit(): void {
  if (!canSubmit.value || !draftDecision.value) return;
  emit("submit", {
    decision: draftDecision.value,
    note: note.value.trim(),
    selected: draftDecision.value === "EVIDENCE_INSUFFICIENT" ? null : selectedCandidate.value,
  });
}

/** 候选身份：索引号 + 分块号 + 产物条目序号。三者一起才能指回唯一一条候选。 */
function candidateKey(candidate: RetrievalCandidate): string {
  return `${candidate.index_no ?? "-"}#${candidate.chunk_no}#${candidate.source_entry_no ?? "-"}`;
}
</script>

<template>
  <div>
    <el-skeleton v-if="loading" :rows="4" animated />

    <el-alert v-else-if="errorMessage" type="error" show-icon :closable="false" :title="`检索失败：${errorMessage}`">
      检索失败与「没有识别到该主体」是两件事：请按错误原因处理后重试，不要据此认定合同不存在该主体。
    </el-alert>

    <template v-else>
      <el-alert type="info" show-icon :closable="false" class="block-gap-sm" title="相似度只用于候选排序">
        <div>相似度不构成主体一致的结论；候选来自印章识别结果，不会自动合并，也不会自动勾选。</div>
        <div v-if="status === 'NO_INDEX'" class="text-muted block-gap-sm">
          本次范围内没有可用的主体索引，请先完成印章识别与主体索引后重试。
        </div>
      </el-alert>

      <div class="text-muted block-gap-sm">
        检索编号：{{ retrievalNo || "未记录" }} · 系统说明：{{ message || "未记录" }}
      </div>

      <el-alert
        v-if="uncoveredMaterials?.length"
        type="warning"
        show-icon
        :closable="false"
        class="block-gap-sm"
        :title="status === 'PARTIAL_COVERAGE' ? '部分材料未进入本次候选' : '指定材料没有可用的主体索引'"
      >
        <div v-for="item in uncoveredMaterials" :key="item.material_key">
          {{ item.material_key || "未标明材料" }}：{{ item.reason || "原因未记录" }}
        </div>
        <div v-if="status === 'PARTIAL_COVERAGE'">本次候选仅供查看，不能作为完整材料范围的主体核对结论。</div>
      </el-alert>

      <el-alert
        v-if="decisionStale"
        type="warning"
        show-icon
        :closable="false"
        class="block-gap-sm"
        title="已保存的结论基于过期候选"
      >
        任务输入在提交结论之后发生了变化（结论修订 {{ decisionInputRevision }}，当前修订
        {{ currentInputRevision }}）。该结论不再适用于当前候选，请重新检索并重新确认。
      </el-alert>

      <StateBlock
        v-if="!hasCandidates"
        kind="empty"
        :title="status === 'NO_INDEX' ? '本次范围内没有可用的主体索引' : '本次范围内没有识别到印章主体候选'"
        description="没有候选不等于合同不存在该主体；请先确认印章识别已完成、主体索引已建立后重试。"
      />

      <template v-else>
        <div class="retrieval-heading">印章主体候选（{{ candidates.length }} 条）</div>
        <el-radio-group v-model="selectedChunkId" class="candidate-list" :disabled="status !== 'OK'">
          <el-card
            v-for="candidate in candidates"
            :key="candidateKey(candidate)"
            shadow="never"
            class="candidate-card"
            :class="{ 'is-selected': selectedChunkId === candidate.chunk_id }"
            @click="status === 'OK' && (selectedChunkId = candidate.chunk_id)"
          >
            <div class="candidate-head">
              <el-radio :value="candidate.chunk_id" class="candidate-radio">
                <span class="candidate-title">
                  <Search :size="14" />
                  {{ candidate.subject_name || "未识别出主体名称" }}
                </span>
              </el-radio>
              <span class="text-muted">相似度 {{ candidate.similarity }}（仅排序用）</span>
            </div>
            <div class="text-muted">
              归属：{{ candidate.material_name || "未关联材料" }}
              <template v-if="candidate.filename"> · {{ candidate.filename }}</template>
              <template v-if="candidate.source_entry_no"> · 印章条目第 {{ candidate.source_entry_no }} 条</template>
            </div>
            <div v-if="decisionSelected && candidateKey(decisionSelected) === candidateKey(candidate)" class="text-hint">
              已保存结论依据的是本条候选。
            </div>
          </el-card>
        </el-radio-group>

        <el-alert
          v-if="decision"
          :type="decision === 'INCONSISTENT' ? 'warning' : 'success'"
          show-icon
          :closable="false"
          class="block-gap-sm"
          title="已保存的主体核对结论"
        >
          <div>
            结论：{{ retrievalDecisionOptions.find((item) => item.value === decision)?.label || decision }}
            <template v-if="decisionNote"> · 理由：{{ decisionNote }}</template>
          </div>
        </el-alert>

        <el-form v-if="status === 'OK'" label-position="top" class="block-gap">
          <el-form-item label="核对结论">
            <el-select v-model="draftDecision" placeholder="请选择一致、不一致或证据不足" style="max-width: 320px">
              <el-option
                v-for="item in retrievalDecisionOptions"
                :key="item.value"
                :label="item.label"
                :value="item.value"
              />
            </el-select>
            <div class="text-hint">
              勾选候选后选择「一致」或「不一致」；无法判断时选择「证据不足」。相似度不参与结论。
            </div>
          </el-form-item>
          <el-form-item label="理由（可选）">
            <el-input v-model="note" type="textarea" :rows="2" maxlength="2000" show-word-limit />
          </el-form-item>
          <el-form-item>
            <el-button type="primary" :disabled="!canSubmit" :loading="submitting" @click="submit">
              保存结论
            </el-button>
          </el-form-item>
        </el-form>
      </template>

      <el-collapse class="block-gap">
        <el-collapse-item title="查看技术信息" name="tech">
          <div class="mono">检索状态：{{ status }}</div>
          <div class="mono">候选条数：{{ candidates.length }}</div>
          <div class="mono">当前输入修订：{{ currentInputRevision ?? "未记录" }}</div>
        </el-collapse-item>
      </el-collapse>
    </template>
  </div>
</template>

<style scoped>
.retrieval-heading {
  font-weight: 600;
  margin: 12px 0 8px;
}

.candidate-list {
  display: block;
  width: 100%;
}

.candidate-card {
  margin-bottom: 8px;
  cursor: pointer;
}

.candidate-card.is-selected {
  border-color: var(--el-color-primary);
}

.candidate-head {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}

.candidate-radio {
  height: auto;
  margin-right: 0;
}

.candidate-title {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-weight: 600;
}
</style>
