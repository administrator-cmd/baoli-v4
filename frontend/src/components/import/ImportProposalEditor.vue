<script setup lang="ts">
import { computed, reactive, ref, useId, watch } from "vue";
import { ElMessage } from "element-plus";
import type {
  ConfigOrigin, ImportJob, ImportProposalChanges, ImportProposalGroup, ImportProposalStatus,
  ProposalCalculation, ProposalInterpretationView, ProposalRule,
} from "@/api/contracts";
import { updateImportProposal } from "@/api/imports";
import DrawerPanel from "@/components/DrawerPanel.vue";
import FailureNotice from "@/components/FailureNotice.vue";
import PlanConfigList from "@/components/plans/PlanConfigList.vue";
import ImportSupportingContentPanel from "./ImportSupportingContentPanel.vue";
import { useRequestRunner } from "@/composables/useRequestRunner";
import { sourceSheetLabel } from "@/composables/useRuleSourceFilter";
import { EXECUTION_MODE_OPTIONS, executionModeLabel } from "@/constants/executionModes";
import { formatBusinessValue, periodScopeLabel, statementScopeLabel, valueTypeLabel } from "@/constants/labels";

const props = defineProps<{ job: ImportJob; readonly: boolean }>();
const emit = defineEmits<{ saved: [job: ImportJob]; busy: [value: boolean] }>();
const { pending, failure, run, clear } = useRequestRunner();
const activeGroup = ref<ImportProposalGroup>("rules");
const savingRef = ref<string | null>(null);
const editing = ref<ProposalRow | null>(null);
const editRevision = ref(0);
const findingsExpanded = ref(false);
const findingsPanelId = useId();
const form = reactive({ name: "", text: "", formula: "", required: false, role: "CHECK" as "CHECK" | "RISK", mode: "UNKNOWN", status: "" });

interface ProposalRow {
  interpretation?: ProposalInterpretationView["interpretation"];
  ref: string;
  group: ImportProposalGroup;
  name: string;
  ruleCode?: string | null;
  /** 来源工作表：与场景、方案配置同一口径。 */
  origin?: ConfigOrigin | null;
  /** 适用材料名称，用于材料筛选；筛选下拉也按名称匹配。 */
  materialKeys?: string[];
  text: string;
  context: string;
  formula?: string;
  required?: boolean;
  role?: "CHECK" | "RISK";
  mode?: string;
  status: string | null;
  fieldType?: string;
  unit?: string;
  currency?: string;
  materials?: string;
  inputFields?: string;
  sourceRules?: string;
  outputFields?: string;
  formats?: string;
  details: Array<{ label: string; value: string }>;
}

const groupLabels: Record<ImportProposalGroup, string> = {
  materials: "材料", fields: "字段", rules: "审核规则", calculations: "计算规则", report_sections: "报告章节",
};

function namesText(names: Array<string | null | undefined>): string {
  return [...new Set(names.filter((name): name is string => Boolean(name)))].join("、");
}

function ruleInputs(rule: ProposalRule) {
  return [rule.comparison.left, rule.comparison.right, ...rule.comparison.extra_inputs,
    ...(rule.comparison.addends ?? [])];
}

function ruleMaterialNames(rule: ProposalRule): string[] {
  return [...new Set([...rule.comparison.scope_material_names, ...ruleInputs(rule).map((input) => input?.material_name)]
    .filter((name): name is string => Boolean(name)))];
}

function ruleMaterialsText(rule: ProposalRule): string {
  return namesText(ruleMaterialNames(rule));
}

function ruleFieldsText(rule: ProposalRule): string {
  return namesText(ruleInputs(rule).map((input) => input?.field_name));
}

function ruleRole(rule: ProposalRule): "CHECK" | "RISK" {
  return rule.rule_role === "RISK" || rule.operator === "RISK_TRIGGER" ? "RISK" : "CHECK";
}

function parametersText(item: ProposalCalculation): string {
  return item.parameters.map((parameter) => {
    if (parameter.source_type === "CONSTANT") return `常量 ${formatBusinessValue(parameter.constant_value ?? null)}`;
    const field = props.job.proposals?.fields.find((field) => field.candidate_ref === parameter.source_code);
    const calculation = props.job.proposals?.calculations.find((calculation) => calculation.result_code === parameter.source_code);
    if (field) {
      return [field.name, field.material_name, periodScopeLabel(field.period_scope), statementScopeLabel(field.statement_scope), field.entity_scope]
        .filter(Boolean).join(" · ");
    }
    return calculation?.rule_name ?? "未建立来源绑定";
  }).join("、");
}

function rowsOf(group: ImportProposalGroup): ProposalRow[] {
  const proposals = props.job.proposals;
  if (!proposals) return [];
  switch (group) {
    case "materials": return proposals.materials.map<ProposalRow>((item) => ({
      ref: item.candidate_ref, group: "materials", name: item.name, text: item.description ?? "", interpretation: item.interpretation,
      ruleCode: item.source_id ?? null, origin: item.origin,
      context: item.required ? "必传" : "按需上传", required: item.required, status: null, details: [],
    }));
    case "fields": return proposals.fields.map<ProposalRow>((item) => ({
      ref: item.candidate_ref, group: "fields", name: item.name, text: item.instruction, interpretation: item.interpretation,
      ruleCode: item.source_id ?? null, origin: item.origin,
      materialKeys: item.material_name ? [item.material_name] : [],
      context: item.material_name ?? "未关联材料",
      fieldType: valueTypeLabel(item.value_type), unit: item.unit ?? "", currency: item.currency ?? "",
      required: item.required, status: item.config_status ?? null,
      details: [{ label: "来源材料", value: item.material_name ?? "未关联材料" },
        { label: "字段类型", value: valueTypeLabel(item.value_type) },
        { label: "单位 / 币种", value: [item.unit, item.currency].filter(Boolean).join(" / ") || "未声明" },
        { label: "期间 / 报表 / 主体", value: [periodScopeLabel(item.period_scope), statementScopeLabel(item.statement_scope), item.entity_scope].filter(Boolean).join(" / ") }],
    }));
    case "rules": return proposals.rules.map<ProposalRow>((item) => ({
      ref: item.candidate_ref, group: "rules", name: item.name, text: item.requirement, interpretation: item.interpretation,
      ruleCode: item.rule_code ?? item.source_id ?? null, origin: item.origin,
      materialKeys: ruleMaterialNames(item),
      context: ruleRole(item) === "RISK" ? "风险结论" : "普通核查", role: ruleRole(item), mode: item.execution_mode ?? "UNKNOWN", status: item.config_status ?? null,
      materials: ruleMaterialsText(item),
      inputFields: ruleFieldsText(item),
      sourceRules: namesText([...item.comparison.source_rule_names,
        ...item.comparison.calculation_sources.map((source) => source.rule_name ?? source.result_code)]),
      outputFields: namesText(item.comparison.output_field_names),
      details: [
        { label: "适用材料", value: ruleMaterialsText(item) || "未限定" },
        { label: "输入字段", value: ruleFieldsText(item) || "未声明" },
        { label: "依赖规则 / 计算", value: namesText([...item.comparison.source_rule_names,
          ...item.comparison.calculation_sources.map((source) => source.rule_name ?? source.result_code)]) || "未声明" },
        { label: "输出字段", value: namesText(item.comparison.output_field_names) || "未声明" },
      ],
    }));
    case "calculations": return proposals.calculations.map<ProposalRow>((item) => ({
      ref: item.candidate_ref, group: "calculations", name: item.rule_name, text: item.requirement, interpretation: item.interpretation,
      ruleCode: item.rule_code, origin: item.origin,
      context: parametersText(item), formula: item.formula, status: item.status,
      fieldType: valueTypeLabel(item.result_type), inputFields: parametersText(item),
      details: [{ label: "参数来源", value: parametersText(item) || "未声明" }],
    }));
    case "report_sections": return proposals.report_sections.map<ProposalRow>((item) => ({
      ref: item.candidate_ref, group: "report_sections", name: item.section_name, text: item.content_instruction, interpretation: item.interpretation,
      ruleCode: item.report_code ?? null, origin: item.origin,
      context: item.report_name ?? "未命名报告", status: item.config_status ?? null,
      formats: item.formats.join("、"),
      details: [{ label: "输出格式", value: item.formats.join("、") || "未声明" }],
    }));
    default: return [];
  }
}

const materialRows = computed(() => rowsOf("materials"));
const fieldRows = computed(() => rowsOf("fields"));
const ruleRows = computed(() => rowsOf("rules"));
const calculationRows = computed(() => rowsOf("calculations"));
const sectionRows = computed(() => rowsOf("report_sections"));
const bindingGaps = computed(() => props.job.proposals?.binding_gaps ?? []);
const editingBindingGaps = computed(() => {
  const row = editing.value;
  return row ? bindingGaps.value.filter((gap) => gap.owner_group === row.group && gap.owner_ref === row.ref) : [];
});
const relatedSupportingContent = computed(() => {
  const row = editing.value;
  if (!row) return [];
  return (props.job.proposals?.supporting_content ?? []).filter((item) =>
    item.targets.some((target) => target.group === row.group && target.candidate_ref === row.ref));
});
const rowsWithFindings = computed(() => {
  const gapOwners = new Set(bindingGaps.value.map((gap) => `${gap.owner_group}:${gap.owner_ref}`));
  const rows = [...materialRows.value, ...fieldRows.value, ...ruleRows.value, ...calculationRows.value, ...sectionRows.value];
  return rows.filter((row) => row.interpretation?.missing_details.length || gapOwners.has(`${row.group}:${row.ref}`));
});

/** 材料筛选按名称匹配：表格里的「适用材料 / 所属材料」显示的就是名称。 */
const materialOptions = computed(() => (props.job.proposals?.materials ?? [])
  .map((item) => ({ material_key: item.name, name: item.name })));

function rowSearchText(row: ProposalRow): string {
  return [row.ruleCode, row.name, row.text, row.context, row.formula, row.materials, row.inputFields,
    row.sourceRules, row.outputFields, row.fieldType, row.unit, row.currency, row.formats]
    .filter(Boolean).join(" ");
}

function statusLabel(status: string | null): string {
  if (status === "DISABLED") return "停用";
  if (status === "PENDING") return "待确认";
  if (!status || status === "CLEAR") return "启用";
  return status;
}

function statusTone(status: string | null): "success" | "warning" | "info" {
  return status === "PENDING" ? "warning" : !status || status === "CLEAR" ? "success" : "info";
}

/** 工具栏右侧的启用统计，与方案配置同一写法。 */
function statusSummary(rows: ProposalRow[]): string {
  const enabled = rows.filter((row) => statusLabel(row.status) === "启用").length;
  return `已启用 ${enabled} 条，未启用 ${rows.length - enabled} 条`;
}

function openEditor(row: ProposalRow): void {
  clear();
  editing.value = row;
  editRevision.value = props.job.draft_revision;
  Object.assign(form, { name: row.name, text: row.text, formula: row.formula ?? "", required: row.required ?? false,
    role: row.role ?? "CHECK", mode: row.mode ?? "UNKNOWN", status: row.status ?? "" });
}

async function persist(row: ProposalRow, changes: ImportProposalChanges, revision: number): Promise<boolean> {
  const jobNo = props.job.job_no;
  let saved = false;
  savingRef.value = row.ref;
  emit("busy", true);
  try {
    await run(async () => {
      const updated = await updateImportProposal(jobNo, row.group, row.ref, revision, changes);
      if (props.job.job_no !== jobNo) return;
      emit("saved", updated);
      saved = true;
      ElMessage.success("修改已保存");
    });
  } finally {
    savingRef.value = null;
    emit("busy", false);
  }
  return saved;
}

async function saveEditor(): Promise<void> {
  const row = editing.value;
  if (!row) return;
  if (!form.name.trim()) { ElMessage.warning("请填写名称"); return; }
  const name = form.name.trim();
  let changes: ImportProposalChanges;
  switch (row.group) {
    case "materials": changes = { name, description: form.text, required: form.required }; break;
    case "fields": changes = { name, instruction: form.text, required: form.required }; break;
    case "rules": changes = { name, requirement: form.text, rule_role: form.role, execution_mode: form.mode }; break;
    case "calculations": changes = { rule_name: name, formula: form.formula, requirement: form.text }; break;
    case "report_sections": changes = { section_name: name, content_instruction: form.text }; break;
  }
  if (row.group !== "materials" && form.status !== (row.status ?? "")) {
    const status = form.status as ImportProposalStatus;
    if (row.group === "calculations") changes.status = status;
    else changes.config_status = status;
  }
  if (await persist(row, changes, editRevision.value)) editing.value = null;
}

watch(() => props.job.job_no, () => {
  editing.value = null;
  findingsExpanded.value = false;
  clear();
});
</script>

<template>
  <div class="proposal-workspace">
    <div class="proposal-heading">
      <div>
        <h3>核对解析配置</h3>
        <p>{{ job.status === 'APPLIED' ? '已写入的解析结果，可到场景配置继续调整。' : readonly ? '正在处理，完成后可继续核对。' : '核对业务要求，调整后写入场景草稿。' }}</p>
      </div>
      <el-tag size="small" :type="readonly ? 'info' : 'success'" effect="plain">{{ job.status === 'APPLIED' ? '已写入场景草稿' : readonly ? '处理中，暂不可编辑' : '可编辑' }}</el-tag>
    </div>

    <FailureNotice v-if="!editing" :error="failure" @dismiss="failure = null" />
    <div v-if="rowsWithFindings.length" class="proposal-findings">
      <el-button
        type="warning"
        plain
        :aria-expanded="findingsExpanded"
        :aria-controls="findingsPanelId"
        @click="findingsExpanded = !findingsExpanded"
      >
        {{ findingsExpanded ? '收起解析缺项' : '查看解析缺项' }}（涉及 {{ rowsWithFindings.length }} 条配置）
      </el-button>
      <div v-show="findingsExpanded" :id="findingsPanelId" class="proposal-findings-list">
        <p>点击条目查看缺项和 Excel 原文。请在发布前核对相关业务口径和取数来源。</p>
        <ul>
          <li v-for="row in rowsWithFindings" :key="`${row.group}:${row.ref}`">
            <el-button link type="primary" :disabled="pending" @click="openEditor(row)">{{ row.name }}</el-button>
            <span> · {{ groupLabels[row.group] }}<template v-if="row.ruleCode"> · {{ row.ruleCode }}</template></span>
          </li>
        </ul>
      </div>
    </div>
    <el-tabs v-model="activeGroup">
      <el-tab-pane :label="`材料（${materialRows.length}）`" name="materials" lazy>
        <PlanConfigList
          :items="materialRows" row-key="ref"
          :number-of="(row: ProposalRow) => row.ruleCode || row.name"
          :search-text="rowSearchText" :status-text="(row: ProposalRow) => statusLabel(row.status)"
          keyword-placeholder="材料名称或材料说明" empty-text="本次解析没有材料"
        >
          <el-table-column label="材料编号" width="140" show-overflow-tooltip>
            <template #default="{ row }"><span class="mono">{{ row.ruleCode || "未填写" }}</span></template>
          </el-table-column>
          <el-table-column prop="name" label="材料名称" width="180" show-overflow-tooltip />
          <el-table-column label="业务说明" min-width="300" show-overflow-tooltip>
            <template #default="{ row }">{{ row.text || "未填写" }}</template>
          </el-table-column>
          <el-table-column label="必传" width="88"><template #default="{ row }">{{ row.required ? "是" : "否" }}</template></el-table-column>
          <el-table-column label="配置状态" width="160">
            <template #default="{ row }"><el-tag size="small" :type="statusTone(row.status)">{{ statusLabel(row.status) }}</el-tag></template>
          </el-table-column>
          <el-table-column label="操作" width="84" class-name="table-actions" fixed="right">
            <template #default="{ row }">
              <el-button link type="primary" size="small" :loading="savingRef === row.ref" :disabled="pending" @click="openEditor(row)">{{ readonly ? "查看" : "编辑" }}</el-button>
            </template>
          </el-table-column>
        </PlanConfigList>
      </el-tab-pane>

      <el-tab-pane :label="`字段（${fieldRows.length}）`" name="fields" lazy>
        <PlanConfigList
          :items="fieldRows" row-key="ref"
          :number-of="(row: ProposalRow) => row.ruleCode || row.name"
          :search-text="rowSearchText" :status-text="(row: ProposalRow) => statusLabel(row.status)"
          :materials="materialOptions" :material-keys="(row: ProposalRow) => row.materialKeys ?? []"
          group-label="数据类型" :group-text="(row: ProposalRow) => row.fieldType ?? '未声明'"
          keyword-placeholder="字段名称、字段编号或抽取要求" empty-text="本次解析没有字段"
        >
          <el-table-column label="字段编号" width="140" show-overflow-tooltip>
            <template #default="{ row }"><span class="mono">{{ row.ruleCode || "未填写" }}</span></template>
          </el-table-column>
          <el-table-column prop="name" label="字段名称" width="180" show-overflow-tooltip />
          <el-table-column prop="context" label="所属材料" width="168" show-overflow-tooltip />
          <el-table-column label="数据类型" width="88"><template #default="{ row }">{{ row.fieldType || "未声明" }}</template></el-table-column>
          <el-table-column label="必抽" width="88"><template #default="{ row }">{{ row.required ? "是" : "否" }}</template></el-table-column>
          <el-table-column label="配置状态" width="160">
            <template #default="{ row }"><el-tag size="small" :type="statusTone(row.status)">{{ statusLabel(row.status) }}</el-tag></template>
          </el-table-column>
          <el-table-column label="提取要求" min-width="300" show-overflow-tooltip>
            <template #default="{ row }">{{ row.text || "未填写" }}</template>
          </el-table-column>
          <el-table-column label="操作" width="84" class-name="table-actions" fixed="right">
            <template #default="{ row }">
              <el-button link type="primary" size="small" :loading="savingRef === row.ref" :disabled="pending" @click="openEditor(row)">{{ readonly ? "查看" : "编辑" }}</el-button>
            </template>
          </el-table-column>
        </PlanConfigList>
      </el-tab-pane>

      <el-tab-pane :label="`审核规则（${ruleRows.length}）`" name="rules" lazy>
        <PlanConfigList
          :items="ruleRows" row-key="ref"
          :number-of="(row: ProposalRow) => row.ruleCode || row.name"
          :search-text="rowSearchText" :status-text="(row: ProposalRow) => statusLabel(row.status)"
          :materials="materialOptions" :material-keys="(row: ProposalRow) => row.materialKeys ?? []"
          group-label="来源工作表" :group-text="(row: ProposalRow) => sourceSheetLabel(row.origin)"
          :summary="statusSummary(ruleRows)"
          keyword-placeholder="规则名称、业务编号或审核要求" empty-text="本次解析没有审核规则"
        >
          <el-table-column label="业务编号" width="140" show-overflow-tooltip>
            <template #default="{ row }"><span class="mono">{{ row.ruleCode || "未填写" }}</span></template>
          </el-table-column>
          <el-table-column prop="name" label="规则名称" min-width="180" show-overflow-tooltip />
          <el-table-column label="执行方式" width="112">
            <template #default="{ row }">{{ executionModeLabel(row.mode) }}</template>
          </el-table-column>
          <el-table-column label="来源工作表" width="204" show-overflow-tooltip>
            <template #default="{ row }">{{ sourceSheetLabel(row.origin) }}</template>
          </el-table-column>
          <el-table-column label="适用材料" width="180" show-overflow-tooltip>
            <template #default="{ row }">{{ row.materials || "—" }}</template>
          </el-table-column>
          <el-table-column label="审核要求" min-width="340" show-overflow-tooltip>
            <template #default="{ row }">{{ row.text || "未填写" }}</template>
          </el-table-column>
          <el-table-column label="配置状态" width="160">
            <template #default="{ row }"><el-tag size="small" :type="statusTone(row.status)">{{ statusLabel(row.status) }}</el-tag></template>
          </el-table-column>
          <el-table-column label="操作" width="84" class-name="table-actions" fixed="right">
            <template #default="{ row }">
              <el-button link type="primary" size="small" :loading="savingRef === row.ref" :disabled="pending" @click="openEditor(row)">{{ readonly ? "查看" : "编辑" }}</el-button>
            </template>
          </el-table-column>
        </PlanConfigList>
      </el-tab-pane>

      <el-tab-pane :label="`计算规则（${calculationRows.length}）`" name="calculations" lazy>
        <PlanConfigList
          :items="calculationRows" row-key="ref"
          :number-of="(row: ProposalRow) => row.ruleCode || row.name"
          :search-text="rowSearchText" :status-text="(row: ProposalRow) => statusLabel(row.status)"
          group-label="来源工作表" :group-text="(row: ProposalRow) => sourceSheetLabel(row.origin)"
          :summary="statusSummary(calculationRows)"
          keyword-placeholder="指标名称、规则编号或计算方法" empty-text="本次解析没有计算规则"
        >
          <el-table-column label="规则编号" width="140" show-overflow-tooltip>
            <template #default="{ row }"><span class="mono">{{ row.ruleCode || "未填写" }}</span></template>
          </el-table-column>
          <el-table-column prop="name" label="指标名称" min-width="180" show-overflow-tooltip />
          <el-table-column label="来源工作表" width="204" show-overflow-tooltip>
            <template #default="{ row }">{{ sourceSheetLabel(row.origin) }}</template>
          </el-table-column>
          <el-table-column label="计算方法" min-width="300" show-overflow-tooltip>
            <template #default="{ row }">{{ row.formula || "未填写" }}</template>
          </el-table-column>
          <el-table-column label="业务要求" min-width="300" show-overflow-tooltip>
            <template #default="{ row }">{{ row.text || "未填写" }}</template>
          </el-table-column>
          <el-table-column label="配置状态" width="160">
            <template #default="{ row }"><el-tag size="small" :type="statusTone(row.status)">{{ statusLabel(row.status) }}</el-tag></template>
          </el-table-column>
          <el-table-column label="操作" width="84" class-name="table-actions" fixed="right">
            <template #default="{ row }">
              <el-button link type="primary" size="small" :loading="savingRef === row.ref" :disabled="pending" @click="openEditor(row)">{{ readonly ? "查看" : "编辑" }}</el-button>
            </template>
          </el-table-column>
        </PlanConfigList>
      </el-tab-pane>

      <el-tab-pane :label="`报告章节（${sectionRows.length}）`" name="report_sections" lazy>
        <PlanConfigList
          :items="sectionRows" row-key="ref"
          :number-of="(row: ProposalRow) => row.ruleCode || row.name"
          :search-text="rowSearchText" :status-text="(row: ProposalRow) => statusLabel(row.status)"
          group-label="所属报告" :group-text="(row: ProposalRow) => row.context"
          keyword-placeholder="章节名称、报告编号或生成要求" empty-text="本次解析没有报告章节"
        >
          <el-table-column label="报告编号" width="140" show-overflow-tooltip>
            <template #default="{ row }"><span class="mono">{{ row.ruleCode || "未填写" }}</span></template>
          </el-table-column>
          <el-table-column prop="name" label="章节名称" width="160" show-overflow-tooltip />
          <el-table-column prop="context" label="所属报告" width="192" show-overflow-tooltip />
          <el-table-column label="输出格式" width="140" show-overflow-tooltip>
            <template #default="{ row }">{{ row.formats || "未声明" }}</template>
          </el-table-column>
          <el-table-column label="生成要求" min-width="300" show-overflow-tooltip>
            <template #default="{ row }">{{ row.text || "未填写" }}</template>
          </el-table-column>
          <el-table-column label="配置状态" width="160">
            <template #default="{ row }"><el-tag size="small" :type="statusTone(row.status)">{{ statusLabel(row.status) }}</el-tag></template>
          </el-table-column>
          <el-table-column label="操作" width="84" class-name="table-actions" fixed="right">
            <template #default="{ row }">
              <el-button link type="primary" size="small" :loading="savingRef === row.ref" :disabled="pending" @click="openEditor(row)">{{ readonly ? "查看" : "编辑" }}</el-button>
            </template>
          </el-table-column>
        </PlanConfigList>
      </el-tab-pane>
    </el-tabs>

    <DrawerPanel :open="Boolean(editing)" :title="`${readonly ? '查看' : '编辑'}${editing ? groupLabels[editing.group] : ''}`" size="min(640px, 100vw)" @close="editing = null">
      <template v-if="editing">
        <FailureNotice :error="failure" @dismiss="failure = null" />
        <div v-if="editing.interpretation" class="proposal-interpretation">
          <strong>AI 识别依据（解析时）</strong><p>{{ editing.interpretation.basis }}</p>
          <p v-if="editing.interpretation.missing_details.length">解析时未明确：{{ editing.interpretation.missing_details.join('；') }}</p>
          <p v-if="editing.interpretation.edited_after_interpretation" class="text-hint">此后已有人工修改，请以当前配置内容为准。</p>
        </div>
        <div v-if="editingBindingGaps.length" class="proposal-interpretation">
          <strong>解析时缺少的输入</strong>
          <p>如果执行时仍缺少取数依据，相关规则会记录为证据不足。</p>
          <ul>
            <li v-for="(gap, index) in editingBindingGaps" :key="index">
              <strong>{{ gap.missing_input }}</strong>
              <p>{{ gap.reason }}</p>
              <p v-if="gap.original_requirement" class="text-hint">原文要求：{{ gap.original_requirement }}</p>
            </li>
          </ul>
        </div>
        <el-form label-position="top" :disabled="readonly || pending" class="proposal-edit-form" @submit.prevent="saveEditor">
          <el-form-item label="名称" required><el-input v-model="form.name" /></el-form-item>
          <div class="proposal-edit-meta">
            <el-form-item v-if="editing.group !== 'materials'" label="配置状态">
              <el-select v-model="form.status" placeholder="未标注（当前启用）">
                <el-option v-if="form.status && !['CLEAR', 'PENDING', 'DISABLED'].includes(form.status)" :value="form.status" :label="form.status" />
                <el-option value="CLEAR" label="启用" /><el-option value="PENDING" label="待确认" /><el-option value="DISABLED" label="停用" />
              </el-select>
            </el-form-item>
            <el-form-item v-if="editing.group === 'rules'" label="执行方式">
              <el-select v-model="form.mode">
                <el-option v-for="item in EXECUTION_MODE_OPTIONS" :key="item.value" :value="item.value" :label="item.label" />
              </el-select>
            </el-form-item>
            <el-form-item v-if="editing.group === 'rules'" label="规则类型"><el-select v-model="form.role"><el-option value="CHECK" label="普通核查" /><el-option value="RISK" label="风险结论" /></el-select></el-form-item>
            <el-form-item v-if="editing.group === 'materials' || editing.group === 'fields'" :label="editing.group === 'materials' ? '必须上传' : '必需字段'"><el-switch v-model="form.required" /></el-form-item>
          </div>
          <el-form-item v-if="editing.group === 'calculations'" label="计算方法"><el-input v-model="form.formula" type="textarea" :rows="4" /></el-form-item>
          <el-form-item :label="({ materials: '材料说明', fields: '抽取要求', rules: '审核要求', calculations: '业务要求', report_sections: '生成要求' })[editing.group]"><el-input v-model="form.text" type="textarea" :rows="9" resize="vertical" /></el-form-item>
        </el-form>
        <el-descriptions v-if="editing.details.length" :column="2" direction="vertical" size="small" border class="proposal-context">
          <el-descriptions-item v-for="detail in editing.details" :key="detail.label" :label="detail.label">{{ detail.value }}</el-descriptions-item>
        </el-descriptions>
        <ImportSupportingContentPanel :key="`${editing.group}:${editing.ref}`" :origin="editing.origin" :items="relatedSupportingContent" />
      </template>
      <template #footer><el-button @click="editing = null">{{ readonly ? '关闭' : '取消' }}</el-button><el-button v-if="!readonly" type="primary" :loading="pending" @click="saveEditor">保存修改</el-button></template>
    </DrawerPanel>
  </div>
</template>

<style scoped>
.proposal-heading { display: flex; align-items: center; justify-content: space-between; gap: 12px; margin-bottom: 10px; }
.proposal-heading h3 { margin: 0; font-size: 15px; font-weight: 600; }
.proposal-heading p { margin: 4px 0 0; color: var(--el-text-color-secondary); font-size: 12px; }
.proposal-edit-meta { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 16px; }
.proposal-edit-form :deep(.el-select) { width: 100%; }
.proposal-context { margin-top: 12px; }
.proposal-interpretation { margin: 0 0 18px; padding: 12px; background: var(--el-fill-color-light); }
.proposal-interpretation p { margin: 6px 0 0; white-space: pre-wrap; overflow-wrap: anywhere; line-height: 1.7; }
.proposal-findings { margin: 8px 0; color: var(--el-text-color-secondary); font-size: 13px; line-height: 1.6; }
.proposal-findings p { margin: 6px 0; }
.proposal-findings ul, .proposal-interpretation ul { margin: 8px 0 0; padding-left: 20px; }
.proposal-findings li, .proposal-interpretation li { margin-bottom: 6px; overflow-wrap: anywhere; }
.proposal-findings-list :deep(.el-button) { white-space: normal; height: auto; text-align: left; line-height: inherit; }
.proposal-context :deep(.el-descriptions__content) { overflow-wrap: anywhere; white-space: pre-wrap; }
@media (max-width: 640px) {
  .proposal-heading { align-items: flex-start; }
  .proposal-edit-meta { grid-template-columns: 1fr; gap: 0; }
}
</style>
