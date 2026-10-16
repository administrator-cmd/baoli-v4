<script setup lang="ts">
import { onMounted, ref } from "vue";
import { useRouter } from "vue-router";
import { ElMessage, ElMessageBox } from "element-plus";

import type { DocumentDetail, DocumentRow } from "@/api/contracts";
import { uploadTaskDocument } from "@/api/audits";
import { getDocument, listDocuments } from "@/api/materials";
import { usePagedList } from "@/composables/usePagedList";
import { useOperationTracker } from "@/composables/useOperationTracker";
import OperationPanel from "@/components/OperationPanel.vue";
import MaterialTypeSelect from "@/components/audit/MaterialTypeSelect.vue";
import PageHeader from "@/components/PageHeader.vue";
import { formatFileSize, statusLabel } from "@/constants/labels";
import StatusBadge from "@/components/StatusBadge.vue";

/**
 * 审核材料：跨任务的材料位与当前文件。
 * 上传/替换/移除都必须指定任务与材料位（不建设游离文件仓库）；
 * 替换与移除都会产生输入修订，历史文件保留为历史版本。
 */
const router = useRouter();

const list = usePagedList<DocumentRow, { task_no: string; keyword: string; material_name: string; has_file: string }>({
  key: "documents",
  initialFilters: { task_no: "", keyword: "", material_name: "", has_file: "" },
  fetch: (params) =>
    listDocuments({
      task_no: params.task_no || undefined,
      keyword: params.keyword || undefined,
      material_name: params.material_name || undefined,
      has_file: params.has_file === "" ? undefined : params.has_file === "true",
      page: params.page,
      page_size: params.page_size,
    }),
});

const { filters, page, pageSize, total, items, loading, error } = list;

function openTask(row: DocumentRow): void {
  void router.push({ name: "audit-task-detail", params: { taskNo: row.task_no }, query: { tab: "materials" } });
}

/* 上传 / 替换 */
const uploadVisible = ref(false);
const uploadRow = ref<DocumentRow | null>(null);
const uploadFile = ref<File | null>(null);
/** 本次上传声明的材料类型：用户从 8018 支持目录中选择，必填。 */
const uploadMaterialType = ref<string>("");
const uploadSaving = ref(false);
const uploadImpact = ref<string | null>(null);
/** 真实上传进度百分比；上传中由浏览器 XHR 进度事件驱动。 */
const uploadPercent = ref<number | null>(null);
const uploadError = ref<string | null>(null);
const classificationError = ref<string | null>(null);
const { operation: classificationOperation, watching: classificationWatching, track: trackClassification,
  requery: requeryClassification, reset: resetClassification } = useOperationTracker();
let uploadGeneration = 0;

function openUpload(row: DocumentRow): void {
  uploadGeneration += 1;
  uploadSaving.value = false;
  resetClassification();
  classificationError.value = null;
  uploadRow.value = row;
  uploadFile.value = null;
  uploadMaterialType.value = "";
  uploadImpact.value = null;
  uploadPercent.value = null;
  uploadError.value = null;
  uploadVisible.value = true;
}

function onFileChange(file: { raw?: File }): void {
  uploadFile.value = file.raw ?? null;
  uploadPercent.value = null;
  uploadError.value = null;
}

async function submitUpload(): Promise<void> {
  if (!uploadRow.value || !uploadFile.value) {
    ElMessage.warning("请选择文件");
    return;
  }
  // 材料类型必填：8018 的 doc_type 是严格枚举，没有类型就无法执行材料校验。
  if (!uploadMaterialType.value) {
    ElMessage.warning("请选择材料类型");
    return;
  }
  const row = uploadRow.value;
  const token = uploadGeneration;
  const replacing = Boolean(row.filename);
  if (replacing) {
    try {
      await ElMessageBox.confirm(
        `替换「${row.material_name}」当前文件后，相关处理产物、字段结果与规则结果会按服务端规则失效。是否继续？`,
        "替换材料文件",
        { type: "warning", confirmButtonText: "替换", cancelButtonText: "取消" },
      );
    } catch {
      return;
    }
  }
  if (token !== uploadGeneration) return;
  uploadSaving.value = true;
  uploadImpact.value = null;
  uploadError.value = null;
  uploadPercent.value = 0;
  let uploaded: Awaited<ReturnType<typeof uploadTaskDocument>>;
  try {
    uploaded = await uploadTaskDocument(
      row.task_no,
      row.document_no,
      uploadFile.value,
      uploadMaterialType.value,
      replacing,
      (percent) => {
        if (token === uploadGeneration) uploadPercent.value = percent;
      },
    );
  } catch (failure) {
    // 失败原因保留在弹窗里，不随提示消失。
    if (token === uploadGeneration) {
      uploadError.value = failure instanceof Error ? failure.message : "上传失败";
      uploadPercent.value = null;
    }
    return;
  } finally {
    if (token === uploadGeneration) uploadSaving.value = false;
  }
  if (token !== uploadGeneration) return;
  uploadPercent.value = 100;
  uploadImpact.value = uploaded.impact?.description ?? "文件已保存；自动校验状态见下方。";
  ElMessage.success(replacing ? "已替换为新的文件版本" : "文件已保存");
  uploadFile.value = null;
  uploadMaterialType.value = "";
  classificationError.value = null;
  trackClassification(uploaded.classification_operation, {
    onFinished: async () => { if (token === uploadGeneration) await list.load(); },
    onError: (failure) => {
      if (token === uploadGeneration) classificationError.value = failure instanceof Error ? failure.message : "校验状态查询失败";
    },
  });
  await list.load();
}

/* 历史版本 */
const detailVisible = ref(false);
const detailLoading = ref(false);
const detailError = ref<string | null>(null);
const detail = ref<DocumentDetail | null>(null);

async function openHistory(row: DocumentRow): Promise<void> {
  detailVisible.value = true;
  detailLoading.value = true;
  detailError.value = null;
  detail.value = null;
  try {
    detail.value = await getDocument(row.document_no);
  } catch (failure) {
    detailError.value = failure instanceof Error ? failure.message : "历史版本读取失败";
  } finally {
    detailLoading.value = false;
  }
}

onMounted(() => {
  void list.load();
});
</script>

<template>
  <div class="app-panel">
    <PageHeader
      eyebrow="资料管理"
      title="审核材料"
      :show-refresh="false"
    />
    <el-form inline class="app-query" @submit.prevent="list.search()">
      <el-form-item label="任务编号">
        <el-input v-model="filters.task_no" clearable placeholder="任务编号" style="width: 190px" @keyup.enter="list.search()" />
      </el-form-item>
      <el-form-item label="材料位">
        <el-input v-model="filters.material_name" clearable placeholder="材料位名称" style="width: 160px" @keyup.enter="list.search()" />
      </el-form-item>
      <el-form-item label="文件名称">
        <el-input v-model="filters.keyword" clearable placeholder="文件名 / 材料位" style="width: 180px" @keyup.enter="list.search()" />
      </el-form-item>
      <el-form-item label="文件状态">
        <el-select v-model="filters.has_file" clearable placeholder="全部" style="width: 140px">
          <el-option value="true" label="已上传" />
          <el-option value="false" label="未上传" />
        </el-select>
      </el-form-item>
      <el-form-item>
        <el-button type="primary" @click="list.search()">查询</el-button>
        <el-button @click="list.reset()">重置</el-button>
        <el-button :loading="loading" @click="list.load()">刷新</el-button>
      </el-form-item>
    </el-form>


    <el-alert v-if="error" type="error" show-icon :closable="false" :title="`读取失败：${error}`">
      <el-button size="small" @click="list.load()">重试</el-button>
    </el-alert>

    <el-table v-else :data="items" border stripe v-loading="loading" empty-text="暂无数据">
      <el-table-column label="任务" min-width="190" show-overflow-tooltip>
        <template #default="{ row }">
          <el-button link type="primary" class="table-link mono" @click="openTask(row)">{{ row.task_no }}</el-button>
          <div class="text-hint">{{ row.task_name }}</div>
        </template>
      </el-table-column>
      <el-table-column label="材料位" width="180" show-overflow-tooltip>
        <template #default="{ row }">
          {{ row.material_name }}
          <el-tag v-if="row.required" type="warning" size="small" effect="plain" disable-transitions>必传</el-tag>
        </template>
      </el-table-column>
      <el-table-column label="文件名" min-width="220" show-overflow-tooltip>
        <template #default="{ row }">{{ row.filename || "—（未上传）" }}</template>
      </el-table-column>
      <el-table-column label="文件版本" width="100">
        <template #default="{ row }">
          <span v-if="row.version_no">第 {{ row.version_no }} 版</span>
          <span v-else>—</span>
        </template>
      </el-table-column>
      <el-table-column label="大小" width="100">
        <template #default="{ row }">{{ row.size_bytes === null ? "—" : formatFileSize(row.size_bytes) }}</template>
      </el-table-column>
      <el-table-column label="上传时间" width="170">
        <template #default="{ row }">{{ row.uploaded_at ? String(row.uploaded_at).replace("T", " ").slice(0, 19) : "—" }}</template>
      </el-table-column>
      <el-table-column label="处理状态" width="130">
        <template #default="{ row }"><StatusBadge :status="row.status" /></template>
      </el-table-column>
      <el-table-column label="上传版本数" width="110">
        <template #default="{ row }">
          <span v-if="row.version_count">{{ row.version_count }} 个</span>
          <span v-else>—</span>
        </template>
      </el-table-column>
      <el-table-column label="操作" width="230" fixed="right">
        <template #default="{ row }">
          <el-button link type="primary" size="small" @click="openUpload(row)">{{ row.filename ? "替换" : "上传" }}</el-button>
          <el-button link type="primary" size="small" @click="openHistory(row)">历史版本</el-button>
        </template>
      </el-table-column>
    </el-table>

    <div class="app-pagination">
      <el-pagination
        :current-page="page"
        :page-size="pageSize"
        :total="total"
        :page-sizes="[10, 20, 50, 100]"
        layout="total, sizes, prev, pager, next, jumper"
        background
        @current-change="list.setPage"
        @size-change="list.setPageSize"
      />
    </div>

    <el-dialog v-model="uploadVisible" :title="uploadRow?.filename ? '替换材料文件' : '上传材料文件'" width="600px" :close-on-click-modal="false">
      <el-descriptions v-if="uploadRow" :column="1" border size="small">
        <el-descriptions-item label="任务">{{ uploadRow.task_name }}</el-descriptions-item>
        <el-descriptions-item label="材料位">{{ uploadRow.material_name }}{{ uploadRow.required ? "（必传）" : "（按需）" }}</el-descriptions-item>
        <el-descriptions-item label="当前文件">{{ uploadRow.filename || "未上传" }}</el-descriptions-item>
      </el-descriptions>
      <div class="block-gap-sm">
        <!-- 材料类型随本次上传声明：系统据此让 8018 校验文件是否放对材料位。 -->
        <MaterialTypeSelect v-model="uploadMaterialType" :disabled="uploadSaving" />
      </div>
      <el-upload class="block-gap-sm" drag :auto-upload="false" :limit="1" :on-change="onFileChange" :on-remove="() => (uploadFile = null)">
        <div class="upload-block__icon">⬆</div>
        <div class="el-upload__text">拖拽文件到此处，或<em>点击选择文件</em></div>
      </el-upload>
      <div v-if="uploadSaving && uploadPercent !== null" class="block-gap-sm">
        <el-progress :percentage="uploadPercent" :status="uploadPercent >= 100 ? 'success' : undefined" />
        <span class="text-hint">
          {{ uploadPercent >= 100 ? "文件已发送，正在等待服务端处理并保存。" : "正在上传文件…" }}
        </span>
      </div>
      <el-alert v-if="uploadImpact" class="block-gap-sm" type="warning" show-icon :closable="false" :title="uploadImpact" />
      <el-alert v-if="uploadError" class="block-gap-sm" type="error" show-icon :closable="false" :title="`上传失败：${uploadError}`" />
      <OperationPanel v-if="classificationOperation" class="block-gap-sm" compact
        :operation="classificationOperation" :watching="classificationWatching" label="文件已保存，自动校验"
        @requery="classificationError = null; requeryClassification()" />
      <el-alert v-if="classificationError" class="block-gap-sm" type="error" show-icon :closable="false"
        :title="`文件已保存；校验状态查询失败：${classificationError}`" />
      <template #footer>
        <el-button @click="uploadVisible = false">关闭</el-button>
        <el-button type="primary" :loading="uploadSaving" @click="submitUpload">
          {{ uploadRow?.filename ? "确认替换" : "上传" }}
        </el-button>
      </template>
    </el-dialog>

    <el-drawer v-model="detailVisible" title="材料详情与历史版本" size="640px">
      <el-alert v-if="detailError" type="error" show-icon :closable="false" :title="detailError" />
      <div v-else v-loading="detailLoading">
        <template v-if="detail">
          <el-descriptions :column="2" border size="small">
            <el-descriptions-item label="任务">{{ detail.task_name }}</el-descriptions-item>
            <el-descriptions-item label="材料位">{{ detail.material_name }}{{ detail.required ? "（必传）" : "（按需）" }}</el-descriptions-item>
            <el-descriptions-item label="处理状态"><StatusBadge :status="detail.status" /></el-descriptions-item>
            <el-descriptions-item label="当前版本">
              {{ detail.uploads.find((item) => item.is_current)?.version_no ?? "—" }}
            </el-descriptions-item>
          </el-descriptions>
          <h4 class="app-section-title block-gap-sm">上传历史（版本从新到旧）</h4>
          <el-table :data="detail.uploads" border size="small" empty-text="暂无上传">
            <el-table-column label="版本" width="70">
              <template #default="{ row }">第 {{ row.version_no }} 版</template>
            </el-table-column>
            <el-table-column prop="filename" label="文件名" min-width="200" show-overflow-tooltip />
            <el-table-column label="大小" width="100">
              <template #default="{ row }">{{ formatFileSize(row.size_bytes) }}</template>
            </el-table-column>
            <el-table-column label="状态" width="110">
              <template #default="{ row }">
                <StatusBadge :status="row.status" :label="row.is_current ? '当前' : statusLabel(row.status)" />
              </template>
            </el-table-column>
            <el-table-column label="上传时间" width="160">
              <template #default="{ row }">{{ String(row.created_at).replace("T", " ").slice(0, 19) }}</template>
            </el-table-column>
          </el-table>
        </template>
      </div>
    </el-drawer>
  </div>
</template>
