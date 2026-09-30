<script setup lang="ts">
import { computed, ref } from "vue";
import { ElMessage } from "element-plus";
import { Download } from "lucide-vue-next";

import type { ProfileSheet } from "@/api/contracts";
import { getProfileSheets, unifiedTemplateUrl } from "@/api/imports";
import { getSceneDraft } from "@/api/scenes";

/**
 * 规则库模板下载。
 *
 * 场景草稿已有确认 Sheet 时，直接下载这些角色，忽略任何勾选。
 * 还没有确认绑定时，由用户勾选本次要下载的工作表：不默认全选、不写场景。
 *
 * 模板按当前场景 Schema 生成。场景选择 Sheet，并可覆盖 Sheet 名与列别名；
 * 勾选哪些表就生成哪些表。
 */
const props = defineProps<{
  scenarioCode: string | null;
  label?: string;
  disabled?: boolean;
}>();

const open = ref(false);
const loading = ref(false);
const sheets = ref<ProfileSheet[]>([]);
const selected = ref<string[]>([]);

/** 全部可选工作表，按协议声明顺序（顺序由协议决定，不在页面重排）。 */
const sheetOptions = computed(() => sheets.value);

const allSelected = computed(
  () => sheetOptions.value.length > 0 && selected.value.length === sheetOptions.value.length,
);

function toggleAll(): void {
  selected.value = allSelected.value ? [] : sheetOptions.value.map((item) => item.sheet_role);
}

async function startDownload(): Promise<void> {
  if (!props.scenarioCode || props.disabled) {
    return;
  }
  loading.value = true;
  try {
    const draft = await getSceneDraft(props.scenarioCode);
    const bound = (draft.sheets ?? []).map((item) => item.sheet_role).filter((role) => role.length > 0);
    if (bound.length > 0) {
      window.location.assign(unifiedTemplateUrl(props.scenarioCode));
      return;
    }
    const profile = await getProfileSheets(props.scenarioCode ?? undefined);
    sheets.value = profile.sheets.map((item) => ({
      sheet_role: item.sheet_role,
      code_prefix: item.code_prefix,
      display_name: item.display_name,
      node_types: item.node_types,
      column_count: item.column_count,
    }));
    selected.value = [];
    open.value = true;
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : "无法准备模板下载");
  } finally {
    loading.value = false;
  }
}

function confirmDownload(): void {
  if (!props.scenarioCode) {
    return;
  }
  if (!selected.value.length) {
    ElMessage.warning("请勾选本次要下载的工作表");
    return;
  }
  window.location.assign(
    unifiedTemplateUrl(props.scenarioCode, { sheetRoles: [...selected.value] }),
  );
  open.value = false;
}
</script>

<template>
  <span>
    <el-button :disabled="!scenarioCode || disabled" :loading="loading" @click="startDownload">
      <template #icon><Download :size="15" /></template>
      {{ label ?? "下载规则库模板" }}
    </el-button>
    <el-dialog v-model="open" title="选择本次下载的工作表" width="680px" :close-on-click-modal="false">
      <el-alert type="warning" show-icon :closable="false" title="这次勾选只用于生成这一份文件">
        下载选择不会写入场景绑定，也不等于最终绑定。上传后仍要确认每张 Sheet 绑到哪个节点。
        未勾选的表可以留空，导入时它们不进校验、不进转换范围。
      </el-alert>
      <div class="download-toolbar">
        <el-button size="small" @click="toggleAll">{{ allSelected ? "取消全选" : "全选" }}</el-button>
        <span class="text-muted">已选 {{ selected.length }} / {{ sheetOptions.length }} 张</span>
      </div>
      <el-checkbox-group v-model="selected" class="download-sheets">
        <el-checkbox v-for="item in sheetOptions" :key="item.sheet_role" :value="item.sheet_role">
          {{ item.display_name }}
        </el-checkbox>
      </el-checkbox-group>
      <template #footer>
        <el-button @click="open = false">取消</el-button>
        <el-button type="primary" :disabled="!selected.length" @click="confirmDownload">
          下载本次勾选
        </el-button>
      </template>
    </el-dialog>
  </span>
</template>

<style scoped>
.download-toolbar {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-top: 12px;
}

.download-sheets {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 4px 16px;
  margin-top: 12px;
}

.download-sheets :deep(.el-checkbox) {
  display: flex;
  margin-right: 0;
}
</style>
