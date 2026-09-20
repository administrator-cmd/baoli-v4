<script setup lang="ts">
import { computed, reactive, ref, watch } from "vue";
import { useRouter } from "vue-router";
import { ElMessage, ElMessageBox } from "element-plus";

import type { DraftRuleItem, ScenarioDraftView } from "@/api/contracts";
import { ApiError } from "@/api/http";
import { deleteDraftRule, getSceneDraft, setDraftRuleEnabled } from "@/api/scenes";
import { nodeLabel, ruleOperatorOptions } from "@/constants/labels";

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
const draft = ref<ScenarioDraftView | null>(null);
const loading = ref(false);
const loadError = ref<string | null>(null);
const saving = ref(false);

const canEdit = computed(() => Boolean(props.scenarioCode));

const query = reactive({ name: "", node: "", kind: "BUSINESS" as "BUSINESS" | "SYSTEM" | "ALL", enabled: "" });
const filteredRules = computed(() =>
  rules.value.filter((item) => {
    if (query.kind === "BUSINESS" && item.system) return false;
    if (query.kind === "SYSTEM" && !item.system) return false;
    if (query.node && item.node !== query.node) return false;
    if (query.enabled === "true" && !item.enabled) return false;
    if (query.enabled === "false" && item.enabled) return false;
    if (query.name.trim() && !item.name.includes(query.name.trim())) return false;
    return true;
  }),
);

const operatorLabels = new Map(ruleOperatorOptions.map((item) => [item.value, item.label]));
function operatorLabel(operator: string): string {
  return operatorLabels.get(operator) ?? operator;
}

/** 风险等级只区分高/中/低与待确认；与风险结果页保持同一配色口径。 */
function riskTagType(level: string | null): "danger" | "warning" | "info" {
  if (level === "高") return "danger";
  if (level === "中") return "warning";
  return "info";
}

const nodeOptions = computed(() => {
  const codes = Array.from(new Set(rules.value.map((item) => item.node)));
  return codes.map((code) => ({ value: code, label: nodeLabel(code) }));
});

const detailRule = ref<DraftRuleItem | null>(null);

function describeError(error: unknown): string {
  return error instanceof Error ? error.message : "操作失败";
}

async function load(): Promise<void> {
  if (!props.scenarioCode) {
    rules.value = [];
    draft.value = null;
    return;
  }
  loading.value = true;
  loadError.value = null;
  try {
    const payload = await getSceneDraft(props.scenarioCode);
    draft.value = payload;
    rules.value = payload.rules;
  } catch (error) {
    rules.value = [];
    loadError.value = error instanceof Error ? error.message : "规则配置读取失败";
  } finally {
    loading.value = false;
  }
}

watch(() => [props.scenarioCode, props.refreshToken], () => void load(), { immediate: true });

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
        <el-form-item label="规则名称">
          <el-input v-model="query.name" clearable placeholder="请输入规则名称" style="width: 200px" />
        </el-form-item>
        <el-form-item label="检查阶段">
          <el-select v-model="query.node" clearable placeholder="全部阶段" style="width: 160px">
            <el-option v-for="item in nodeOptions" :key="item.value" :value="item.value" :label="item.label" />
          </el-select>
        </el-form-item>
        <el-form-item label="规则类别">
          <el-select v-model="query.kind" style="width: 140px">
            <el-option value="BUSINESS" label="业务规则" />
            <el-option value="SYSTEM" label="系统规则" />
            <el-option value="ALL" label="全部" />
          </el-select>
        </el-form-item>
        <el-form-item label="启用状态">
          <el-select v-model="query.enabled" clearable placeholder="全部" style="width: 120px">
            <el-option value="true" label="启用" />
            <el-option value="false" label="停用" />
          </el-select>
        </el-form-item>
      </el-form>

      <div class="app-toolbar">
        <div class="app-toolbar__left">
          <el-button v-if="canEdit" type="primary" @click="openEditor('new')">新增规则</el-button>
        </div>
        <div class="app-toolbar__right">
          <el-button :loading="loading" @click="load">刷新</el-button>
        </div>
      </div>

      <el-alert v-if="loadError" type="error" show-icon :closable="false" :title="`读取失败：${loadError}`">
        <el-button size="small" @click="load">重试</el-button>
      </el-alert>

      <el-table v-else :data="filteredRules" border stripe v-loading="loading" empty-text="暂无规则配置">
        <el-table-column type="index" label="序号" width="60" />
        <el-table-column label="规则 key" width="190">
          <template #default="{ row }"><span class="mono">{{ row.rule_key }}</span></template>
        </el-table-column>
        <el-table-column prop="name" label="规则名称" min-width="190" show-overflow-tooltip>
          <template #default="{ row }">
            <el-button link type="primary" class="table-link" @click="detailRule = row">{{ row.name }}</el-button>
          </template>
        </el-table-column>
        <el-table-column label="规则类型" width="120">
          <template #default="{ row }">
            <el-tag :type="row.system ? 'info' : 'primary'" size="small" effect="plain" disable-transitions>
              {{ row.system ? "系统派生" : "业务规则" }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="执行节点" width="120">
          <template #default="{ row }">{{ nodeLabel(row.node) }}</template>
        </el-table-column>
        <el-table-column label="算子" width="170" show-overflow-tooltip>
          <template #default="{ row }">{{ operatorLabel(row.operator) }}</template>
        </el-table-column>
        <el-table-column label="严重级别" width="100">
          <template #default="{ row }">
            <el-tag v-if="row.risk_level" :type="riskTagType(row.risk_level)" size="small" effect="light" disable-transitions>
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
              <el-button link type="primary" size="small" @click="openEditor('new', { from: row.rule_key })">复制</el-button>
              <el-button link type="primary" size="small" :disabled="saving" @click="toggleEnabled(row)">
                {{ row.enabled ? "停用" : "启用" }}
              </el-button>
              <el-button link type="danger" size="small" @click="removeRule(row)">删除</el-button>
            </template>
          </template>
        </el-table-column>
      </el-table>
      <p class="text-hint">
        「是否自动纳入」是方案与任务的概念（方案按依赖自动补入规则），场景本身定义全部规则，因此这里不显示。
        「来源工作表 / 原始行号」只对规则库导入生成的配置有意义，后端在场景配置接口未返回该来源信息，页面不显示。
      </p>

      <el-drawer :model-value="Boolean(detailRule)" title="规则详情" size="520px" @close="detailRule = null">
        <template v-if="detailRule">
          <el-descriptions :column="1" border size="small">
            <el-descriptions-item label="规则名称">{{ detailRule.name }}</el-descriptions-item>
            <el-descriptions-item label="检查阶段">{{ nodeLabel(detailRule.node) }}</el-descriptions-item>
            <el-descriptions-item label="检查方式">{{ operatorLabel(detailRule.operator) }}</el-descriptions-item>
            <el-descriptions-item label="规则类别">{{ detailRule.system ? "系统派生（只读）" : "业务规则" }}</el-descriptions-item>
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
          <p class="text-hint block-gap-sm">
            自动抽取与报告生成规则由字段与固定报告派生，不作为业务规则计数；停用或删除它们会破坏执行依赖，因此只读。
          </p>
        </template>
      </el-drawer>
    </template>
  </div>
</template>
