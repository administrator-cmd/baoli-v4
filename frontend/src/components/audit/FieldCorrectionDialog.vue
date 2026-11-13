<script setup lang="ts">
import { computed, ref, watch } from "vue";

import type {
  FieldCorrectionRequest,
  FieldCorrectionResponse,
  AuditField,
} from "@/api/contracts";

import StatusBadge from "@/components/StatusBadge.vue";
import { formatBusinessValue } from "@/constants/labels";
import { statusTextOf } from "@/constants/statusVocabulary";

/**
 * 字段纠错弹窗。
 *
 * PRESENT 错值与 NOT_PRESENT 漏值都能主动发起；提交后展示服务端返回的失效与重算范围，
 * 由服务端按同一类型校验、版本记录与下游重算通道处理，而不是绕过复核直接覆盖。
 */
const props = defineProps<{
  open: boolean;
  field: AuditField | null;
  /** 服务端返回的纠错结果，用于展示失效与重算范围。 */
  result: FieldCorrectionResponse | null;
  pending?: boolean;
}>();

const emit = defineEmits<{
  close: [];
  submit: [payload: FieldCorrectionRequest];
}>();

const decision = ref<"MODIFY_FIELD" | "MARK_NOT_PRESENT">("MODIFY_FIELD");
const fieldValue = ref("");
const businessNote = ref("");
const basisType = ref<"HUMAN_STATEMENT" | "DOCUMENT_EVIDENCE" | "UPLOAD_RECORD">("HUMAN_STATEMENT");

const currentValue = computed(() => formatBusinessValue(props.field?.raw_value ?? props.field?.normalized_value));
const isMissing = computed(() => props.field?.status === "NOT_PRESENT");

watch(
  () => [props.open, props.field] as const,
  () => {
    if (!props.open) {
      return;
    }
    decision.value = "MODIFY_FIELD";
    fieldValue.value = "";
    businessNote.value = "";
    basisType.value = "HUMAN_STATEMENT";
  },
  { immediate: true },
);

const valid = computed(
  () => businessNote.value.trim().length > 0 && (decision.value === "MARK_NOT_PRESENT" || fieldValue.value.trim().length > 0),
);

function submit(): void {
  if (!props.field || !valid.value) {
    return;
  }
  emit("submit", {
    field_key: props.field.field_key ?? props.field.name,
    material_key: props.field.material_key ?? props.field.material_name,
    decision: decision.value,
    ...(decision.value === "MODIFY_FIELD" ? { field_value: fieldValue.value.trim() } : {}),
    business_note: businessNote.value.trim(),
    basis_type: basisType.value,
  });
}
</script>

<template>
  <el-dialog :model-value="open" title="字段纠错" width="600px" append-to-body @update:model-value="emit('close')">
    <el-descriptions :column="1" size="small" border>
      <el-descriptions-item label="字段">{{ field?.name || "未知字段" }}</el-descriptions-item>
      <el-descriptions-item label="来源材料">{{ field?.material_name || "未关联材料" }}</el-descriptions-item>
      <el-descriptions-item label="当前值">
        {{ currentValue }}
        <StatusBadge v-if="field" class="block-gap-sm" domain="field" :status="field.status" />
      </el-descriptions-item>
    </el-descriptions>

    <template v-if="!result">
      <el-form label-position="top" class="block-gap">
        <el-form-item label="纠错动作" required>
          <el-radio-group v-model="decision">
            <el-radio value="MODIFY_FIELD">修改为人工确认的值</el-radio>
            <el-radio value="MARK_NOT_PRESENT">标记为文档中不存在</el-radio>
          </el-radio-group>
          <div v-if="isMissing" class="text-hint">该字段当前是「未抽取到」；若实际存在，请选择修改并填写真实值。</div>
        </el-form-item>

        <el-form-item v-if="decision === 'MODIFY_FIELD'" label="纠正后的值" required>
          <el-input v-model="fieldValue" placeholder="填写人工确认的真实值" style="max-width: 420px" />
        </el-form-item>

        <el-form-item label="依据类型" required>
          <el-select v-model="basisType" style="width: 260px">
            <el-option label="人工业务判断" value="HUMAN_STATEMENT" />
            <el-option label="文档证据" value="DOCUMENT_EVIDENCE" />
            <el-option label="上传记录" value="UPLOAD_RECORD" />
          </el-select>
        </el-form-item>

        <el-form-item label="业务说明" required>
          <el-input
            v-model="businessNote"
            type="textarea"
            :rows="3"
            maxlength="2000"
            show-word-limit
            placeholder="说明为什么需要纠正，便于后续追溯"
          />
        </el-form-item>
      </el-form>
    </template>

    <template v-else>
      <el-alert class="block-gap" type="success" show-icon :closable="false" title="纠错已提交">
        新的字段事实版本 {{ result.fact_revision_no }} 已生效，当前状态 {{ statusTextOf("field", result.status) }}。
      </el-alert>
      <el-descriptions :column="1" size="small" border>
        <el-descriptions-item label="修改前">{{ result.before_value ?? "未提供" }}</el-descriptions-item>
        <el-descriptions-item label="修改后">{{ result.after_value ?? "未提供" }}</el-descriptions-item>
        <el-descriptions-item label="失效复核">{{ result.invalidated_reviews }} 项</el-descriptions-item>
        <el-descriptions-item label="失效规则结果">{{ result.invalidated_rule_results }} 条</el-descriptions-item>
        <el-descriptions-item label="重算状态">
          <span v-if="result.recomputed_rules.length">{{ result.recomputed_rules.join("、") }}</span>
          <span v-else class="text-muted">本次仅保存纠错，未自动重新执行审核。</span>
        </el-descriptions-item>
      </el-descriptions>
    </template>

    <template #footer>
      <el-button v-if="result" type="primary" @click="emit('close')">关闭</el-button>
      <template v-else>
        <el-button @click="emit('close')">取消</el-button>
        <el-button type="primary" :disabled="!valid" :loading="pending" @click="submit">提交纠错</el-button>
      </template>
    </template>
  </el-dialog>
</template>

<style scoped>
:deep(.el-descriptions__label) {
  width: 110px;
}
</style>
