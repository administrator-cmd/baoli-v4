<script setup lang="ts">
import { computed } from "vue";
import { FileSearch } from "lucide-vue-next";

import type { PendingReview } from "@/api/contracts";
import StatusBadge from "@/components/StatusBadge.vue";
import {
  basisTypeOptions,
  formatBusinessValue,
  reviewDecisionLabel,
  reviewOptions,
  reviewTypeLabel,
  statusLabel,
  statusTone,
} from "@/constants/labels";

export interface ReviewDraft {
  decision: string;
  note: string;
  fieldValue: string;
  /** 人工结论的依据类型：人工说明 / 文档证据 / 上传记录。 */
  basisType: string;
}

/**
 * 单条待复核项的详情与填写区。
 *
 * 首屏按人工真正要回答的问题组织：
 *   系统原判断 → 原文内容 → 系统标准化结果 → 当前有效值 → 用户修改后的值 →
 *   修改理由 → 复核后有效状态 → 依赖哪些规则重新计算。
 * 证据展示引用原文和内容依据核对结论。
 *
 * 系统判断只读；人工结论由用户在业务选项中选择，并显式选择依据类型。
 * 不要求用户填 JSON、编码或内部编号。
 */
const props = defineProps<{
  review: PendingReview;
  draft: ReviewDraft;
  /** 未到人工复核等待状态时不允许修改。 */
  disabled: boolean;
}>();

const emit = defineEmits<{
  "update:draft": [value: ReviewDraft];
  "open-evidence": [evidenceNo: string];
}>();

const options = computed(() => reviewOptions(props.review.review_type));

/** 确认沿用系统候选值，只有修改字段时允许编辑最终值。 */
const needsFieldValue = computed(() => props.draft.decision === "CONFIRM_FIELD" || props.draft.decision === "MODIFY_FIELD");
const fieldValueReadOnly = computed(() => props.draft.decision === "CONFIRM_FIELD");

const judgment = computed(() => props.review.system_judgment ?? null);
const judgmentEvidences = computed(() => {
  if (judgment.value?.evidences?.length) return judgment.value.evidences;
  return judgment.value?.evidence ? [judgment.value.evidence] : [];
});
const reviewEvidenceOptions = computed(() =>
  judgmentEvidences.value.flatMap((item) =>
    typeof item.evidence_no === "string" && item.evidence_no.length > 0
      ? [{ ...item, evidence_no: item.evidence_no }]
      : [],
  ),
);
const singleReviewEvidenceNo = computed(() =>
  reviewEvidenceOptions.value.length === 1 ? reviewEvidenceOptions.value[0]?.evidence_no ?? null : null,
);

/**
 * 主体/签章一致性核对的判据：待核对主体原文、检索候选与完整印章条目。
 * 三者一起展示，人工才能对照判断；只给系统状态无法完成主体核对。
 */
const subjectQuery = computed(() => judgment.value?.subject_query ?? null);
const retrievalSnapshot = computed(() => judgment.value?.retrieval ?? null);
const sealRecords = computed(() => judgment.value?.seal_records ?? []);
const isSubjectReview = computed(
  () => Boolean(retrievalSnapshot.value || sealRecords.value.length || subjectQuery.value),
);

/**
 * 材料类型核对的原始识别结论（8018 返回）。
 * 置信度不参与判定，但它是人工判断「文件是否放错材料位」的直接依据，必须展示；
 * 数值原样呈现，不在这里设阈值或把它翻译成「可信/不可信」的结论。
 */
const materialClassify = computed(() => judgment.value?.service_result ?? null);

/** 命中关键词原文；服务返回的不是字符串列表时按「未记录」处理，不臆造内容。 */
const matchedKeywords = computed<string[]>(() => {
  const raw = materialClassify.value?.matched_keywords;
  return Array.isArray(raw) ? raw.filter((item): item is string => typeof item === "string") : [];
});

/** 系统标准化结果：只认服务端快照里的 normalized_value。 */
const normalizedText = computed(() => {
  const value = judgment.value?.normalized_value;
  if (value === null || value === undefined || value === "") {
    return null;
  }
  return formatBusinessValue(value);
});

/** 当前有效值：字段确认类以 before_value 为准；没有时退回系统抽取值。 */
const currentValueText = computed(() => {
  if (props.review.before_value !== null && props.review.before_value !== undefined) {
    return formatBusinessValue(props.review.before_value);
  }
  return null;
});

/** 用户本次填写的修改后值；没有填写时不显示成「已改」。 */
const draftValueText = computed(() => {
  if (!needsFieldValue.value) {
    return null;
  }
  return props.draft.fieldValue.trim() || null;
});

/**
 * 依赖哪些规则重新计算。
 *
 * 服务端在复核响应里给出 `field_changes` 与 `recomputed_rules`；待办本身没有携带
 * 该清单，因此这里不猜——只如实提示「提交后由服务端计算失效与重算范围」，
 * 并在字段确认类复核里说明会退回校验链路的哪一步（这是已确认的业务口径）。
 */
const recomputeText = computed(() => {
  if (props.review.review_type === "FIELD_CONFIRMATION") {
    return "提交后：字段事实生成新版本 → 抽取规则结果重算 → 文件内部校验及其之后的结论失效并重新判定。具体失效与重算清单以服务端返回为准。";
  }
  if (props.review.review_type === "RULE_RESULT") {
    return "提交后：本条规则的最终状态按人工结论更新，依赖它的风险规则重新评定。具体重算清单以服务端返回为准。";
  }
  if (props.review.review_type === "MATERIAL_TYPE_MISMATCH") {
    return "提交后：材料位类型状态按人工结论更新，受影响的文件质量结论就地重算，不退回已排队的复核。具体重算清单以服务端返回为准。";
  }
  return "提交后由服务端计算失效与重算范围；页面不预先推断。";
});

function update(patch: Partial<ReviewDraft>): void {
  emit("update:draft", { ...props.draft, ...patch });
}

function openReviewEvidence(command: unknown): void {
  if (typeof command === "string" && command) {
    emit("open-evidence", command);
  }
}

function openSingleReviewEvidence(): void {
  const evidenceNo = singleReviewEvidenceNo.value;
  if (evidenceNo) {
    emit("open-evidence", evidenceNo);
  }
}
</script>

<template>
  <div>
    <div class="review-head">
      <div>
        <div class="review-head__title">{{ review.material_name || "未关联材料" }}</div>
        <div class="text-muted">
          {{ reviewTypeLabel(review.review_type) }}
          <template v-if="review.field_name"> · 字段：{{ review.field_name }}</template>
          <template v-if="review.rule_name"> · 规则：{{ review.rule_name }}</template>
        </div>
      </div>
      <StatusBadge status="WAITING_REVIEW" size="default" />
    </div>

    <!-- 1. 系统原判断（不可变快照，只读） -->
    <el-descriptions :column="1" size="small" border class="block-gap">
      <el-descriptions-item label="系统原判断">
        <template v-if="judgment">
          <div>
            <StatusBadge :status="judgment.status" :label="judgment.status ? statusLabel(judgment.status) : '无状态'" :tone="statusTone(judgment.status)" />
          </div>
          <div class="block-gap-sm">{{ judgment.message || "系统没有留下判断说明，请以原文证据为准。" }}</div>
        </template>
        <span v-else>系统没有留下判断快照，请以原文证据为准。</span>
      </el-descriptions-item>

      <!-- 2. 原文内容：本条复核判断所依据的材料原文 -->
      <el-descriptions-item label="原文内容">
        <template v-if="judgmentEvidences.length">
          <div v-for="(item, index) in judgmentEvidences" :key="index" class="review-quote block-gap-sm">
            <div v-if="item.field_path" class="text-muted">字段属性：{{ item.field_path }}</div>
            {{ item.quote_text || "本条证据没有记录引用原文。" }}
            <div v-if="item.quote_check_label" class="text-muted">原文核对：{{ item.quote_check_label }}</div>
          </div>
        </template>
        <div v-else class="review-quote">{{ review.evidence_quote || "本条复核没有关联证据原文，请人工核对材料后再下结论。" }}</div>
        <el-button
          v-if="singleReviewEvidenceNo"
          link
          type="primary"
          class="block-gap-sm"
          @click="openSingleReviewEvidence"
        >
          <template #icon><FileSearch :size="14" /></template>
          查看完整证据
        </el-button>
        <el-dropdown
          v-else-if="reviewEvidenceOptions.length > 1"
          trigger="click"
          class="block-gap-sm"
          @command="openReviewEvidence"
        >
          <el-button link type="primary">
            <template #icon><FileSearch :size="14" /></template>
            查看完整证据（{{ reviewEvidenceOptions.length }}）
          </el-button>
          <template #dropdown>
            <el-dropdown-menu>
              <el-dropdown-item
                v-for="item in reviewEvidenceOptions"
                :key="item.evidence_no"
                :command="item.evidence_no"
              >
                {{ item.field_path || "字段原文" }}
              </el-dropdown-item>
            </el-dropdown-menu>
          </template>
        </el-dropdown>
        <el-button
          v-else-if="review.evidence_no"
          link
          type="primary"
          class="block-gap-sm"
          @click="emit('open-evidence', review.evidence_no)"
        >
          <template #icon><FileSearch :size="14" /></template>
          查看完整证据
        </el-button>
      </el-descriptions-item>

      <!-- 3. 系统标准化结果：系统把原文算成了什么；不代表系统判断正确 -->
      <el-descriptions-item label="系统标准化结果">
        <template v-if="normalizedText">{{ normalizedText }}</template>
        <span v-else class="text-muted">服务端没有返回标准化结果。</span>
      </el-descriptions-item>

      <!-- 4. 当前有效值：进入本条复核前，该对象在系统里的值 -->
      <el-descriptions-item label="当前有效值">
        <template v-if="currentValueText">{{ currentValueText }}</template>
        <span v-else class="text-muted">服务端没有返回当前有效值。</span>
      </el-descriptions-item>

      <!-- 5. 系统建议值（字段确认类的抽取建议） -->
      <el-descriptions-item v-if="review.proposed_value" label="系统建议值">
        {{ review.proposed_value }}
      </el-descriptions-item>

      <!-- 6. 用户修改后的值：只显示本次真实填写的值 -->
      <el-descriptions-item v-if="draftValueText" label="将生效的修改后值">
        <span class="review-value">{{ draftValueText }}</span>
      </el-descriptions-item>

      <!-- 7. 复核后有效状态：本条已有人工结论时服务端返回 -->
      <el-descriptions-item v-if="review.effective_status" label="复核后有效状态">
        <StatusBadge :status="review.effective_status" />
      </el-descriptions-item>
      <el-descriptions-item v-if="review.manual_conclusion" label="已有人工结论">
        {{ reviewDecisionLabel(review.manual_conclusion) }}
      </el-descriptions-item>
      <el-descriptions-item v-if="review.business_note" label="历史修改理由">{{ review.business_note }}</el-descriptions-item>

      <!-- 8. 依赖哪些规则重新计算 -->
      <el-descriptions-item label="提交后重算范围">{{ recomputeText }}</el-descriptions-item>
    </el-descriptions>

    <!-- 材料类型核对：8018 的原始识别结论，人工据此判断是否放错材料位 -->
    <el-descriptions v-if="materialClassify" :column="1" size="small" border class="block-gap">
      <el-descriptions-item label="材料识别结论">
        <div>服务判定：{{ materialClassify.detected_material_type || "未识别出类型" }}</div>
        <div>材料位声明：{{ materialClassify.declared_material_type || "未声明" }}</div>
        <div>置信度：{{ materialClassify.confidence ?? "—" }}（仅作参考，不参与判定）</div>
        <div>
          命中关键词：
          <template v-if="matchedKeywords.length">{{ matchedKeywords.join("、") }}</template>
          <template v-else>未命中任何关键词</template>
        </div>
        <div class="text-hint">
          置信度高低不改变流程走向：系统只区分「文件是否通过该材料类型的识别」，
          不匹配即交人工核对，由人工判断文件是否放错材料位。
        </div>
      </el-descriptions-item>
    </el-descriptions>

    <!-- 主体核对判据：双方原文 + 候选 + 完整印章条目，全部来自本次执行的快照 -->
    <el-card v-if="isSubjectReview" shadow="never" class="block-gap">
      <template #header>主体核对判据（来自本次执行快照）</template>
      <div v-if="subjectQuery" class="block-gap-sm">
        <div class="subject-label">待核对主体（材料侧）</div>
        <div class="subject-value">{{ subjectQuery.value || "未取得有效值" }}</div>
        <div class="text-muted">
          来源：{{ subjectQuery.material_name || "未关联材料" }}
          <template v-if="subjectQuery.field_name"> · {{ subjectQuery.field_name }}</template>
        </div>
      </div>
      <div v-else-if="retrievalSnapshot" class="text-muted block-gap-sm">
        本次快照未记录待核对主体原文；以下候选仍需人工核对。
      </div>

      <div v-if="retrievalSnapshot" class="block-gap-sm">
        <div class="subject-label">检索候选（{{ retrievalSnapshot.candidate_count ?? 0 }} 条，相似度仅用于排序）</div>
        <el-table :data="retrievalSnapshot.candidates || []" size="small" border empty-text="本次没有返回候选">
          <el-table-column prop="subject_name" label="识别到的印章主体" min-width="180" show-overflow-tooltip />
          <el-table-column label="归属" min-width="150" show-overflow-tooltip>
            <template #default="{ row }">{{ row.material_name || "未关联材料" }}</template>
          </el-table-column>
          <el-table-column label="相似度" width="110">
            <template #default="{ row }">{{ row.similarity ?? "—" }}（仅排序）</template>
          </el-table-column>
          <el-table-column label="候选身份" width="180">
            <template #default="{ row }">
              <span class="mono">{{ row.index_no ?? "—" }}#{{ row.chunk_no ?? "—" }}#{{ row.source_entry_no ?? "—" }}</span>
            </template>
          </el-table-column>
        </el-table>
        <div v-if="(retrievalSnapshot.uncovered_materials || []).length" class="text-muted block-gap-sm">
          <div v-for="item in retrievalSnapshot.uncovered_materials" :key="item.material_key">
            {{ item.material_key || "未标明材料" }}：{{ item.reason || "原因未记录" }}
          </div>
        </div>
        <div class="text-hint">
          相似度不构成主体一致的结论；候选是否与待核对主体一致，由人工判断并填写结论与依据。
        </div>
      </div>

      <div class="block-gap-sm">
        <div class="subject-label">规则材料范围内的全部印章条目（{{ sealRecords.length }} 条）</div>
        <el-table :data="sealRecords" size="small" border empty-text="规则材料范围内没有印章识别记录">
          <el-table-column prop="material_name" label="材料" min-width="140" show-overflow-tooltip />
          <el-table-column prop="subject_name" label="识别主体" min-width="180" show-overflow-tooltip />
          <el-table-column prop="seal_type" label="印章类型" width="120" show-overflow-tooltip />
          <el-table-column label="置信度" width="100">
            <template #default="{ row }">{{ row.confidence ?? "—" }}</template>
          </el-table-column>
        </el-table>
        <div class="text-hint">识别结果原样保留，不做合并，也不按相似度挑选「最匹配」的一条。</div>
      </div>
    </el-card>

    <!-- 人工结论填写区 -->
    <el-form label-position="top" class="block-gap">
      <el-form-item label="人工结论" required>
        <el-radio-group
          :model-value="draft.decision"
          :disabled="disabled"
          @update:model-value="update({ decision: $event })"
        >
          <el-radio v-for="option in options" :key="option.value" :value="option.value" class="review-radio">
            {{ option.label }}
          </el-radio>
        </el-radio-group>
      </el-form-item>

      <el-form-item label="结论依据类型" required>
        <el-select
          :model-value="draft.basisType || 'HUMAN_STATEMENT'"
          :disabled="disabled"
          style="max-width: 320px"
          @update:model-value="update({ basisType: $event })"
        >
          <el-option v-for="option in basisTypeOptions" :key="option.value" :label="option.label" :value="option.value" />
        </el-select>
        <div class="text-hint">人工说明与文档证据要分开：旧原文证据不能当作人工新值的证明。</div>
      </el-form-item>

      <el-form-item v-if="needsFieldValue" label="最终生效的字段值" required>
        <el-input
          :model-value="draft.fieldValue"
          :disabled="disabled || fieldValueReadOnly"
          :placeholder="fieldValueReadOnly ? '沿用系统候选值' : '填写人工修改后的字段值'"
          style="max-width: 420px"
          @update:model-value="update({ fieldValue: $event })"
        />
      </el-form-item>

      <el-form-item label="修改理由（业务说明）" required>
        <el-input
          :model-value="draft.note"
          type="textarea"
          :rows="3"
          maxlength="2000"
          show-word-limit
          :disabled="disabled"
          placeholder="说明判断依据，便于后续追溯；服务端要求每条复核都填写"
          @update:model-value="update({ note: $event })"
        />
      </el-form-item>
    </el-form>

  </div>
</template>

<style scoped>
.review-head {
  display: flex;
  flex-wrap: wrap;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
}

.review-head__title {
  font-size: 15px;
  font-weight: 600;
}

.review-quote {
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}

.review-value {
  font-weight: 600;
}

.review-radio {
  display: flex;
  height: auto;
  margin-right: 0;
  white-space: normal;
  line-height: 1.6;
}

.subject-label {
  font-weight: 600;
  margin-bottom: 4px;
}

.subject-value {
  font-size: 15px;
  font-weight: 600;
  overflow-wrap: anywhere;
}
</style>
