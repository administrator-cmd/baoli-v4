<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from "vue";
import { ElMessage, type UploadFile, type UploadRawFile } from "element-plus";

import type { OperationAccepted, ReportStyleProfile, ReportTemplate } from "@/api/contracts";
import {
  getOperation,
  isOperationActive,
  operationIdempotencyKey,
  watchOperation,
  type OperationWatch,
} from "@/api/http";
import {
  listReportTemplates,
  reanalyzeReportTemplate,
  reportTemplatePdfUrl,
  updateReportTemplate,
  uploadReportTemplate,
} from "@/api/reportTemplates";
import { formatDate } from "@/constants/labels";

const items = ref<ReportTemplate[]>([]);
const loading = ref(false);
const error = ref<string | null>(null);
const operationQueryFailed = ref<Record<string, boolean>>({});
const operationWatches = new Map<string, OperationWatch>();
const reanalysisKeys = new Map<string, string>();

function applyOperation(operation: OperationAccepted): void {
  items.value = items.value.map((item) => item.template_no === operation.object_no
    ? { ...item, latest_operation: operation }
    : item);
}

function trackOperation(operation: OperationAccepted): void {
  applyOperation(operation);
  operationQueryFailed.value = { ...operationQueryFailed.value, [operation.object_no]: false };
  if (!isOperationActive(operation.status) || operationWatches.has(operation.operation_no)) return;
  const watch = watchOperation(
    operation.operation_no,
    (current) => {
      applyOperation(current);
      if (!isOperationActive(current.status)) {
        operationWatches.get(current.operation_no)?.stop();
        operationWatches.delete(current.operation_no);
        reanalysisKeys.delete(current.object_no);
        void load().then(() => {
          if (current.status === "SUCCEEDED") ElMessage.success("报告模板风格档案已更新");
          else ElMessage.warning(operationFailureMessage(current));
        });
      }
    },
    () => {
      operationWatches.get(operation.operation_no)?.stop();
      operationWatches.delete(operation.operation_no);
      operationQueryFailed.value = { ...operationQueryFailed.value, [operation.object_no]: true };
    },
  );
  operationWatches.set(operation.operation_no, watch);
}

function operationFailureMessage(operation: OperationAccepted): string {
  const template = items.value.find((item) => item.template_no === operation.object_no);
  if (operation.failure_message) return operation.failure_message;
  return template?.style_profile ? "风格更新未完成，原档案已保留" : "风格分析未完成，可重新分析";
}

function announceAcceptedOperation(operation: OperationAccepted, action: "upload" | "reanalyze"): void {
  trackOperation(operation);
  if (isOperationActive(operation.status)) {
    ElMessage.success(action === "upload" ? "报告模板已保存，风格分析正在后台进行" : "已开始后台分析；分析失败时会保留当前档案");
  } else if (operation.status === "SUCCEEDED") {
    ElMessage.success(action === "upload" ? "报告模板已分析并保存" : "风格档案已重新生成");
  } else {
    ElMessage.warning(operationFailureMessage(operation));
  }
}

async function load(): Promise<void> {
  loading.value = true;
  error.value = null;
  try {
    items.value = (await listReportTemplates()).items;
    for (const item of items.value) {
      if (item.latest_operation && isOperationActive(item.latest_operation.status)) {
        trackOperation(item.latest_operation);
      }
    }
  } catch (failure) {
    error.value = failure instanceof Error ? failure.message : "报告模板读取失败";
  } finally {
    loading.value = false;
  }
}

async function queryOperation(row: ReportTemplate): Promise<void> {
  const operationNo = row.latest_operation?.operation_no;
  if (!operationNo) return;
  try {
    const operation = await getOperation(operationNo);
    operationQueryFailed.value = { ...operationQueryFailed.value, [row.template_no]: false };
    if (isOperationActive(operation.status)) trackOperation(operation);
    else {
      applyOperation(operation);
      await load();
    }
  } catch (failure) {
    operationQueryFailed.value = { ...operationQueryFailed.value, [row.template_no]: true };
    ElMessage.error(failure instanceof Error ? failure.message : "状态查询失败，请重试");
  }
}

const uploadVisible = ref(false);
const uploadName = ref("");
const uploadFile = ref<UploadRawFile | null>(null);
const uploadSaving = ref(false);
const uploadProgress = ref(0);
const uploadIdempotencyKey = ref(operationIdempotencyKey("REPORT_TEMPLATE_UPLOAD"));

function onFileChange(file: UploadFile): void {
  uploadFile.value = file.raw ?? null;
  uploadIdempotencyKey.value = operationIdempotencyKey("REPORT_TEMPLATE_UPLOAD");
  if (!uploadName.value && file.name) uploadName.value = file.name.replace(/\.pdf$/i, "");
}

function clearUpload(): void {
  uploadName.value = "";
  uploadFile.value = null;
  uploadProgress.value = 0;
  uploadIdempotencyKey.value = operationIdempotencyKey("REPORT_TEMPLATE_UPLOAD");
}

function rotateUploadIdempotencyKey(): void {
  uploadIdempotencyKey.value = operationIdempotencyKey("REPORT_TEMPLATE_UPLOAD");
}

async function submitUpload(): Promise<void> {
  if (!uploadFile.value) {
    ElMessage.warning("请选择 PDF 文件");
    return;
  }
  uploadSaving.value = true;
  uploadProgress.value = 0;
  try {
    const result = await uploadReportTemplate(uploadFile.value, uploadName.value, uploadIdempotencyKey.value, (percent) => {
      uploadProgress.value = percent;
    });
    uploadVisible.value = false;
    clearUpload();
    await load();
    announceAcceptedOperation(result.operation, "upload");
  } catch (failure) {
    ElMessage.error(failure instanceof Error ? failure.message : "上传失败");
  } finally {
    uploadSaving.value = false;
  }
}

const editVisible = ref(false);
const editRow = ref<ReportTemplate | null>(null);
const editName = ref("");
const profileText = ref("");
const editSaving = ref(false);
const profileError = ref<string | null>(null);
const pdfVisible = ref(false);
const pdfRow = ref<ReportTemplate | null>(null);
const pdfUrl = computed(() => pdfRow.value ? reportTemplatePdfUrl(pdfRow.value.template_no) : "");
const editLocked = computed(() => Boolean(
  editRow.value && items.value.some((row) => row.template_no === editRow.value?.template_no && isAnalyzing(row)),
));

function openEdit(row: ReportTemplate): void {
  editRow.value = row;
  editName.value = row.name;
  profileText.value = row.style_profile ? JSON.stringify(row.style_profile, null, 2) : "";
  profileError.value = null;
  editVisible.value = true;
}

function previewPdf(row: ReportTemplate): void {
  pdfRow.value = row;
  pdfVisible.value = true;
}

async function submitEdit(): Promise<void> {
  if (!editRow.value) return;
  if (editLocked.value) {
    profileError.value = "风格分析完成后才能保存档案";
    return;
  }
  let profile: ReportStyleProfile | null = null;
  if (editRow.value.style_profile) {
    try {
      const parsed: unknown = JSON.parse(profileText.value);
      if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
        profileError.value = "风格 JSON 必须是对象，不能为 null、数组或其他值";
        return;
      }
      profile = parsed as ReportStyleProfile;
    } catch {
      profileError.value = "JSON 格式无效，请检查括号、引号和逗号";
      return;
    }
  }
  editSaving.value = true;
  profileError.value = null;
  try {
    await updateReportTemplate(editRow.value.template_no, {
      name: editName.value.trim(),
      style_profile: profile,
    });
    ElMessage.success("报告模板已保存");
    editVisible.value = false;
    await load();
  } catch (failure) {
    profileError.value = failure instanceof Error ? failure.message : "保存失败";
  } finally {
    editSaving.value = false;
  }
}

const reanalyzing = ref<string | null>(null);
async function reanalyze(row: ReportTemplate): Promise<void> {
  if (isAnalyzing(row) || reanalyzing.value === row.template_no) return;
  const idempotencyKey = reanalysisKeys.get(row.template_no)
    ?? operationIdempotencyKey("REPORT_TEMPLATE_ANALYZE", row.template_no);
  reanalysisKeys.set(row.template_no, idempotencyKey);
  reanalyzing.value = row.template_no;
  try {
    const operation = await reanalyzeReportTemplate(row.template_no, idempotencyKey);
    await load();
    if (!isOperationActive(operation.status)) reanalysisKeys.delete(row.template_no);
    announceAcceptedOperation(operation, "reanalyze");
  } catch (failure) {
    await load();
    reanalysisKeys.delete(row.template_no);
    const latest = items.value.find((item) => item.template_no === row.template_no)?.latest_operation;
    if (latest && isOperationActive(latest.status)) {
      trackOperation(latest);
      ElMessage.info("该模板已有分析正在进行，已接续查询进度");
    } else {
      ElMessage.error(failure instanceof Error ? failure.message : "重新分析失败");
    }
  } finally {
    reanalyzing.value = null;
  }
}

function isAnalyzing(row: ReportTemplate): boolean {
  return row.latest_operation?.operation_type === "REPORT_TEMPLATE_ANALYZE"
    && isOperationActive(row.latest_operation.status);
}

function statusLabel(row: ReportTemplate): string {
  if (isAnalyzing(row)) return "风格更新中";
  if (row.status === "READY" && row.analysis_error) return "可用，更新失败";
  if (row.status === "READY" && row.latest_operation?.status === "INTERRUPTED") return "档案可用，更新中断";
  if (row.status === "PROCESSING" && row.latest_operation
    && ["FAILED", "INTERRUPTED"].includes(row.latest_operation.status)) return "分析失败";
  return row.status === "READY" ? "可使用" : row.status === "PROCESSING" ? "处理中" : "分析失败";
}

function statusType(row: ReportTemplate): "success" | "warning" | "danger" {
  if (isAnalyzing(row) || row.analysis_error) return "warning";
  if (row.status === "READY" && row.latest_operation?.status === "INTERRUPTED") return "warning";
  if (row.status === "PROCESSING" && row.latest_operation
    && ["FAILED", "INTERRUPTED"].includes(row.latest_operation.status)) return "danger";
  return row.status === "READY" ? "success" : row.status === "PROCESSING" ? "warning" : "danger";
}

function statusHint(row: ReportTemplate): string | null {
  if (isAnalyzing(row) && row.style_profile) return "正在重新分析，现有档案仍可使用";
  return row.analysis_error || row.latest_operation?.failure_message || null;
}

function formatSize(size: number): string {
  return size < 1024 * 1024 ? `${(size / 1024).toFixed(0)} KB` : `${(size / 1024 / 1024).toFixed(1)} MB`;
}

onMounted(() => void load());
onUnmounted(() => {
  for (const watch of operationWatches.values()) watch.stop();
  operationWatches.clear();
});
</script>

<template>
  <div class="app-panel">
    <div class="page-heading">
      <div><h2>报告模板</h2><p>上传成品 PDF，提炼可复用的章节结构与表达风格。</p></div>
      <div class="page-heading__actions">
        <el-button :loading="loading" @click="load">刷新</el-button>
        <el-button type="primary" @click="uploadVisible = true">上传报告模板</el-button>
      </div>
    </div>

    <el-alert v-if="error" type="error" show-icon :closable="false" :title="error">
      <el-button size="small" @click="load">重试</el-button>
    </el-alert>

    <el-table v-else :data="items" border stripe class="management-table" v-loading="loading" empty-text="暂无报告模板">
      <el-table-column label="模板名称" min-width="200" show-overflow-tooltip>
        <template #default="{ row }"><span>{{ row.name }}</span></template>
      </el-table-column>
      <el-table-column label="PDF 文件" min-width="240" show-overflow-tooltip prop="source_filename" />
      <el-table-column label="页数" width="90" align="center" prop="page_count" />
      <el-table-column label="文件大小" width="120">
        <template #default="{ row }">{{ formatSize(row.source_size) }}</template>
      </el-table-column>
      <el-table-column label="状态" width="150">
        <template #default="{ row }">
          <el-tooltip v-if="statusHint(row)" :content="statusHint(row) || ''" placement="top">
            <el-tag :type="statusType(row)" effect="light">{{ statusLabel(row) }}</el-tag>
          </el-tooltip>
          <el-tag v-else :type="statusType(row)" effect="light">{{ statusLabel(row) }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column label="更新时间" width="200">
        <template #default="{ row }">{{ formatDate(row.updated_at) }}</template>
      </el-table-column>
      <el-table-column label="操作" width="260" class-name="table-actions" fixed="right">
        <template #default="{ row }">
          <el-button link type="primary" size="small" @click="previewPdf(row)">查看 PDF</el-button>
          <el-button link type="primary" size="small" @click="openEdit(row)">{{ row.style_profile ? "查看 / 编辑" : "修改名称" }}</el-button>
          <el-button link type="primary" size="small" :loading="reanalyzing === row.template_no" :disabled="isAnalyzing(row)" @click="reanalyze(row)">重新分析</el-button>
          <el-button v-if="isAnalyzing(row) && operationQueryFailed[row.template_no]" link type="primary" size="small" @click="queryOperation(row)">再次查询</el-button>
        </template>
      </el-table-column>
    </el-table>

    <el-dialog
      v-model="pdfVisible"
      :title="`${pdfRow?.name ?? '报告模板'} · PDF 预览`"
      width="min(1100px, 92vw)"
      align-center
      append-to-body
      destroy-on-close
      class="report-template-pdf-dialog"
    >
      <iframe v-if="pdfUrl" :src="pdfUrl" :title="`${pdfRow?.source_filename ?? '报告模板'} PDF 预览`" class="report-template-pdf-frame" />
    </el-dialog>

    <el-dialog v-model="uploadVisible" title="上传报告模板" width="560px" :close-on-click-modal="false" @closed="clearUpload">
      <el-form label-width="90px">
        <el-form-item label="模板名称">
          <el-input v-model="uploadName" maxlength="255" placeholder="默认使用 PDF 文件名" :disabled="uploadSaving" @input="rotateUploadIdempotencyKey" />
        </el-form-item>
        <el-form-item label="PDF 文件" required>
          <el-upload drag accept=".pdf,application/pdf" :auto-upload="false" :limit="1" :disabled="uploadSaving" :on-change="onFileChange" :on-remove="() => { uploadFile = null }">
            <div class="upload-copy">拖入 PDF，或点击选择文件</div>
            <template #tip><div class="el-upload__tip">PDF 将提交至已配置的 PaddleOCR 在线服务识别。</div></template>
          </el-upload>
        </el-form-item>
      </el-form>
      <el-progress v-if="uploadSaving" :percentage="uploadProgress" />
      <template #footer>
        <el-button @click="uploadVisible = false">取消</el-button>
        <el-button type="primary" :loading="uploadSaving" @click="submitUpload">上传并分析</el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="editVisible" :title="editRow?.style_profile ? '报告风格档案' : '修改模板名称'" width="760px" :close-on-click-modal="false">
      <el-form label-width="90px">
        <el-form-item label="模板名称" required><el-input v-model="editName" maxlength="255" /></el-form-item>
        <el-form-item v-if="editRow?.style_profile" label="风格 JSON" required>
          <el-input v-model="profileText" type="textarea" :rows="20" spellcheck="false" class="json-editor" />
        </el-form-item>
      </el-form>
      <el-alert v-if="editLocked" type="warning" :closable="false" title="模板正在重新分析，当前档案仍可用；分析完成后可再保存修改。" />
      <el-alert v-if="profileError" type="error" :closable="false" :title="profileError" />
      <template #footer>
        <el-button @click="editVisible = false">取消</el-button>
        <el-button type="primary" :loading="editSaving" :disabled="editLocked" @click="submitEdit">{{ editRow?.style_profile ? "保存档案" : "保存名称" }}</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.page-heading { display: flex; justify-content: space-between; align-items: center; gap: 16px; margin-bottom: 18px; }
.page-heading h2 { margin: 0; font-size: 20px; }
.page-heading p { margin: 6px 0 0; color: var(--el-text-color-secondary); }
.page-heading__actions { display: flex; gap: 8px; }
.upload-copy { padding: 16px 0; }
.json-editor :deep(textarea) { font-family: Consolas, "Microsoft YaHei", monospace; line-height: 1.55; }
.report-template-pdf-frame { display: block; width: 100%; height: min(76vh, 900px); border: 0; }
.report-template-pdf-dialog :deep(.el-dialog__body) { padding-top: 0; }
@media (max-width: 720px) {
  .page-heading { align-items: flex-start; flex-direction: column; }
}
</style>
