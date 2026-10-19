<script setup lang="ts">
import { computed, reactive, ref, watch } from "vue";
import { useRouter } from "vue-router";
import { ElMessage, ElMessageBox } from "element-plus";

import type { DraftRuleItem } from "@/api/contracts";
import { ApiError } from "@/api/http";
import { deleteDraftRule, getSceneConfiguration, setDraftRuleEnabled } from "@/api/scenes";
import { ruleOperatorOptions } from "@/constants/labels";
import { sourceSheetLabel, useRuleSourceFilter } from "@/composables/useRuleSourceFilter";

/**
 * 规则配置面板：业务规则可增删改；自动抽取与报告生成规则由系统派生，只读。
 * 审核要求、输入对象、参数与适用条件都来自真实配置；发布时由后端做同口径编译校验。
 */
const props = defineProps<{
  scenarioCode: string | null;
  /** 由父页面触发的外部刷新（例如发布后）。 */
  refreshToken?: number;
}>();

const emit = defineEmits<{
  changed: [];
}>();

const router = useRouter();

const rules = ref<DraftRuleItem[]>([]);
const loading = ref(false);
const loadError = ref<string | null>(null);
const saving = ref(false);
let loadSequence = 0;

const canEdit = computed(() => Boolean(props.scenarioCode));

/** 来源工作表、名称 / 业务编号与启用状态可组合筛选。 */
const query = reactive({ name: "", enabled: "" });
const { selectedSheet, sourceSheetOptions, matchesSourceSheet } = useRuleSourceFilter(() => rules.value);
const keyword = computed(() => query.name.trim().toLowerCase());
const filteredRules = computed(() =>
  rules.value.filter((item) => {
    if (!matchesSourceSheet(item)) return false;
    if (query.enabled === "true" && !item.enabled) return false;
    if (query.enabled === "false" && item.enabled) return false;
    if (keyword.value && !`${item.name}\n${item.source_ref ?? ""}`.toLowerCase().includes(keyword.value)) return false;
    return true;
  }),
);

function resetFilters(): void {
  query.name = "";
  query.enabled = "";
  selectedSheet.value = "";
}

const operatorLabels = new Map(ruleOperatorOptions.map((item) => [item.value, item.label]));
function operatorLabel(operator: string): string {
  return operatorLabels.get(operator) ?? operator;
}



const detailRule = ref<DraftRuleItem | null>(null);

function describeError(error: unknown): string {
  return error instanceof Error ? error.message : "操作失败";
}

async function load(): Promise<void> {
  const sequence = ++loadSequence;
  if (!props.scenarioCode) {
    rules.value = [];
    return;
  }
  loading.value = true;
  loadError.value = null;
  try {
    const payload = await getSceneConfiguration(props.scenarioCode);
    if (sequence !== loadSequence) return;
    rules.value = payload.rules;
  } catch (error) {
    if (sequence !== loadSequence) return;
    rules.value = [];
    loadError.value = error instanceof Error ? error.message : "规则配置读取失败";
  } finally {
    if (sequence === loadSequence) loading.value = false;
  }
}

watch(() => [props.scenarioCode, props.refreshToken], () => void load(), { immediate: true });
watch(() => props.scenarioCode, () => {
  resetFilters();
  detailRule.value = null;
});

function openEditor(ruleKey: string, options?: { from?: string }): void {
  if (!props.scenarioCode) {
    return;
  }
  void router.push({
    name: "scene-rule-edit",
    params: { scenarioCode: props.scenarioCode, ruleKey },
    query: options?.from ? { from: options.from } : {},
  });
}

async function toggleEnabled(rule: DraftRuleItem): Promise<void> {
  if (!props.scenarioCode) {
    return;
  }
  saving.value = true;
  try {
    await setDraftRuleEnabled(props.scenarioCode, rule.rule_key, !rule.enabled);
    ElMessage.success(rule.enabled ? "规则已停用" : "规则已启用");
    await load();
    emit("changed");
  } catch (error) {
    ElMessage.error(describeError(error));
  } finally {
    saving.value = false;
  }
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
    await load();
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
        <el-form-item label="启用状态">
          <el-select v-model="query.enabled" clearable placeholder="全部" style="width: 120px">
            <el-option value="true" label="启用" />
            <el-option value="false" label="停用" />
          </el-select>
        </el-form-item>
        <el-form-item>
          <el-button @click="resetFilters">重置</el-button>
        </el-form-item>
      </el-form>

      <div class="app-toolbar">
        <div class="app-toolbar__left">
          <el-button v-if="canEdit" type="primary" @click="openEditor('new')">新增规则</el-button>
          <span v-if="!loadError" class="text-muted">显示 {{ filteredRules.length }} / {{ rules.length }} 条</span>
        </div>
        <div class="app-toolbar__right">
          <el-button :loading="loading" @click="load">刷新</el-button>
        </div>
      </div>

      <el-alert v-if="loadError" type="error" show-icon :closable="false" :title="`读取失败：${loadError}`">
        <el-button size="small" @click="load">重试</el-button>
      </el-alert>

      <el-table v-else :data="filteredRules" border stripe v-loading="loading" :empty-text="rules.length ? '未找到匹配的规则' : '暂无规则配置'">
        <el-table-column type="index" label="序号" width="60" />
        <el-table-column label="业务编号" width="155" show-overflow-tooltip>
          <template #default="{ row }"><span class="mono">{{ row.source_ref || (row.system ? "系统派生" : "未填写") }}</span></template>
        </el-table-column>
        <el-table-column prop="name" label="规则名称" min-width="190" show-overflow-tooltip>
          <template #default="{ row }">
            <el-button link type="primary" class="table-link" @click="detailRule = row">{{ row.name }}</el-button>
          </template>
        </el-table-column>
        <el-table-column label="来源工作表" min-width="180" show-overflow-tooltip>
          <template #default="{ row }">
            {{ sourceSheetLabel(row.origin) }}<span v-if="row.origin?.sheet_name" class="text-hint"> · 第 {{ row.origin.row_no }} 行</span>
          </template>
        </el-table-column>
        <el-table-column label="执行方式" width="170" show-overflow-tooltip>
          <template #default="{ row }">{{ operatorLabel(row.operator) }}</template>
        </el-table-column>
        <el-table-column label="严重级别" width="100">
          <template #default="{ row }">
            <el-tag v-if="row.risk_level" type="info" size="small" effect="light" disable-transitions>
              {{ row.risk_level }}
            </el-tag>
            <span v-else class="text-hint">非风险规则</span>
          </template>
        </el-table-column>
        <el-table-column label="适用材料" min-width="150" show-overflow-tooltip>
          <template #default="{ row }">{{ scopeText(row) }}</template>
        </el-table-column>
        <el-table-column label="依赖规则" min-width="150" show-overflow-tooltip>
          <template #default="{ row }">{{ row.source_rule_name || "—" }}</template>
        </el-table-column>
        <el-table-column label="启用状态" width="100">
          <template #default="{ row }">
            <el-tag :type="row.enabled ? 'success' : 'info'" size="small" effect="light" disable-transitions>
              {{ row.enabled ? "启用" : "停用" }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="操作" :width="canEdit ? 260 : 80" fixed="right">
          <template #default="{ row }">
            <el-button link type="primary" size="small" @click="detailRule = row">详情</el-button>
            <template v-if="canEdit && !row.system">
              <el-button link type="primary" size="small" @click="openEditor(row.rule_key)">编辑</el-button>
              <el-button v-if="row.operator !== 'LLM_EXTRACT'" link type="primary" size="small" @click="openEditor('new', { from: row.rule_key })">复制</el-button>
              <el-button link type="primary" size="small" :disabled="saving" @click="toggleEnabled(row)">
                {{ row.enabled ? "停用" : "启用" }}
              </el-button>
              <el-button link type="danger" size="small" @click="removeRule(row)">删除</el-button>
            </template>
          </template>
        </el-table-column>
      </el-table>

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
            <el-descriptions-item label="执行方式">{{ operatorLabel(detailRule.operator) }}</el-descriptions-item>
            <el-descriptions-item label="输入对象">{{ inputText(detailRule) }}</el-descriptions-item>
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
            <el-descriptions-item v-if="detailRule.source_rule_name" label="来源规则">
              {{ detailRule.source_rule_name }}
            </el-descriptions-item>
            <el-descriptions-item v-if="detailRule.risk_level" label="风险等级">{{ detailRule.risk_level }}</el-descriptions-item>
            <el-descriptions-item v-if="detailRule.risk_action" label="处理建议">{{ detailRule.risk_action }}</el-descriptions-item>
            <el-descriptions-item label="启用状态">{{ detailRule.enabled ? "启用" : "停用" }}</el-descriptions-item>
          </el-descriptions>
        </template>
      </el-drawer>
    </template>
  </div>
</template>
