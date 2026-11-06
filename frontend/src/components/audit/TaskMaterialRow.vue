<script setup lang="ts">
import { computed, ref, watch } from "vue";
import { ElMessage, ElMessageBox } from "element-plus";
import { Upload } from "lucide-vue-next";
import type { AuditDocument } from "@/api/contracts";
import MaterialTypeSelect from "@/components/audit/MaterialTypeSelect.vue";
import StatusBadge from "@/components/StatusBadge.vue";
import type { MaterialTypeCatalog } from "@/composables/useMaterialTypeCatalog";
import type { StatusTone } from "@/constants/labels";
import { materialQualityLabel, materialQualityTone } from "@/constants/materialQuality";
import {
  processingArtifactLabel,
  processingStatusLabel,
  type ProcessingArtifact,
} from "@/constants/materialProcessing";

const props = defineProps<{
  document: AuditDocument;
  catalog: MaterialTypeCatalog;
  busy: boolean;
  disabled: boolean;
  /** 同一任务已有后台操作在跑：只读的处理状态仍可查看，处理中的产物不算失败。 */
  operationActive?: boolean;
  replacementImpact?: {
    stale_field_values: number;
    invalidated_rule_results: number;
    invalidated_node_executions: number;
    invalidated_reviews: number;
    description?: string;
  } | null;
}>();

const emit = defineEmits<{
  pick: [file: File, materialType: string];
  remove: [document: AuditDocument];
  inspectProcessing: [document: AuditDocument];
}>();

const uploadRef = ref<{ clearFiles: () => void } | null>(null);
const selectedType = ref("");
/** 当前上传已保存的类型；上传后刷新或重新进入任务时用它回显。 */
const savedMaterialType = computed(() => (
  props.document.declared_material_type
  ?? props.document.material_classify?.declared_material_type
  ?? ""
));
watch(savedMaterialType, (value) => {
  selectedType.value = value;
}, { immediate: true });
const missingRequired = computed(() => props.document.required && !props.document.filename);
const quality = computed(() => props.document.quality_precheck);
const qualityBlocked = computed(() => quality.value?.blocked === true);
const waitingQuality = computed(() => Boolean(props.document.filename && quality.value?.processing_allowed === false && !qualityBlocked.value));
const qualityNote = computed(() => {
  if (qualityBlocked.value) return quality.value?.reasons[0]?.message ?? quality.value?.message;
  if (quality.value?.status === "RUNNING") return quality.value.message;
  return null;
});
const materialClassify = computed(() => props.document.material_classify ?? null);
const confidenceLabel = computed(() => {
  const confidence = materialClassify.value?.confidence;
  return typeof confidence === "number" ? `${(confidence * 100).toFixed(1)}%` : "—";
});

function materialStatusTone(status: string | null | undefined): StatusTone {
  if (qualityBlocked.value) return "warning";
  if (waitingQuality.value) return "neutral";
  if (artifactAttention.value) return "danger";
  if (processingActive.value) return "info";
  if (status === "PREPARED" || status === "PROCESSED") return "success";
  if (status === "TYPE_REJECTED") return "danger";
  if (status === "TYPE_REVIEW" || status === "WAITING_UPLOAD") return "warning";
  if (status === "UPLOADED") return "info";
  return "neutral";
}

const statusLabelOverride = computed<string | null>(() => {
  if (qualityBlocked.value) return "已停止处理";
  if (waitingQuality.value) return "等待前置检查完成";
  if (artifactAttention.value) return "处理未完成";
  if (processingActive.value) return "处理中";
  if (props.document.status === "TYPE_REVIEW") return "材料类型待复核";
  if (props.document.status === "TYPE_REJECTED") return "材料类型不符";
  if (!props.document.filename) return "待上传";
  if (props.document.status === "UPLOADED") return "已上传，待处理";
  return null;
});

const impactText = computed(() => {
  const impact = props.replacementImpact;
  if (!impact) return null;
  return `字段 ${impact.stale_field_values} 项、规则 ${impact.invalidated_rule_results} 条、节点 ${impact.invalidated_node_executions} 次、复核 ${impact.invalidated_reviews} 项需更新`;
});

/** 四项处理产物状态（任务详情一次返回）；按处理顺序展示当前进度与失败项。 */
const ARTIFACT_ORDER = ["MATERIAL_CLASSIFY", "DOCUMENT_PARSE", "OCR", "SEAL"] as const;
const processingActive = computed(() => Object.values(props.document.artifacts ?? {}).some(
  (artifact) => artifact.status === "PENDING"
    || (props.operationActive && ["PROCESSING", "SUBMITTING"].includes(artifact.status)),
));

function artifactOf(type: (typeof ARTIFACT_ORDER)[number]): ProcessingArtifact | null {
  const item = props.document.artifacts?.[type];
  if (!item) return null;
  return {
    artifact_type: type,
    artifact_no: item.artifact_no,
    status: item.status,
    message: item.message,
    job_id: item.job_id ?? null,
    deadline_at: item.deadline_at ?? null,
    provider_state: item.provider_state ?? null,
    extract_progress: item.extract_progress ?? null,
    progress_updated_at: item.progress_updated_at ?? null,
  };
}

/** OCR 识别进度：只显示供应商真实返回的页数，没有页数时不编百分比。 */
const ocrProgressText = computed(() => {
  const ocr = artifactOf("OCR");
  if (!ocr || ocr.status !== "PENDING") return null;
  const extracted = ocr.extract_progress?.extractedPages;
  const total = ocr.extract_progress?.totalPages;
  if (typeof extracted !== "number") {
    return processingStatusLabel(ocr);
  }
  return typeof total === "number"
    ? `${processingStatusLabel(ocr)} ${extracted} / ${total} 页`
    : `${processingStatusLabel(ocr)} ${extracted} 页`;
});

/** 当前需要用户处理的处理项：确定失败，或操作结束后仍结果未知的第一项。 */
const artifactAttention = computed(() => {
  if (quality.value?.processing_allowed === false) return null;
  for (const type of ARTIFACT_ORDER) {
    const artifact = artifactOf(type);
    if (!artifact) continue;
    if (artifact.status === "FAILED") {
      return { label: processingArtifactLabel(type), artifact };
    }
    // 后台操作进行中时，PROCESSING/SUBMITTING 表示仍在处理，不是失败。
    if (!props.operationActive && ["PROCESSING", "SUBMITTING"].includes(artifact.status)) {
      return { label: processingArtifactLabel(type), artifact };
    }
  }
  return null;
});

async function onFileChange(file: { raw?: File }): Promise<void> {
  uploadRef.value?.clearFiles();
  if (!file.raw) return;
  if (!props.catalog.state.available) {
    ElMessage.error("材料类型目录不可用，无法上传：请重试读取目录");
    return;
  }
  if (!selectedType.value) {
    ElMessage.warning("请先选择材料类型，再上传文件");
    return;
  }
  if (props.document.filename) {
    try {
      await ElMessageBox.confirm(
        `替换「${props.document.material_name}」的当前文件后，已有的处理产物、字段结果、规则结果与报告会按服务端规则失效，需要重新处理并执行审核。旧文件保留为历史版本。`,
        "替换材料文件",
        { type: "warning", confirmButtonText: "替换", cancelButtonText: "取消" },
      );
    } catch {
      return;
    }
  }
  emit("pick", file.raw, selectedType.value);
}
</script>

<template>
  <tr class="material-row" :class="{
    'material-row--attention': missingRequired || qualityBlocked,
    'material-row--uncertain': qualityBlocked && quality?.status === 'EVIDENCE_INSUFFICIENT',
  }">
    <td>
      <div class="material-row__name">
        <strong>{{ document.material_name }}</strong>
        <el-tag :type="document.required ? (missingRequired ? 'danger' : 'warning') : 'info'" size="small" effect="plain">
          {{ document.required ? "必传" : "按需" }}
        </el-tag>
      </div>
    </td>
    <td class="material-row__file">{{ document.filename || "未上传" }}</td>
    <td>
      <StatusBadge
        :status="quality?.status"
        :label="document.filename ? materialQualityLabel(quality?.status) : '待上传'"
        :tone="materialQualityTone(quality?.status)"
      />
      <p v-if="qualityNote" class="material-row__note material-row__quality-note">{{ qualityNote }}</p>
      <el-button v-if="document.filename" link type="primary" size="small" :disabled="busy" @click="emit('inspectProcessing', document)">
        查看明细<template v-if="(quality?.reasons.length ?? 0) > 1">（{{ quality?.reasons.length }} 项问题）</template>
      </el-button>
    </td>
    <td>
      <div class="material-row__status">
        <StatusBadge :status="document.status" :label="statusLabelOverride" :tone="materialStatusTone(document.status)" />
        <span v-if="materialClassify" class="material-row__confidence">置信度 {{ confidenceLabel }}</span>
      </div>
      <div v-if="ocrProgressText && !qualityBlocked && !waitingQuality" class="material-row__note">{{ ocrProgressText }}</div>
      <div v-if="artifactAttention" class="material-row__note" :class="artifactAttention.artifact.status === 'FAILED' ? 'text-danger' : ''">
        {{ artifactAttention.label }}：{{ artifactAttention.artifact.message }}
      </div>
      <div v-if="document.status === 'TYPE_REJECTED'" class="text-danger material-row__note">相关检查将记为证据不足</div>
      <div v-if="replacementImpact" class="material-row__note">
        {{ replacementImpact.description || "本次替换已使旧结果失效" }}；{{ impactText }}
      </div>
    </td>
    <td><MaterialTypeSelect v-model="selectedType" :catalog="catalog" :show-error="false" :disabled="disabled || busy" /></td>
    <td>
      <div class="material-row__actions">
        <el-upload ref="uploadRef" accept=".pdf,.png,.jpg,.jpeg,.tif,.tiff" :auto-upload="false" :show-file-list="false" :disabled="disabled || busy" :on-change="onFileChange">
          <el-button size="small" type="primary" :loading="busy" :disabled="disabled || busy">
            <template #icon><Upload :size="14" /></template>
            {{ document.filename ? "替换" : "上传" }}
          </el-button>
        </el-upload>
        <template v-if="document.filename">
          <el-button link type="danger" size="small" :disabled="disabled || busy" @click="emit('remove', document)">移除</el-button>
        </template>
      </div>
    </td>
  </tr>
</template>

<style scoped>
.material-row--attention { background: var(--el-color-danger-light-9); }
.material-row--uncertain { background: var(--el-color-warning-light-9); }
.material-row__name,
.material-row__status,
.material-row__actions {
  display: flex;
  align-items: center;
  justify-content: center;
  flex-wrap: wrap;
  gap: 6px;
}
.material-row__file { overflow-wrap: anywhere; }
.material-row__note { margin-top: 4px; line-height: 1.5; overflow-wrap: anywhere; }
.material-row__quality-note {
  display: -webkit-box;
  -webkit-box-orient: vertical;
  -webkit-line-clamp: 2;
  overflow: hidden;
  margin-bottom: 4px;
}
.material-row__actions { gap: 4px 8px; }
.material-row__confidence { color: var(--el-text-color-secondary); font-size: 12px; }
</style>
