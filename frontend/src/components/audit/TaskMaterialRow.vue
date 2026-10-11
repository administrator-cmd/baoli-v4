<script setup lang="ts">
import { computed, ref } from "vue";
import { ElMessage } from "element-plus";
import { Download, Eye, Upload } from "lucide-vue-next";

import type { AuditDocument } from "@/api/contracts";
import MaterialTypeSelect from "@/components/audit/MaterialTypeSelect.vue";
import StatusBadge from "@/components/StatusBadge.vue";
import type { MaterialTypeCatalog } from "@/composables/useMaterialTypeCatalog";
import type { StatusTone } from "@/constants/labels";

const props = defineProps<{
  document: AuditDocument;
  catalog: MaterialTypeCatalog;
  busy: boolean;
  disabled: boolean;
  needsAttention?: boolean;
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
  download: [document: AuditDocument];
  preview: [document: AuditDocument];
  remove: [document: AuditDocument];
  inspectProcessing: [document: AuditDocument];
}>();

const uploadRef = ref<{ clearFiles: () => void } | null>(null);
const selectedType = ref("");
const missingRequired = computed(() => props.document.required && !props.document.filename);
const materialClassify = computed(() => props.document.material_classify ?? null);
const matchedKeywords = computed(() => {
  const raw = materialClassify.value?.matched_keywords;
  return Array.isArray(raw) ? raw.filter((item): item is string => typeof item === "string") : [];
});

function materialStatusTone(status: string | null | undefined): StatusTone {
  if (status === "PREPARED" || status === "PROCESSED") return "success";
  if (status === "TYPE_REJECTED") return "danger";
  if (status === "TYPE_REVIEW" || status === "WAITING_UPLOAD") return "warning";
  if (status === "UPLOADED") return "info";
  return "neutral";
}

const statusLabelOverride = computed<string | null>(() => {
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

function onFileChange(file: { raw?: File }): void {
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
  if (props.document.filename && !window.confirm(
    `替换「${props.document.material_name}」当前文件后，已有的处理产物、字段结果、规则结果与报告会按服务端规则失效，需要重新执行相关节点。是否继续？`,
  )) return;
  emit("pick", file.raw, selectedType.value);
}
</script>

<template>
  <tr class="material-row" :class="{ 'material-row--attention': needsAttention && missingRequired }">
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
      <div class="material-row__status">
        <StatusBadge :status="document.status" :label="statusLabelOverride" :tone="materialStatusTone(document.status)" />
        <el-popover v-if="materialClassify" trigger="click" width="340">
          <template #reference><el-button link type="primary" size="small">识别详情</el-button></template>
          <div class="material-row__classification">
            <strong>自动识别（仅供参考）</strong>
            <div>判定类型：{{ materialClassify.detected_material_type || "未识别" }}</div>
            <div v-if="materialClassify.declared_material_type">上传声明：{{ materialClassify.declared_material_type }}</div>
            <div>置信度：{{ materialClassify.confidence ?? "—" }}</div>
            <div>命中关键词：{{ matchedKeywords.length ? matchedKeywords.join("、") : "无" }}</div>
          </div>
        </el-popover>
      </div>
      <div v-if="document.status === 'TYPE_REJECTED'" class="text-danger material-row__note">相关检查将记为无法判断</div>
      <div v-if="replacementImpact" class="material-row__note">
        {{ replacementImpact.description || "本次替换已使旧结果失效" }}；{{ impactText }}
      </div>
    </td>
    <td><MaterialTypeSelect v-model="selectedType" :catalog="catalog" :show-error="false" :disabled="disabled || busy" /></td>
    <td>
      <div class="material-row__actions">
        <el-upload ref="uploadRef" :auto-upload="false" :show-file-list="false" :disabled="disabled || busy" :on-change="onFileChange">
          <el-button size="small" type="primary" :loading="busy" :disabled="disabled || busy">
            <template #icon><Upload :size="14" /></template>
            {{ document.filename ? "替换" : "上传" }}
          </el-button>
        </el-upload>
        <template v-if="document.filename">
          <el-button link type="primary" size="small" :disabled="disabled || busy" @click="emit('inspectProcessing', document)">处理状态</el-button>
          <el-button link type="primary" size="small" :disabled="disabled || busy" @click="emit('preview', document)">
            <template #icon><Eye :size="14" /></template>预览
          </el-button>
          <el-button link type="primary" size="small" :disabled="disabled || busy" @click="emit('download', document)">
            <template #icon><Download :size="14" /></template>下载
          </el-button>
          <el-button link type="danger" size="small" :disabled="disabled || busy" @click="emit('remove', document)">移除</el-button>
        </template>
      </div>
    </td>
  </tr>
</template>

<style scoped>
.material-row--attention { background: var(--el-color-danger-light-9); }
.material-row__name,
.material-row__status,
.material-row__actions {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 6px;
}
.material-row__file { overflow-wrap: anywhere; }
.material-row__note { margin-top: 4px; line-height: 1.5; overflow-wrap: anywhere; }
.material-row__actions { gap: 4px 8px; }
.material-row__classification { line-height: 1.7; overflow-wrap: anywhere; }
</style>
