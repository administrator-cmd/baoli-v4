<script setup lang="ts">
import { computed, ref } from "vue";
import { ElMessage, ElMessageBox } from "element-plus";

import type { DraftCalculationItem, DraftFieldItem, JsonValue } from "@/api/contracts";
import { deleteDraftCalculation } from "@/api/scenes";
import SceneCalculationEditor from "@/components/scenes/SceneCalculationEditor.vue";
import { sourceSheetLabel, useRuleSourceFilter } from "@/composables/useRuleSourceFilter";
import { compareNaturalLabels } from "@/utils/naturalSort";

const props = defineProps<{ scenarioCode: string; items: DraftCalculationItem[]; fields: DraftFieldItem[] }>();
const emit = defineEmits<{ changed: [] }>();
const editorVisible = ref(false);
const editingItem = ref<DraftCalculationItem | null>(null);
const detailItem = ref<DraftCalculationItem | null>(null);
const deleting = ref(false);
const search = ref("");
const { selectedSheet, sourceSheetOptions, matchesSourceSheet } = useRuleSourceFilter(() => props.items);
const keyword = computed(() => search.value.trim().toLowerCase());
const filteredCalculations = computed(() => props.items.filter((item) => {
  if (!matchesSourceSheet(item)) return false;
  return !keyword.value || `${item.name ?? ""}\n${item.rule_code ?? ""}`.toLowerCase().includes(keyword.value);
}).sort((left, right) => compareNaturalLabels(left.rule_code, right.rule_code)));
const detailParameters = computed(() => (detailItem.value?.definition.parameters ?? []) as Record<string, JsonValue>[]);

function resetFilters(): void {
  search.value = "";
  selectedSheet.value = "";
}

function openEditor(item: DraftCalculationItem | null): void {
  editingItem.value = item;
  editorVisible.value = true;
}

function statusLabel(item: DraftCalculationItem): string {
  const status = item.definition.config_status ?? item.status;
  if (status === "PENDING") return "待确认";
  if (status === "DISABLED" || item.definition.enabled === false) return "停用";
  return status === "CLEAR" || status == null ? "启用" : String(status);
}

function displayValue(value: JsonValue | undefined): string {
  if (value == null || value === "") return "—";
  return typeof value === "object" ? JSON.stringify(value, null, 2) : String(value);
}

function sourceLabel(type: string): string {
  return ({ FIELD: "字段", CALC_RESULT: "计算结果", CONSTANT: "常量" } as Record<string, string>)[type] ?? type;
}

function resultName(code: string): string {
  const item = props.items.find((entry) => entry.result_code === code || entry.rule_code === code);
  return item?.name ? `${item.name}（${code}）` : code;
}

function parameterValue(parameter: Record<string, JsonValue>): string {
  if (parameter.source_type === "CONSTANT") return JSON.stringify(parameter.constant_value ?? null);
  const code = String(parameter.source_code ?? "");
  if (parameter.source_type === "CALC_RESULT") return resultName(code);
  if (parameter.source_type === "FIELD") {
    const field = props.fields.find((entry) => entry.field_key === code);
    if (field) return `${field.material_name} · ${field.name}`;
  }
  return code || "—";
}

async function remove(item: DraftCalculationItem): Promise<void> {
  if (!item.rule_code) {
    ElMessage.error("计算规则缺少编号，无法删除");
    return;
  }
  try {
    await ElMessageBox.confirm(`确认删除计算规则「${item.name ?? item.rule_code}」？`, "删除计算规则", {
      type: "warning", confirmButtonText: "删除", cancelButtonText: "取消",
    });
  } catch {
    return;
  }
  deleting.value = true;
  try {
    await deleteDraftCalculation(props.scenarioCode, item.rule_code);
    ElMessage.success("计算规则已从草稿删除");
    emit("changed");
  } catch (cause) {
    ElMessage.error(cause instanceof Error ? cause.message : "删除失败");
  } finally {
    deleting.value = false;
  }
}
</script>

<template>
  <div>
    <el-form inline class="app-query block-gap-sm">
      <el-form-item label="来源工作表">
        <el-select v-model="selectedSheet" clearable filterable placeholder="全部工作表" style="width: 240px">
          <el-option v-for="sheet in sourceSheetOptions" :key="sheet.value" :value="sheet.value" :label="sheet.label" />
        </el-select>
      </el-form-item>
      <el-form-item label="规则搜索">
        <el-input v-model="search" clearable placeholder="规则名称或业务编号" style="width: 220px" />
      </el-form-item>
      <el-form-item>
        <el-button @click="resetFilters">重置</el-button>
      </el-form-item>
    </el-form>

    <div class="app-toolbar">
      <div class="app-toolbar__left">
        <el-button type="primary" @click="openEditor(null)">新增计算规则</el-button>
        <span class="text-muted">显示 {{ filteredCalculations.length }} / {{ items.length }} 条</span>
      </div>
    </div>

    <el-table :data="filteredCalculations" border stripe class="management-table" :empty-text="items.length ? '未找到匹配的规则' : '当前配置没有计算规则'">
      <el-table-column prop="rule_code" label="规则编号" width="120" show-overflow-tooltip align="center" />
      <el-table-column prop="name" label="名称" min-width="140" show-overflow-tooltip />
      <el-table-column label="来源工作表" width="160" show-overflow-tooltip>
        <template #default="{ row }">
          {{ sourceSheetLabel(row.origin) }}
        </template>
      </el-table-column>
      <el-table-column prop="formula" label="计算方法" min-width="160" show-overflow-tooltip />
      <el-table-column prop="requirement" label="业务要求" min-width="180" show-overflow-tooltip />
      <el-table-column prop="result_code" label="结果编号" width="112" show-overflow-tooltip align="center" />
      <el-table-column prop="unit" label="单位" width="64" align="center" />
      <el-table-column prop="currency" label="币种" width="64" align="center" />
      <el-table-column label="状态" width="80" align="center"><template #default="{ row }">{{ statusLabel(row) }}</template></el-table-column>
      <el-table-column label="操作" width="140" class-name="table-actions" fixed="right" align="center">
        <template #default="{ row }">
          <el-button link type="primary" size="small" @click="detailItem = row">详情</el-button>
          <el-button link type="primary" size="small" @click="openEditor(row)">编辑</el-button>
          <el-button link type="danger" size="small" :disabled="deleting" @click="remove(row)">删除</el-button>
        </template>
      </el-table-column>
    </el-table>

    <SceneCalculationEditor v-model="editorVisible" :scenario-code="scenarioCode" :item="editingItem"
      :items="items" :fields="fields" @saved="emit('changed')" />

    <el-drawer :model-value="Boolean(detailItem)" title="计算规则详情" size="min(680px, 96vw)" @close="detailItem = null">
      <template v-if="detailItem">
        <el-descriptions :column="2" border>
          <el-descriptions-item label="规则编号">{{ detailItem.rule_code || '—' }}</el-descriptions-item>
          <el-descriptions-item label="计算名称">{{ detailItem.name || '—' }}</el-descriptions-item>
          <el-descriptions-item label="业务编号">{{ displayValue(detailItem.definition.source_id) }}</el-descriptions-item>
          <el-descriptions-item label="结果标识">{{ detailItem.result_code || '—' }}</el-descriptions-item>
          <el-descriptions-item label="来源工作表">{{ sourceSheetLabel(detailItem.origin) }}</el-descriptions-item>
          <el-descriptions-item label="启用状态">{{ statusLabel(detailItem) }}</el-descriptions-item>
          <el-descriptions-item label="计算方法" :span="2"><div class="detail-text">{{ detailItem.formula || '—' }}</div></el-descriptions-item>
          <el-descriptions-item label="业务要求" :span="2"><div class="detail-text">{{ detailItem.requirement || '—' }}</div></el-descriptions-item>
          <el-descriptions-item label="结果类型">{{ displayValue(detailItem.definition.result_type) }}</el-descriptions-item>
          <el-descriptions-item label="单位">{{ detailItem.unit || '—' }}</el-descriptions-item>
          <el-descriptions-item label="币种">{{ detailItem.currency || '—' }}</el-descriptions-item>
          <el-descriptions-item label="期间要求">{{ displayValue(detailItem.definition.period_policy) }}</el-descriptions-item>
          <el-descriptions-item label="报表口径" :span="2">{{ displayValue(detailItem.definition.statement_scope) }}</el-descriptions-item>
          <el-descriptions-item label="依赖计算" :span="2"><div class="detail-text">{{ (detailItem.definition.depends_on_result_codes as string[] | undefined)?.map(resultName).join('、') || '—' }}</div></el-descriptions-item>
          <el-descriptions-item label="计算参数" :span="2">
            <el-table :data="detailParameters" border empty-text="未配置参数">
              <el-table-column prop="parameter_code" label="参数名称" min-width="110" />
              <el-table-column label="来源" width="100"><template #default="{ row }">{{ sourceLabel(row.source_type) }}</template></el-table-column>
              <el-table-column label="取值" min-width="170"><template #default="{ row }">{{ parameterValue(row) }}</template></el-table-column>
            </el-table>
          </el-descriptions-item>
          <el-descriptions-item label="备注" :span="2"><div class="detail-text">{{ displayValue(detailItem.definition.remark) }}</div></el-descriptions-item>
          <el-descriptions-item label="业务补充" :span="2"><div class="detail-text">{{ displayValue(detailItem.business_context) }}</div></el-descriptions-item>
        </el-descriptions>
      </template>
    </el-drawer>
  </div>
</template>

<style scoped>
.detail-text { white-space: pre-wrap; overflow-wrap: anywhere; }
</style>
