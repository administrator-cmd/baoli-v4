<script setup lang="ts">
import { computed, reactive, ref, watch } from "vue";
import { ElMessage, ElMessageBox } from "element-plus";

import type { DraftMaterialItem, ScenarioDraftView } from "@/api/contracts";
import { ApiError } from "@/api/http";
import {
  createDraftMaterial,
  deleteDraftMaterial,
  getSceneDraft,
  updateDraftMaterial,
} from "@/api/scenes";

/**
 * 当前场景草稿的材料配置面板。可新增、编辑、删除；
 * 删除前由服务端返回被引用的字段与规则清单。
 * 这里维护的是材料位定义；实际上传文件在资料管理中按任务维护。
 */
const props = defineProps<{
  scenarioCode: string | null;
  /** 由父页面触发的外部刷新（例如发布后）。 */
  refreshToken?: number;
}>();

const emit = defineEmits<{
  changed: [];
}>();

const materials = ref<DraftMaterialItem[]>([]);
const draft = ref<ScenarioDraftView | null>(null);
const loading = ref(false);
const loadError = ref<string | null>(null);
const saving = ref(false);

const canEdit = computed(() => Boolean(props.scenarioCode));

const dialogVisible = ref(false);
const editingItem = ref<DraftMaterialItem | null>(null);
const form = reactive({
  name: "",
  required: true,
  description: "",
  /**
   * 业务编号（Excel 的「材料编号」列）。规则库导出时写回该列，导入时它是必填项；
   * 从 Excel 导入的材料会带回原编号，页面新建的材料需要人工补一个。
   */
  source_ref: "",
});

const dependencyIssue = ref<{ title: string; entries: string[] } | null>(null);

async function load(): Promise<void> {
  if (!props.scenarioCode) {
    materials.value = [];
    draft.value = null;
    return;
  }
  loading.value = true;
  loadError.value = null;
  try {
    const payload = await getSceneDraft(props.scenarioCode);
    draft.value = payload;
    materials.value = payload.materials;
  } catch (error) {
    materials.value = [];
    draft.value = null;
    loadError.value = error instanceof Error ? error.message : "材料配置读取失败";
  } finally {
    loading.value = false;
  }
}

watch(() => [props.scenarioCode, props.refreshToken], () => void load(), { immediate: true });

function openCreate(): void {
  editingItem.value = null;
  form.name = "";
  form.required = true;
  form.description = "";
  form.source_ref = "";
  dialogVisible.value = true;
}

function openEdit(item: DraftMaterialItem): void {
  editingItem.value = item;
  form.name = item.name;
  form.required = item.required;
  form.description = item.description;
  form.source_ref = item.source_ref ?? "";
  dialogVisible.value = true;
}

function describeError(error: unknown): string {
  if (error instanceof ApiError) {
    return error.message;
  }
  return error instanceof Error ? error.message : "操作失败";
}

async function submitForm(): Promise<void> {
  if (!props.scenarioCode || !form.name.trim()) {
    ElMessage.warning("请填写材料名称");
    return;
  }
  saving.value = true;
  try {
    const payload = {
      name: form.name.trim(),
      required: form.required,
      description: form.description,
      source_ref: form.source_ref.trim() ? form.source_ref.trim() : null,
    };
    if (editingItem.value) {
      await updateDraftMaterial(props.scenarioCode, editingItem.value.material_key, payload);
      ElMessage.success("材料配置已保存");
    } else {
      await createDraftMaterial(props.scenarioCode, payload);
      ElMessage.success("材料配置已新增");
    }
    dialogVisible.value = false;
    await load();
    emit("changed");
  } catch (error) {
    // 失败时保留弹窗与输入，错误贴近当前操作区显示。
    ElMessage.error(describeError(error));
  } finally {
    saving.value = false;
  }
}

async function removeItem(item: DraftMaterialItem): Promise<void> {
  if (!props.scenarioCode) {
    return;
  }
  try {
    await ElMessageBox.confirm(
      `确认删除材料「${item.name}」？删除前会检查字段与规则引用；历史版本不受影响。`,
      "删除材料配置",
      { type: "warning", confirmButtonText: "删除", cancelButtonText: "取消" },
    );
  } catch {
    return;
  }
  saving.value = true;
  try {
    await deleteDraftMaterial(props.scenarioCode, item.material_key);
    ElMessage.success("材料配置已删除");
    await load();
    emit("changed");
  } catch (error) {
    if (error instanceof ApiError && error.code === "MATERIAL_REFERENCED") {
      const details = error.details as Record<string, unknown>;
      const fields = Array.isArray(details.field_keys) ? (details.field_keys as string[]) : [];
      const rules = Array.isArray(details.rule_keys) ? (details.rule_keys as string[]) : [];
      dependencyIssue.value = {
        title: error.message,
        entries: [...fields.map((item) => `字段：${item}`), ...rules.map((item) => `规则：${item}`)],
      };
    } else {
      ElMessage.error(describeError(error));
    }
  } finally {
    saving.value = false;
  }
}

async function toggleRequired(item: DraftMaterialItem): Promise<void> {
  if (!props.scenarioCode) {
    return;
  }
  try {
    await updateDraftMaterial(props.scenarioCode, item.material_key, { required: !item.required });
    ElMessage.success(item.required ? "已改为按需材料" : "已改为必传材料");
    await load();
    emit("changed");
  } catch (error) {
    ElMessage.error(describeError(error));
  }
}
</script>

<template>
  <div>
    <el-alert v-if="!scenarioCode" type="info" show-icon :closable="false" title="请先选择场景" />

    <template v-else>
      <div class="app-toolbar">
        <div class="app-toolbar__left">
          <el-button v-if="canEdit" type="primary" @click="openCreate">新增材料</el-button>
          <span v-if="draft" class="text-muted">
            {{ draft.source_label }}
          </span>
        </div>
        <div class="app-toolbar__right">
          <el-button :loading="loading" @click="load">刷新</el-button>
        </div>
      </div>

      <el-alert v-if="loadError" type="error" show-icon :closable="false" :title="`读取失败：${loadError}`">
        <el-button size="small" @click="load">重试</el-button>
      </el-alert>

      <el-table v-else :data="materials" border stripe v-loading="loading" empty-text="暂无材料配置">
        <el-table-column type="index" label="序号" width="60" />
        <el-table-column label="材料编码" width="190">
          <template #default="{ row }"><span class="mono">{{ row.material_key }}</span></template>
        </el-table-column>
        <el-table-column prop="source_ref" label="材料编号" width="120">
          <template #default="{ row }">
            <span v-if="row.source_ref" class="mono">{{ row.source_ref }}</span>
            <span v-else class="text-hint">未填写</span>
          </template>
        </el-table-column>
        <el-table-column prop="name" label="材料名称" min-width="180" show-overflow-tooltip />
        <el-table-column label="是否必需" width="100">
          <template #default="{ row }">
            <el-tag :type="row.required ? 'warning' : 'info'" size="small" effect="plain" disable-transitions>
              {{ row.required ? "必需" : "按需" }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="description" label="业务说明" min-width="220" show-overflow-tooltip>
          <template #default="{ row }">{{ row.description || "—" }}</template>
        </el-table-column>
        <el-table-column label="更新时间" width="170">
          <template #default="{ row }">{{ row.updated_at ? String(row.updated_at).replace('T', ' ').slice(0, 19) : "—" }}</template>
        </el-table-column>
        <el-table-column v-if="canEdit" label="操作" width="220" fixed="right">
          <template #default="{ row }">
            <el-button link type="primary" size="small" @click="openEdit(row)">编辑</el-button>
            <el-button link type="primary" size="small" @click="toggleRequired(row)">
              {{ row.required ? "改为按需" : "改为必需" }}
            </el-button>
            <el-button link type="danger" size="small" @click="removeItem(row)">删除</el-button>
          </template>
        </el-table-column>
      </el-table>
      <p class="text-hint">
        材料编码由系统生成，用于稳定引用；材料位只定义「需要哪些材料」，
        材料类型在**上传文件时**由用户从 8018 支持目录中选择，不在这里配置。
      </p>

      <el-dialog v-model="dialogVisible" :title="editingItem ? '编辑材料' : '新增材料'" width="600px" :close-on-click-modal="false">
        <el-form label-width="100px">
          <el-form-item label="材料编号">
            <el-input v-model="form.source_ref" maxlength="80" placeholder="例如：MAT-001；导出规则库时写回「材料编号」列" />
            <div class="text-hint">从 Excel 导入的材料会带回原编号；页面新建的请补一个，否则导出会因缺少必填编号被拒绝。</div>
          </el-form-item>
          <el-form-item label="材料名称" required>
            <el-input v-model="form.name" maxlength="255" placeholder="例如：主合同" />
            <div class="text-hint">材料名称是业务叫法，只表达这个材料位需要什么文件。</div>
          </el-form-item>
          <el-form-item label="是否必传">
            <el-radio-group v-model="form.required">
              <el-radio :value="true">必传</el-radio>
              <el-radio :value="false">按需</el-radio>
            </el-radio-group>
          </el-form-item>
          <el-form-item label="业务说明">
            <el-input v-model="form.description" type="textarea" :rows="3" maxlength="4000" placeholder="说明这个材料位需要什么文件" />
          </el-form-item>
        </el-form>
        <template #footer>
          <el-button @click="dialogVisible = false">取消</el-button>
          <el-button type="primary" :loading="saving" @click="submitForm">确定</el-button>
        </template>
      </el-dialog>

      <el-dialog :model-value="Boolean(dependencyIssue)" title="材料仍被引用，不能删除" width="600px" @close="dependencyIssue = null">
        <p>{{ dependencyIssue?.title }}</p>
        <ul class="text-muted">
          <li v-for="entry in dependencyIssue?.entries ?? []" :key="entry">{{ entry }}</li>
        </ul>
        <p class="text-hint">请先在字段配置 / 规则配置中移除这些引用，再删除该材料。</p>
        <template #footer>
          <el-button type="primary" @click="dependencyIssue = null">知道了</el-button>
        </template>
      </el-dialog>

      <div class="text-hint block-gap-sm">
        已保存到草稿，发布后才供新方案使用。已建方案与任务使用它们创建时的快照。
      </div>
    </template>
  </div>
</template>
