<script setup lang="ts">
import { onMounted, ref } from "vue";
import { useRouter } from "vue-router";
import { ElMessage, ElMessageBox } from "element-plus";

import {
  abortImportJob,
  createImportJob,
  deleteImportJob,
  importJobsExportUrl,
  listImportJobsPage,
  listRulebookTemplateExamples,
  rulebookTemplateUrl,
  type ImportJobRow,
  type RulebookTemplateExampleItem,
} from "@/api/imports";
import { usePagedList } from "@/composables/usePagedList";
import PageHeader from "@/components/PageHeader.vue";
import { statusLabel } from "@/constants/labels";

/**
 * 规则库导入（入口列表）：文件名、目标场景、状态与待处理数来自真实作业。
 * 新增导入后进入详情五步（文件/工作表/规则确认/发布预览/发布结果）；
 * 已发布作业不能删除（保留版本来源追溯），服务端会明确说明原因。
 */
const router = useRouter();

const list = usePagedList<ImportJobRow, { filename: string; status: string; created_from: string; created_to: string }>({
  key: "scene-imports",
  initialFilters: { filename: "", status: "", created_from: "", created_to: "" },
  fetch: (params) =>
    listImportJobsPage({
      filename: params.filename || undefined,
      status: params.status || undefined,
      created_from: params.created_from || undefined,
      created_to: params.created_to || undefined,
      page: params.page,
      page_size: params.page_size,
    }),
});

const { filters, page, pageSize, total, items, loading, error } = list;

/** 正在中止的作业号；中止是同步请求，用它给对应行的按钮显示 loading。 */
const abortingJobNo = ref("");

const statusOptions = [
  { value: "PARSED", label: "已解析" },
  { value: "PROPOSED", label: "已生成候选" },
  { value: "PENDING_DECISION", label: "待裁决" },
  { value: "CONFIRMED", label: "已确认" },
  { value: "PRECHECKED", label: "已预检查" },
  { value: "PUBLISHED", label: "已发布" },
];

function exportUrl(): string {
  return importJobsExportUrl({
    filename: filters.filename || undefined,
    status: filters.status || undefined,
    created_from: filters.created_from || undefined,
    created_to: filters.created_to || undefined,
  });
}

function openJob(row: ImportJobRow): void {
  void router.push({ name: "scene-import-detail", params: { jobNo: row.job_no } });
}

/* ---- 新增导入 ---- */
const createVisible = ref(false);
const selectedFile = ref<File | null>(null);
const uploading = ref(false);

function onFileChange(file: { raw?: File }): void {
  selectedFile.value = file.raw ?? null;
}

async function submitCreate(): Promise<void> {
  if (!selectedFile.value) {
    ElMessage.warning("请选择规则库 Excel 文件");
    return;
  }
  uploading.value = true;
  try {
    const job = await createImportJob(selectedFile.value);
    ElMessage.success("导入作业已创建，请继续完成工作表识别与规则确认");
    createVisible.value = false;
    selectedFile.value = null;
    await router.push({ name: "scene-import-detail", params: { jobNo: job.job_no } });
  } catch (failure) {
    ElMessage.error(failure instanceof Error ? failure.message : "上传失败");
  } finally {
    uploading.value = false;
  }
}

async function removeJob(row: ImportJobRow): Promise<void> {
  try {
    await ElMessageBox.confirm(
      `确认删除导入作业「${row.source_filename}」？仅未发布的草稿作业可删除；已发布作业会保留来源追溯。`,
      "删除导入作业",
      { type: "warning", confirmButtonText: "删除", cancelButtonText: "取消" },
    );
  } catch {
    return;
  }
  try {
    await deleteImportJob(row.job_no);
    ElMessage.success("导入作业已删除");
    await list.load();
  } catch (failure) {
    ElMessage.error(failure instanceof Error ? failure.message : "删除失败");
  }
}

function targetText(row: ImportJobRow): string {
  if (!row.target_name) {
    return "—（发布前选择）";
  }
  const mode = row.target_mode === "update" ? "更新" : row.target_mode === "new" ? "新建" : "";
  return mode ? `${row.target_name}（${mode}）` : row.target_name;
}

/**
 * 中止该作业未结束的后台转换操作。
 * 这是「释放占用」而不是删除：作业状态与草稿数据都不改，中止后可重新发起转换或删除作业。
 * 转换期间作业状态不变，所以只能按服务端返回的活跃操作数判断，不能按 status 猜。
 */
function canAbort(row: ImportJobRow): boolean {
  return row.active_operation_count > 0;
}

async function abortJob(row: ImportJobRow): Promise<void> {
  try {
    await ElMessageBox.confirm(
      `确认中止「${row.source_filename}」正在进行的转换？中止后该作业不再处于执行中，可重新发起转换或删除；作业状态与已保存的裁决不会被改动。`,
      "中止转换",
      { type: "warning", confirmButtonText: "中止", cancelButtonText: "取消" },
    );
  } catch {
    return;
  }
  abortingJobNo.value = row.job_no;
  try {
    await abortImportJob(row.job_no);
    ElMessage.success("转换已中止");
    await list.load();
  } catch (failure) {
    ElMessage.error(failure instanceof Error ? failure.message : "中止失败");
  } finally {
    abortingJobNo.value = "";
  }
}

/** 当前格式版本真实可下载的模板示例；由后端列出，不在前端写死示例 A/B。 */
const templateExamples = ref<RulebookTemplateExampleItem[]>([]);

async function loadTemplateExamples(): Promise<void> {
  try {
    templateExamples.value = await listRulebookTemplateExamples();
  } catch {
    templateExamples.value = [];
  }
}

onMounted(() => {
  void list.load();
  void loadTemplateExamples();
});
</script>

<template>
  <div class="app-panel">
    <PageHeader
      eyebrow="场景管理"
      title="规则库导入"
      subtitle="上传规则库 Excel，按五步完成工作表绑定、规则转换确认、发布前检查与模板发布。"
      :show-refresh="false"
    />
    <el-form inline class="app-query" @submit.prevent="list.search()">
      <el-form-item label="文件名称">
        <el-input v-model="filters.filename" clearable placeholder="请输入文件名称" style="width: 200px" @keyup.enter="list.search()" />
      </el-form-item>
      <el-form-item label="导入状态">
        <el-select v-model="filters.status" clearable placeholder="全部" style="width: 140px">
          <el-option v-for="item in statusOptions" :key="item.value" :value="item.value" :label="item.label" />
        </el-select>
      </el-form-item>
      <el-form-item label="创建时间">
        <el-date-picker v-model="filters.created_from" type="date" value-format="YYYY-MM-DD" placeholder="开始日期" style="width: 150px" />
        <span style="margin: 0 6px">至</span>
        <el-date-picker v-model="filters.created_to" type="date" value-format="YYYY-MM-DD" placeholder="结束日期" style="width: 150px" />
      </el-form-item>
      <el-form-item>
        <el-button type="primary" @click="list.search()">查询</el-button>
        <el-button @click="list.reset()">重置</el-button>
        <el-button tag="a" :href="exportUrl()">导出</el-button>
        <el-button :loading="loading" @click="list.load()">刷新</el-button>
      </el-form-item>
    </el-form>

    <div class="app-toolbar">
      <div class="app-toolbar__left">
        <el-button type="primary" @click="createVisible = true">新增导入</el-button>
        <el-dropdown trigger="click">
          <el-button>下载模板</el-button>
          <template #dropdown>
            <el-dropdown-menu>
              <!-- 只列出当前格式版本真实提供的示例，避免展示必然下载失败的入口。 -->
              <el-dropdown-item v-for="item in templateExamples" :key="item.key">
                <a :href="rulebookTemplateUrl(item.key)">{{ item.label }}</a>
              </el-dropdown-item>
            </el-dropdown-menu>
          </template>
        </el-dropdown>
      </div>
    </div>

    <el-alert v-if="error" type="error" show-icon :closable="false" :title="`读取失败：${error}`">
      <el-button size="small" @click="list.load()">重试</el-button>
    </el-alert>

    <el-table v-else :data="items" border stripe v-loading="loading" empty-text="暂无数据">
      <el-table-column type="index" label="序号" width="60" />
      <el-table-column label="文件名称" min-width="220" show-overflow-tooltip>
        <template #default="{ row }">
          <el-button link type="primary" class="table-link" @click="openJob(row)">{{ row.source_filename }}</el-button>
        </template>
      </el-table-column>
      <el-table-column label="目标场景" min-width="180" show-overflow-tooltip>
        <template #default="{ row }">{{ targetText(row) }}</template>
      </el-table-column>
      <el-table-column label="导入状态" width="130">
        <template #default="{ row }">
          <el-tag
            :type="row.status === 'PUBLISHED' ? 'success' : row.status === 'PENDING_DECISION' ? 'warning' : 'primary'"
            size="small"
            effect="light"
            disable-transitions
          >
            {{ statusLabel(row.status) }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column label="待处理数" width="100">
        <template #default="{ row }">
          <span :class="row.pending_issue_count ? 'text-danger' : ''">{{ row.pending_issue_count }}</span>
        </template>
      </el-table-column>
      <el-table-column label="创建时间" width="170">
        <template #default="{ row }">{{ String(row.created_at).replace("T", " ").slice(0, 19) }}</template>
      </el-table-column>
      <el-table-column label="操作" width="220" fixed="right">
        <template #default="{ row }">
          <el-button link type="primary" size="small" @click="openJob(row)">进入处理</el-button>
          <el-button
            v-if="canAbort(row)"
            link
            type="warning"
            size="small"
            :loading="abortingJobNo === row.job_no"
            @click="abortJob(row)"
          >
            中止
          </el-button>
          <el-button link type="danger" size="small" @click="removeJob(row)">删除</el-button>
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

    <el-dialog v-model="createVisible" title="新增导入" width="600px" :close-on-click-modal="false">
      <el-alert type="info" show-icon :closable="false" title="上传规则库 Excel 后进入五步流程">
        工作表识别 → 规则确认 → 发布预览 → 发布结果；新建/更新模板的目标在发布预览前选择并绑定。
      </el-alert>
      <div class="block-gap-sm">
        <el-upload
          drag
          :auto-upload="false"
          :limit="1"
          accept=".xlsx,.xlsm"
          :on-change="onFileChange"
          :on-remove="() => (selectedFile = null)"
        >
          <div class="upload-block__icon">⬆</div>
          <div class="el-upload__text">拖拽规则库 Excel 到此处，或<em>点击选择文件</em></div>
          <template #tip>
            <div class="text-hint">支持 .xlsx / .xlsm；请使用下载的模板或与之同格式的规则库文件。</div>
          </template>
        </el-upload>
      </div>
      <template #footer>
        <el-button @click="createVisible = false">取消</el-button>
        <el-button type="primary" :loading="uploading" @click="submitCreate">上传并进入处理</el-button>
      </template>
    </el-dialog>
  </div>
</template>
