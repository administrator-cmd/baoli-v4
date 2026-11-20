<script setup lang="ts">
import { computed, reactive, ref, watch } from "vue";
import { useRouter } from "vue-router";
import { ElMessage, ElMessageBox } from "element-plus";

import type { DraftRuleItem, ScenarioConfigurationView } from "@/api/contracts";
import { ApiError } from "@/api/http";
import { deleteDraftRule } from "@/api/scenes";
import { ruleOperatorOptions } from "@/constants/labels";
import { EXECUTION_MODE_OPTIONS, executionModeLabel } from "@/constants/executionModes";
import { sourceSheetLabel, useRuleSourceFilter } from "@/composables/useRuleSourceFilter";
import { readRuleListPaging, rememberRuleListPaging } from "@/utils/ruleListPaging";
import { compareNaturalLabels } from "@/utils/naturalSort";
import { PAGE_SIZE_OPTIONS } from "@/constants/pagination";

/**
 * 规则配置面板：业务规则可增删改，自动抽取规则通过对应的字段配置编辑。
 * 审核要求、输入对象、参数与适用条件都来自真实配置；发布时由后端做同口径编译校验。
 */
const props = defineProps<{
  scenarioCode: string | null;
  /** 父级持有的同一份场景配置；面板不再各自请求整份配置。 */
  config: ScenarioConfigurationView | null;
  configError?: string | null;
  configLoading?: boolean;
}>();

const emit = defineEmits<{
  changed: [];
  refresh: [];
  editField: [fieldKey: string];
}>();

const router = useRouter();

/** 数据来自父级唯一的配置请求。 */
const rules = computed(() => props.config?.rules ?? []);
const loading = computed(() => props.configLoading === true);
const loadError = computed(() => props.configError ?? null);
const saving = ref(false);

const canEdit = computed(() => Boolean(props.scenarioCode));

/** 来源工作表、名称 / 业务编号与启用状态可组合筛选。 */
const query = reactive({ name: "", enabled: "", mode: "" });
const { selectedSheet, sourceSheetOptions, matchesSourceSheet } = useRuleSourceFilter(() => rules.value);
const keyword = computed(() => query.name.trim().toLowerCase());
function configurationState(rule: DraftRuleItem): string {
  if (rule.config_status === "PENDING") return "PENDING";
  return rule.enabled ? "true" : "false";
}

function configurationLabel(rule: DraftRuleItem): string {
  if (rule.config_status === "PENDING") return "待确认";
  return rule.enabled ? "启用" : "停用";
}

const filteredRules = computed(() =>
  rules.value.filter((item) => {
    if (!matchesSourceSheet(item)) return false;
    if (query.enabled && configurationState(item) !== query.enabled) return false;
    if (query.mode && (item.execution_mode || "UNKNOWN") !== query.mode) return false;
    if (keyword.value && !`${item.name}\n${item.source_ref ?? ""}`.toLowerCase().includes(keyword.value)) return false;
    return true;
  }).sort((left, right) => compareNaturalLabels(left.source_ref, right.source_ref)),
);

/** 规则在场景里可达上百条，列表分页展示；打开编辑页前记住页码，保存返回后回到同一页。 */
const savedPaging = readRuleListPaging(props.scenarioCode);
const page = ref(savedPaging?.page ?? 1);
const pageSize = ref(savedPaging?.pageSize ?? 20);
const pagedRules = computed(() =>
  filteredRules.value.slice((page.value - 1) * pageSize.value, page.value * pageSize.value),
);

const fieldKeys = computed(() => new Set(props.config?.fields.map((field) => field.field_key)));

function editableFieldKey(rule: DraftRuleItem): string | undefined {
  if (!rule.system || rule.operator !== "LLM_EXTRACT" || rule.outputs.length !== 1) return;
  const fieldKey = rule.outputs[0]?.field_key;
  return fieldKey && fieldKeys.value.has(fieldKey) ? fieldKey : undefined;
}

const actionsWidth = computed(() => {
  if (canEdit.value && pagedRules.value.some((rule) => !rule.system)) return 132;
  if (canEdit.value && pagedRules.value.some(editableFieldKey)) return 96;
  return 64;
});

watch([selectedSheet, () => query.name, () => query.enabled, () => query.mode], () => {
  page.value = 1;
});

/** 删除或筛选后当前页可能越界，回退到最后一个有效页。 */
watch(() => filteredRules.value.length, (total) => {
  const maxPage = Math.max(1, Math.ceil(total / pageSize.value));
  if (page.value > maxPage) {
    page.value = maxPage;
  }
});

function changePage(value: number): void {
  page.value = value;
}

function changePageSize(value: number): void {
  pageSize.value = value;
  page.value = 1;
}

function resetFilters(): void {
  query.name = "";
  query.enabled = "";
  query.mode = "";
  selectedSheet.value = "";
}

const operatorLabels = new Map(ruleOperatorOptions.map((item) => [item.value, item.label]));
function operatorLabel(operator: string): string {
  return operatorLabels.get(operator) ?? operator;
}



const detailRule = ref<DraftRuleItem | null>(null);
const detailDependencies = computed(() => {
  const rule = detailRule.value;
  if (!rule) return [];
  const keys = new Set(rule.source_rule_keys ?? []);
  if (rule.source_rule_key) keys.add(rule.source_rule_key);
  const names = [...keys].map((key) =>
    rules.value.find((item) => item.rule_key === key)?.name
    ?? (key === rule.source_rule_key ? rule.source_rule_name : null)
    ?? "未找到引用的规则",
  );
  const calculations = props.config?.calculations ?? [];
  for (const code of new Set(rule.source_rule_codes ?? [])) {
    const matches = calculations.filter((item) => item.result_code === code || item.rule_code === code);
    names.push(matches.length === 1 ? matches[0]!.name || code : code);
  }
  return names;
});

function describeError(error: unknown): string {
  return error instanceof Error ? error.message : "操作失败";
}

watch(() => props.scenarioCode, () => {
  resetFilters();
  detailRule.value = null;
});

function openEditor(ruleKey: string): void {
  if (!props.scenarioCode) {
    return;
  }
  rememberRuleListPaging(props.scenarioCode, page.value, pageSize.value);
  void router.push({
    name: "scene-rule-edit",
    params: { scenarioCode: props.scenarioCode, ruleKey },
  });
}

function editRule(rule: DraftRuleItem): void {
  if (!rule.system) {
    openEditor(rule.rule_key);
    return;
  }
  const fieldKey = editableFieldKey(rule);
  if (fieldKey) emit("editField", fieldKey);
}

async function removeRule(rule: DraftRuleItem): Promise<void> {
  if (!props.scenarioCode) {
    return;
  }
  try {
    await ElMessageBox.confirm(
      `确认删除规则「${rule.name}」？如它是某条风险规则的来源规则，需先处理风险规则。`,
      "删除规则配置",
      { type: "warning", confirmButtonText: "删除", cancelButtonText: "取消" },
    );
  } catch {
    return;
  }
  saving.value = true;
  try {
    await deleteDraftRule(props.scenarioCode, rule.rule_key);
    ElMessage.success("规则已删除");
    emit("changed");
  } catch (error) {
    if (error instanceof ApiError && error.code === "RULE_REFERENCED_BY_RISK") {
      const details = error.details as Record<string, unknown>;
      const dependents = Array.isArray(details.rule_keys) ? (details.rule_keys as string[]) : [];
      ElMessage.warning(`该规则仍是以下风险规则的来源：${dependents.join("、")}。请先调整后再删除。`);
    } else {
      ElMessage.error(describeError(error));
    }
  } finally {
    saving.value = false;
  }
}

function inputText(rule: DraftRuleItem): string {
  if (!rule.inputs.length) {
    return "—";
  }
  return rule.inputs
    .map((item) => `${item.material_name ?? "未知材料"}·${item.field_name ?? "未知字段"}`)
    .join(" ↔ ");
}

function scopeText(rule: DraftRuleItem): string {
  if (rule.materials.length) {
    return rule.materials.map((item) => item.material_name ?? "未知材料").join("、");
  }
  if (rule.outputs.length) {
    return rule.outputs.map((item) => item.field_name ?? item.field_key).join("、");
  }
  return "—";
}
</script>

<template>
  <div>
    <el-alert v-if="!scenarioCode" type="info" show-icon :closable="false" title="请先选择场景" />

    <template v-else>
      <el-form inline class="app-query block-gap-sm">
        <el-form-item label="来源工作表">
          <el-select v-model="selectedSheet" clearable filterable placeholder="全部工作表" style="width: 240px">
            <el-option v-for="sheet in sourceSheetOptions" :key="sheet.value" :value="sheet.value" :label="sheet.label" />
          </el-select>
        </el-form-item>
        <el-form-item label="规则搜索">
          <el-input v-model="query.name" clearable placeholder="规则名称或业务编号" style="width: 220px" />
        </el-form-item>
        <el-form-item label="配置状态">
          <el-select v-model="query.enabled" clearable placeholder="全部" style="width: 190px">
            <el-option value="true" label="启用" />
            <el-option value="PENDING" label="待确认" />
            <el-option value="false" label="停用" />
          </el-select>
        </el-form-item>
        <el-form-item label="执行方式">
          <el-select v-model="query.mode" clearable placeholder="全部" style="width: 150px">
            <el-option v-for="item in EXECUTION_MODE_OPTIONS" :key="item.value" :value="item.value" :label="item.label" />
          </el-select>
        </el-form-item>
      </el-form>

      <div class="app-toolbar">
        <div class="app-toolbar__left">
          <el-button v-if="canEdit" type="primary" @click="openEditor('new')">新增规则</el-button>
          <span v-if="!loadError" class="text-muted">显示 {{ filteredRules.length }} / {{ rules.length }} 条</span>
        </div>
        <div class="app-toolbar__right">
          <el-button :loading="loading" @click="$emit('refresh')">刷新</el-button>
        </div>
      </div>

      <el-alert v-if="loadError" type="error" show-icon :closable="false" :title="`读取失败：${loadError}`">
        <el-button size="small" @click="$emit('refresh')">重试</el-button>
      </el-alert>

      <el-table v-else :data="pagedRules" border stripe class="management-table" v-loading="loading" :empty-text="rules.length ? '未找到匹配的规则' : '暂无规则配置'">
        <el-table-column label="业务编号" width="140" show-overflow-tooltip align="center">
          <template #default="{ row }"><span class="mono">{{ row.source_ref || (row.system ? "系统派生" : "未填写") }}</span></template>
        </el-table-column>
        <el-table-column prop="name" label="规则名称" min-width="180" show-overflow-tooltip align="center">
          <template #default="{ row }">
            <el-button link type="primary" class="table-link" @click="detailRule = row">{{ row.name }}</el-button>
            <div class="table-note">{{ executionModeLabel(row.execution_mode) }}</div>
          </template>
        </el-table-column>
        <el-table-column label="来源工作表" width="204" show-overflow-tooltip>
          <template #default="{ row }">
            {{ sourceSheetLabel(row.origin) }}
          </template>
        </el-table-column>
        <el-table-column label="执行能力" width="152" show-overflow-tooltip align="center">
          <template #default="{ row }">{{ operatorLabel(row.operator) }}</template>
        </el-table-column>
        <el-table-column label="严重级别" width="140" align="center">
          <template #default="{ row }">
            <el-tag v-if="row.risk_level" type="info" size="small" effect="light" disable-transitions>
              {{ row.risk_level }}
            </el-tag>
            <span v-else class="text-hint">非风险规则</span>
          </template>
        </el-table-column>
        <el-table-column label="适用材料" width="180" show-overflow-tooltip>
          <template #default="{ row }">{{ scopeText(row) }}</template>
        </el-table-column>
        <el-table-column label="配置状态" width="160" align="center">
          <template #default="{ row }">
            <el-tag :type="row.config_status === 'PENDING' ? 'warning' : row.enabled ? 'success' : 'info'" size="small" effect="light" disable-transitions>
              {{ configurationLabel(row) }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="操作" :width="actionsWidth" class-name="table-actions" fixed="right" align="center">
          <template #default="{ row }">
            <el-button link type="primary" size="small" @click="detailRule = row">详情</el-button>
            <el-button v-if="canEdit && (!row.system || editableFieldKey(row))" link type="primary" size="small" @click="editRule(row)">编辑</el-button>
            <el-button v-if="canEdit && !row.system" link type="danger" size="small" :disabled="saving" @click="removeRule(row)">删除</el-button>
          </template>
        </el-table-column>
      </el-table>

      <div v-if="filteredRules.length" class="app-pagination">
        <el-pagination
          :current-page="page"
          :page-size="pageSize"
          :total="filteredRules.length"
          :page-sizes="PAGE_SIZE_OPTIONS"
          layout="total, sizes, prev, pager, next, jumper"
          background
          @current-change="changePage"
          @size-change="changePageSize"
        />
      </div>

      <el-drawer :model-value="Boolean(detailRule)" title="规则详情" size="520px" @close="detailRule = null">
        <template v-if="detailRule">
          <el-descriptions :column="1" border size="small">
            <el-descriptions-item label="规则名称">{{ detailRule.name }}</el-descriptions-item>
            <el-descriptions-item label="业务编号">{{ detailRule.source_ref || (detailRule.system ? "系统派生" : "未填写") }}</el-descriptions-item>
            <el-descriptions-item v-if="detailRule.origin" label="原规则库来源">
              <el-button
                v-if="detailRule.origin.import_job_no"
                link
                type="primary"
                @click="router.push({ name: 'scene-import-detail', params: { jobNo: detailRule.origin.import_job_no } })"
              >{{ detailRule.origin.sheet_name }} 第 {{ detailRule.origin.row_no }} 行</el-button>
              <span v-else>{{ detailRule.origin.sheet_name }} 第 {{ detailRule.origin.row_no }} 行</span>
            </el-descriptions-item>
            <el-descriptions-item label="执行方式">{{ executionModeLabel(detailRule.execution_mode) }}</el-descriptions-item>
            <el-descriptions-item label="执行能力">{{ operatorLabel(detailRule.operator) }}</el-descriptions-item>
            <el-descriptions-item label="输入对象">{{ inputText(detailRule) }}</el-descriptions-item>
            <el-descriptions-item v-if="detailDependencies.length" label="依赖规则">
              {{ detailDependencies.join("、") }}
            </el-descriptions-item>
            <el-descriptions-item label="材料范围 / 输出">{{ scopeText(detailRule) }}</el-descriptions-item>
            <el-descriptions-item label="审核要求">{{ detailRule.requirement || "—" }}</el-descriptions-item>
            <el-descriptions-item label="适用条件">
              {{ detailRule.applicability.kind === "MATERIALS_UPLOADED" ? "所引用材料均已上传" : "始终适用" }}
            </el-descriptions-item>
            <el-descriptions-item label="参数">
              <span v-if="Object.keys(detailRule.parameters).length">
                {{ JSON.stringify(detailRule.parameters) }}
              </span>
              <span v-else>—</span>
            </el-descriptions-item>
            <el-descriptions-item v-if="detailRule.risk_level" label="风险等级">{{ detailRule.risk_level }}</el-descriptions-item>
            <el-descriptions-item v-if="detailRule.risk_action" label="规则配置的处置要求">{{ detailRule.risk_action }}</el-descriptions-item>
            <el-descriptions-item label="配置状态">{{ configurationLabel(detailRule) }}</el-descriptions-item>
          </el-descriptions>
        </template>
      </el-drawer>
    </template>
  </div>
</template>
