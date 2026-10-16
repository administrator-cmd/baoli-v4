<script setup lang="ts">
import { computed, onMounted, ref, watch } from "vue";
import { useRoute, useRouter } from "vue-router";
import { ElMessage, ElMessageBox } from "element-plus";

import {
  abortImportJob,
  createImportJob,
  deleteImportJob,
  listImportJobsPage,
  listTemplates,
  type ImportJobRow,
  type TemplateOption,
} from "@/api/imports";
import { usePagedList } from "@/composables/usePagedList";
import PageHeader from "@/components/PageHeader.vue";
import { statusLabel } from "@/constants/labels";

/**
 * 规则库导入（入口列表）：文件名、所属场景、状态与待处理数来自真实作业。
 * 新增导入必须先选定已创建的场景。确认后只写入场景草稿，由管理员核对后显式发布。
 */
const router = useRouter();
const route = useRoute();
const scopedScenarioCode = computed(() => typeof route.query.scenario === "string" ? route.query.scenario : "");

const list = usePagedList<ImportJobRow, { filename: string; status: string; created_from: string; created_to: string }>({
  key: `scene-imports:${scopedScenarioCode.value || "all"}`,
  initialFilters: { filename: "", status: "", created_from: "", created_to: "" },
  fetch: (params) =>
    listImportJobsPage({
      scenario_code: scopedScenarioCode.value || undefined,
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
const deletingJobs = ref<string[]>([]);
const deleteError = ref<string | null>(null);

const statusOptions = [
  { value: "PARSED", label: "已解析" },
  { value: "PROPOSED", label: "已生成候选" },
  { value: "CONFIRMED", label: "已确认" },
  { value: "APPLIED", label: "已写入场景配置" },
];

function openJob(row: ImportJobRow): void {
  void router.push({ name: "scene-import-detail", params: { jobNo: row.job_no } });
}

/* 新增导入 */
const createVisible = ref(false);
const selectedFile = ref<File | null>(null);
const uploading = ref(false);
/** 新增导入所属的场景编码：上传以场景为起点，作业必须明确属于哪个场景。 */
const createScenarioCode = ref("");
const scenariosError = ref<string | null>(null);
const scenariosLoading = ref(false);

function openCreate(): void {
  createVisible.value = true;
  void loadScenarios();
}

function onFileChange(file: { raw?: File }): void {
  selectedFile.value = file.raw ?? null;
}

async function submitCreate(): Promise<void> {
  if (scopedScenarioCode.value && createScenarioCode.value !== scopedScenarioCode.value) {
    ElMessage.error("当前导入页的场景范围已变化，请重新选择所属场景");
    return;
  }
  if (!createScenarioCode.value) {
    ElMessage.warning("请先选择本次导入所属的场景；系统不会从 Excel 内容猜场景");
    return;
  }
  if (!selectedFile.value) {
    ElMessage.warning("请选择规则库 Excel 文件");
    return;
  }
  uploading.value = true;
  try {
    const job = await createImportJob(selectedFile.value, createScenarioCode.value);
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
      `确认删除导入作业「${row.source_filename}」？验证项目允许直接删除，已发布版本会解除这份导入来源引用。`,
      "删除导入作业",
      { type: "warning", confirmButtonText: "删除", cancelButtonText: "取消" },
    );
  } catch (failure) {
    if (failure !== "cancel" && failure !== "close") {
      deleteError.value = failure instanceof Error ? failure.message : "删除确认失败";
      ElMessage.error(deleteError.value);
    }
    return;
  }
  deletingJobs.value.push(row.job_no);
  deleteError.value = null;
  try {
    await deleteImportJob(row.job_no);
    ElMessage.success("导入作业已删除");
    await list.load();
  } catch (failure) {
    deleteError.value = failure instanceof Error ? failure.message : "删除失败";
    ElMessage.error(deleteError.value);
  } finally {
    deletingJobs.value = deletingJobs.value.filter((jobNo) => jobNo !== row.job_no);
  }
}

/**
 * 这份规则库所属的场景。
 *
 * 显示服务端保存的所属场景，不根据表头或文件名猜测。
 */
function sceneText(row: ImportJobRow): string {
  // 作业所属场景在上传时指定。
  return row.scenario_name || row.scenario_code || "未记录场景";
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
      `确认中止「${row.source_filename}」正在进行的转换？中止后该作业不再处于执行中，可重新发起转换或删除；作业状态与已保存的候选不会被改动。`,
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

/**
 * 可选场景：上传必须以场景为起点，因此新增导入前必须先选场景。
 * 列表来自后端场景目录，前端不写死任何场景。
 */
const scenarios = ref<TemplateOption[]>([]);
const availableScenarios = computed(() => scopedScenarioCode.value
  ? scenarios.value.filter((item) => item.scenario_code === scopedScenarioCode.value)
  : scenarios.value);

async function loadScenarios(): Promise<void> {
  scenariosLoading.value = true;
  scenariosError.value = null;
  try {
    // 管理员在场景发布之前就要能选中它。业务用户的已发布列表在这里选不到新建场景。
    scenarios.value = await listTemplates();
    const requestedScenario = scopedScenarioCode.value;
    if (requestedScenario && scenarios.value.some((item) => item.scenario_code === requestedScenario)) {
      createScenarioCode.value = requestedScenario;
    } else if (!scenarios.value.some((item) => item.scenario_code === createScenarioCode.value)) {
      createScenarioCode.value = "";
    }
  } catch (error) {
    scenarios.value = [];
    scenariosError.value = error instanceof Error ? error.message : "场景目录读取失败";
  } finally {
    scenariosLoading.value = false;
  }
}

onMounted(() => {
  void list.load();
  if (route.query.create === "1" && scopedScenarioCode.value) {
    openCreate();
    void router.replace({ name: "scene-imports", query: { scenario: scopedScenarioCode.value } });
  }
});
watch(scopedScenarioCode, () => {
  createScenarioCode.value = "";
  list.search();
});
</script>

<template>
  <div class="app-panel">
    <PageHeader
      eyebrow="场景管理"
      title="规则库导入"
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
      </el-form-item>
    </el-form>

    <div class="app-toolbar">
      <div class="app-toolbar__left">
        <el-button type="primary" @click="openCreate">新增导入</el-button>
      </div>
    </div>

    <el-alert v-if="error" type="error" show-icon :closable="false" :title="`读取失败：${error}`">
      <el-button size="small" @click="list.load()">重试</el-button>
    </el-alert>

    <el-alert v-if="deleteError" type="error" show-icon :closable="false" :title="`删除失败：${deleteError}`" />

    <el-table v-if="!error" :data="items" border stripe v-loading="loading" empty-text="暂无数据">
      <el-table-column type="index" label="序号" width="60" />
      <el-table-column label="文件名称" min-width="220" show-overflow-tooltip>
        <template #default="{ row }">
          <el-button link type="primary" class="table-link" @click="openJob(row)">{{ row.source_filename }}</el-button>
        </template>
      </el-table-column>
      <el-table-column label="所属场景" min-width="180" show-overflow-tooltip>
        <template #default="{ row }">{{ sceneText(row) }}</template>
      </el-table-column>
      <el-table-column label="导入状态" width="130">
        <template #default="{ row }">
          <el-tag
            :type="row.status === 'APPLIED' ? 'success' : 'primary'"
            size="small"
            effect="light"
            disable-transitions
          >
            {{ statusLabel(row.status) }}
          </el-tag>
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
          <el-button
            link
            type="danger"
            size="small"
            :loading="deletingJobs.includes(row.job_no)"
            :disabled="abortingJobNo === row.job_no"
            @click="removeJob(row)"
          >删除</el-button>
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
      <el-alert v-if="scenariosError" type="error" show-icon :closable="false" :title="`场景目录读取失败：${scenariosError}`">
        <el-button size="small" @click="loadScenarios">重试</el-button>
      </el-alert>
      <el-alert v-else-if="!scenariosLoading && availableScenarios.length === 0" type="warning" show-icon :closable="false" :title="scopedScenarioCode ? '当前场景不可用于导入' : '还没有可选场景'">
        <el-button size="small" @click="router.push({ name: 'scenes' })">打开场景列表</el-button>
      </el-alert>
      <el-alert type="info" show-icon :closable="false" title="先选择场景，再上传规则库" />
      <el-form label-width="100px" class="block-gap-sm">
        <el-form-item label="所属场景" required>
          <!-- 上传以场景为起点：作业明确属于该场景，系统不从 Excel 内容猜场景。 -->
          <el-select v-model="createScenarioCode" filterable :loading="scenariosLoading" :disabled="Boolean(scenariosError) || Boolean(scopedScenarioCode)" placeholder="请选择本次导入所属的场景" style="width: 320px">
            <el-option
              v-for="item in availableScenarios"
              :key="item.scenario_code"
              :value="item.scenario_code"
              :label="item.latest_version_no ? `${item.scenario_name}（已发布第 ${item.latest_version_no} 版）` : `${item.scenario_name}（尚未发布）`"
            />
          </el-select>
        </el-form-item>
      </el-form>
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
          </template>
        </el-upload>
      </div>
      <template #footer>
        <el-button @click="createVisible = false">取消</el-button>
        <el-button type="primary" :loading="uploading" :disabled="scenariosLoading || Boolean(scenariosError)" @click="submitCreate">上传并进入处理</el-button>
      </template>
    </el-dialog>
  </div>
</template>
