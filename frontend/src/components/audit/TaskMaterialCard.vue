<script setup lang="ts">
import { computed, ref } from "vue";
import { ElMessage } from "element-plus";
import { Download, Eye, Upload } from "lucide-vue-next";

import type { AuditDocument } from "@/api/contracts";
import StatusBadge from "@/components/StatusBadge.vue";
import type { StatusTone } from "@/constants/labels";

/**
 * 一个材料位的上传卡片。
 *
 * 阅读顺序：材料名称与必传属性 → 当前文件 → 材料处理状态 → 缺失或失败原因 →
 * 可用操作。业务信息优先；本地路径、内部编号、页码一律不显示（上传编号只在
 * 下载/预览地址里使用，不作为展示内容）。
 *
 * 8018 的识别结论（判定类型、置信度、命中关键词）原样显示：它不参与任何判定，
 * 不改变材料状态，也不阻断后续节点执行，因此放在次级位置并明确标注。
 */
const props = defineProps<{
  document: AuditDocument;
  /** 上传请求进行中：该卡片禁止重复操作。 */
  busy: boolean;
  /** 页面其它操作进行中。 */
  disabled: boolean;
  /**
   * 替换该材料会失效哪些结果。
   * 由服务端在替换后返回真实影响范围；没有返回时不编造数字。
   */
  replacementImpact?: {
    stale_field_values: number;
    invalidated_rule_results: number;
    invalidated_node_executions: number;
    invalidated_reviews: number;
    description?: string;
  } | null;
  /**
   * 该材料位是否需要用户现在处理（必传文件缺失）。
   * 由父页面按当前节点判定；缺省时不强调，不影响其余信息的展示。
   */
  needsAttention?: boolean;
}>();

const emit = defineEmits<{
  pick: [file: File];
  /** 打开 OCR 查询 / 对账入口（状态未知或作业过期时需要）。 */
  ocr: [document: AuditDocument];
  /** 下载当前文件（历史版本在材料详情里查看）。 */
  download: [document: AuditDocument];
  /** 预览当前文件（仅浏览器支持的格式会真正预览）。 */
  preview: [document: AuditDocument];
  /** 移除当前文件（输入修订：旧文件保留历史，依赖结果失效）。 */
  remove: [document: AuditDocument];
}>();

const uploadRef = ref<{ clearFiles: () => void } | null>(null);

/**
 * 材料处理状态的语义色。
 *
 * 与节点状态分开：材料位有自己的一组状态码，「已上传」是蓝、「已处理」是绿、
 * 「待上传」是橙、「材料类型不符」是红、「已替换」是灰。用节点的配色表会错配——
 * 材料位上的 `UPLOADED` 在节点表里根本没有对应项。
 */
function materialStatusTone(status: string | null | undefined): StatusTone {
  if (status === "PREPARED" || status === "PROCESSED") {
    return "success";
  }
  if (status === "TYPE_REJECTED") {
    return "danger";
  }
  if (status === "TYPE_REVIEW" || status === "TYPE_MISMATCH") {
    return "warning";
  }
  if (status === "WAITING_UPLOAD") {
    return "warning";
  }
  if (status === "UPLOADED") {
    return "info";
  }
  return "neutral";
}

const missingRequired = computed(() => props.document.required && !props.document.filename);

/** 该材料位的 8018 识别结论；材料处理节点尚未执行时为 null。 */
const materialClassify = computed(() => props.document.material_classify ?? null);

/** 命中关键词原文；服务返回的不是字符串列表时按「无」处理，不臆造内容。 */
const matchedKeywords = computed<string[]>(() => {
  const raw = materialClassify.value?.matched_keywords;
  return Array.isArray(raw) ? raw.filter((item): item is string => typeof item === "string") : [];
});

/**
 * 材料处理状态。
 *
 * 「已上传」只表示文件到了服务端；「已处理」才表示文件处理节点跑完并产出了可审核的内容。
 * 「材料类型待复核」「材料类型不符」是另外两件事：前者尚未由人定案，后者是人工已确认不符。
 * 它们必须分开显示，不能统一写成「处理中」。
 */
const statusLabelOverride = computed<string | null>(() => {
  if (props.document.status === "TYPE_REVIEW") {
    return "材料类型待复核";
  }
  if (props.document.status === "TYPE_REJECTED") {
    return "材料类型不符";
  }
  if (!props.document.filename) {
    return "待上传";
  }
  if (props.document.status === "UPLOADED") {
    return "已上传，待文件处理";
  }
  return null;
});

/** 当前状态对业务的含义：为什么要关心它，以及它不表示什么。 */
const statusDescription = computed(() => {
  if (!props.document.filename) {
    return props.document.required
      ? "该材料位是必传项，尚未上传文件；依赖它的检查不会因此被当作通过。"
      : "该材料位是按需项，尚未上传；未上传时依赖它的规则按适用条件处理。";
  }
  if (props.document.status === "TYPE_REVIEW") {
    return "该材料位的自动识别类型结论尚未由人定案。文件内容照常参与审核，类型本身会形成一条待复核事项。";
  }
  if (props.document.status === "TYPE_REJECTED") {
    return "人工已确认该文件与材料位不符。依赖它的检查缺少有效依据，会明确记为无法判断。";
  }
  if (props.document.status === "UPLOADED") {
    return "文件已上传，但文件处理节点尚未完成，还没有可供审核的处理产物。";
  }
  if (props.document.status === "PREPARED" || props.document.status === "PROCESSED") {
    return "文件处理已完成，可供字段抽取与规则检查使用。处理完成不代表内容符合审核要求。";
  }
  return null;
});

/** 缺失或失败原因：只有服务端真实给出时才显示，不臆造。 */
const failureReason = computed(() => {
  if (missingRequired.value) {
    return "必传材料尚未上传：材料检查节点会指出缺少这项材料，审核结果不会因此被当作通过。";
  }
  return null;
});

/** 替换后的真实影响范围；没有返回时不显示数字。 */
const impactText = computed(() => {
  const impact = props.replacementImpact;
  if (!impact) {
    return null;
  }
  return `失效字段结果 ${impact.stale_field_values} 项 · 失效规则结果 ${impact.invalidated_rule_results} 条 · 失效节点执行 ${impact.invalidated_node_executions} 次 · 需重新复核 ${impact.invalidated_reviews} 项`;
});

/** 当前下一步操作：只有一个主操作，其余收在次级按钮。 */
const nextAction = computed(() => {
  if (!props.document.filename) {
    return props.document.required ? "上传必传材料" : "上传材料文件";
  }
  if (props.document.status === "UPLOADED") {
    return "等待文件处理节点执行";
  }
  return null;
});

function onFileChange(file: { raw?: File }): void {
  uploadRef.value?.clearFiles();
  if (!file.raw) {
    return;
  }
  if (props.document.filename) {
    const confirmed = window.confirm(
      `替换「${props.document.material_name}」当前文件后，已有的处理产物、字段结果、规则结果与报告会按服务端规则失效，需要重新执行相关节点。是否继续？`,
    );
    if (!confirmed) {
      ElMessage.info("已取消替换，当前文件保持不变。");
      return;
    }
  }
  emit("pick", file.raw);
}
</script>

<template>
  <el-card shadow="never" class="material-item" :class="{ 'material-item--attention': needsAttention && missingRequired }">
    <div class="material-item__title">
      <span>{{ document.material_name }}</span>
      <el-tag
        :type="document.required ? (missingRequired ? 'danger' : 'warning') : 'info'"
        size="small"
        effect="plain"
        disable-transitions
      >
        {{ document.required ? "必传" : "按需" }}
      </el-tag>
    </div>

    <!-- 当前文件 -->
    <div class="material-item__file">
      <template v-if="document.filename">
        <span class="text-muted">当前文件：</span><strong>{{ document.filename }}</strong>
      </template>
      <template v-else>
        <span class="text-muted">该材料位还没有文件。</span>
      </template>
    </div>

    <!-- 材料处理状态，以及它对业务意味着什么 -->
    <div class="material-item__status">
      <StatusBadge :status="document.status" :label="statusLabelOverride" :tone="materialStatusTone(document.status)" />
      <span v-if="statusDescription" class="material-item__status-text">{{ statusDescription }}</span>
    </div>

    <!-- 缺失或失败原因 -->
    <el-alert v-if="failureReason" class="block-gap-sm" type="error" show-icon :closable="false" title="缺件">
      {{ failureReason }}
    </el-alert>
    <el-alert
      v-if="document.status === 'TYPE_REJECTED'"
      class="block-gap-sm"
      type="error"
      show-icon
      :closable="false"
      title="材料类型已被人工确认与材料位不符"
    >
      依赖该材料的检查会明确记为无法判断，不会显示成通过。
    </el-alert>

    <!-- 替换后会失效的内容 -->
    <el-alert
      v-if="replacementImpact"
      class="block-gap-sm"
      type="warning"
      show-icon
      :closable="false"
      title="本次变更已使以下结果失效（服务端计算）"
    >
      <div>{{ replacementImpact.description || "旧的处理产物与结果不再进入当前报告。" }}</div>
      <div v-if="impactText">{{ impactText }}</div>
    </el-alert>

    <!-- 8018 识别结论：原样显示，不参与判定，也不阻断本材料的后续执行 -->
    <div v-if="materialClassify" class="material-classify block-gap-sm">
      <div class="material-classify__title">自动识别结论（仅作参考，不参与判定）</div>
      <div>判定类型：{{ materialClassify.detected_material_type || "未识别" }}
        <template v-if="materialClassify.declared_material_type">
          （材料位声明：{{ materialClassify.declared_material_type }}）
        </template>
      </div>
      <div>置信度：{{ materialClassify.confidence ?? "—" }}</div>
      <div>
        命中关键词：
        <template v-if="matchedKeywords.length">{{ matchedKeywords.join("、") }}</template>
        <template v-else>无</template>
      </div>
      <div class="text-hint">置信度高低不改变流程走向，也不改变材料状态。</div>
    </div>

    <!-- 当前下一步操作 -->
    <div v-if="nextAction" class="material-item__next">{{ nextAction }}</div>

    <div class="material-item__footer">
      <div class="card-actions">
        <el-upload
          ref="uploadRef"
          :auto-upload="false"
          :show-file-list="false"
          :disabled="disabled || busy"
          :on-change="onFileChange"
        >
          <el-button size="small" type="primary" :loading="busy" :disabled="disabled || busy">
            <template #icon><Upload :size="14" /></template>
            {{ document.filename ? "替换文件" : "上传文件" }}
          </el-button>
        </el-upload>
        <template v-if="document.filename">
          <el-button link type="primary" size="small" :disabled="disabled || busy" @click="emit('preview', document)">
            <template #icon><Eye :size="14" /></template>
            预览
          </el-button>
          <el-button link type="primary" size="small" :disabled="disabled || busy" @click="emit('download', document)">
            <template #icon><Download :size="14" /></template>
            下载
          </el-button>
          <el-dropdown trigger="click">
            <el-button link type="primary" size="small" :disabled="disabled || busy">更多</el-button>
            <template #dropdown>
              <el-dropdown-menu>
                <el-dropdown-item @click="emit('ocr', document)">OCR 查询与对账</el-dropdown-item>
                <el-dropdown-item divided @click="emit('remove', document)">移除当前文件</el-dropdown-item>
              </el-dropdown-menu>
            </template>
          </el-dropdown>
        </template>
      </div>
    </div>
  </el-card>
</template>

<style scoped>
.material-item--attention {
  border-color: var(--el-color-danger-light-5);
}

.material-item__title {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
  font-weight: 600;
}

.material-item__file {
  margin-top: 6px;
  overflow-wrap: anywhere;
}

.material-item__status {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
  margin-top: 8px;
}

.material-item__status-text {
  color: var(--el-text-color-secondary);
  line-height: 1.6;
}

.material-classify {
  padding: 8px 10px;
  background: var(--el-fill-color-light);
  border-radius: 4px;
  line-height: 1.7;
}

.material-classify__title {
  font-weight: 600;
  margin-bottom: 2px;
}

.material-item__next {
  margin-top: 8px;
  color: var(--el-color-primary);
  font-weight: 600;
}

.material-item__footer {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: 12px;
  margin-top: 12px;
  flex-wrap: wrap;
}

@media (max-width: 768px) {
  .material-item__footer {
    justify-content: flex-start;
  }
}
</style>
