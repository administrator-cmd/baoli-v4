<script setup lang="ts">
import { computed, reactive, ref, watch } from "vue";
import { ElMessage } from "element-plus";
import { ArrowDown, Edit, Search } from "@element-plus/icons-vue";
import type {
  ImportJob, ImportProposalChanges, ImportProposalGroup, ImportProposalStatus,
  ProposalCalculation, ProposalRule,
} from "@/api/contracts";
import { updateImportProposal } from "@/api/imports";
import DrawerPanel from "@/components/DrawerPanel.vue";
import FailureNotice from "@/components/FailureNotice.vue";
import { useRequestRunner } from "@/composables/useRequestRunner";
import { formatBusinessValue, valueTypeLabel } from "@/constants/labels";

const props = defineProps<{ job: ImportJob; readonly: boolean }>();
const emit = defineEmits<{ saved: [job: ImportJob]; busy: [value: boolean] }>();
const { pending, failure, run, clear } = useRequestRunner();
const activeGroup = ref<ImportProposalGroup>("rules");
const query = ref("");
const statusFilter = ref("all");
const savingRef = ref<string | null>(null);
const editing = ref<ProposalRow | null>(null);
const editRevision = ref(0);
const form = reactive({ name: "", text: "", formula: "", required: false, role: "CHECK" as "CHECK" | "RISK", status: "" });

interface ProposalRow {
  ref: string;
  group: ImportProposalGroup;
  name: string;
  text: string;
  context: string;
  formula?: string;
  required?: boolean;
  role?: "CHECK" | "RISK";
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

function proposalRowClass({ row }: { row: ProposalRow }): string {
  return ["DISABLED", "PENDING"].includes(row.status ?? "") ? "proposal-row--paused" : "";
}

const groupLabels: Record<ImportProposalGroup, string> = {
  materials: "材料", fields: "字段", rules: "审核规则", calculations: "计算规则", report_sections: "报告章节",
};
const tabs = computed(() => (Object.keys(groupLabels) as ImportProposalGroup[]).map((group) => ({
  group, label: groupLabels[group], count: props.job.proposals?.[group].length ?? 0,
})));
const supportsStatus = computed(() => activeGroup.value !== "materials");
const textLabel = computed(() => ({ materials: "材料说明", fields: "抽取要求", rules: "审核要求", calculations: "业务要求", report_sections: "生成要求" })[activeGroup.value]);

function namesText(names: Array<string | null | undefined>): string {
  return [...new Set(names.filter((name): name is string => Boolean(name)))].join("、");
}

function ruleInputs(rule: ProposalRule) {
  return [rule.comparison.left, rule.comparison.right, ...rule.comparison.extra_inputs,
    ...(rule.comparison.addends ?? [])];
}

function ruleMaterialsText(rule: ProposalRule): string {
  return namesText([...rule.comparison.scope_material_names, ...ruleInputs(rule).map((input) => input?.material_name)]);
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
    return field?.name ?? calculation?.rule_name ?? String(parameter.source_code ?? "未声明来源");
  }).join("、");
}

const rows = computed<ProposalRow[]>(() => {
  const proposals = props.job.proposals;
  if (!proposals) return [];
  switch (activeGroup.value) {
    case "materials": return proposals.materials.map<ProposalRow>((item) => ({
      ref: item.candidate_ref, group: "materials", name: item.name, text: item.description ?? "",
      context: item.required ? "必传" : "按需上传", required: item.required, status: null, details: [],
    }));
    case "fields": return proposals.fields.map<ProposalRow>((item) => ({
      ref: item.candidate_ref, group: "fields", name: item.name, text: item.instruction,
      context: item.material_name ?? "未关联材料",
      fieldType: valueTypeLabel(item.value_type), unit: item.unit ?? "", currency: item.currency ?? "",
      required: item.required, status: item.config_status ?? null,
      details: [{ label: "来源材料", value: item.material_name ?? "未关联材料" },
        { label: "字段类型", value: valueTypeLabel(item.value_type) },
        { label: "单位 / 币种", value: [item.unit, item.currency].filter(Boolean).join(" / ") || "未声明" }],
    }));
    case "rules": return proposals.rules.map<ProposalRow>((item) => ({
      ref: item.candidate_ref, group: "rules", name: item.name, text: item.requirement,
      context: ruleRole(item) === "RISK" ? "风险结论" : "普通核查", role: ruleRole(item), status: item.config_status ?? null,
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
      ref: item.candidate_ref, group: "calculations", name: item.rule_name, text: item.requirement,
      context: parametersText(item), formula: item.formula, status: item.status,
      fieldType: valueTypeLabel(item.result_type), inputFields: parametersText(item),
      details: [{ label: "参数来源", value: parametersText(item) || "未声明" }],
    }));
    case "report_sections": return proposals.report_sections.map<ProposalRow>((item) => ({
      ref: item.candidate_ref, group: "report_sections", name: item.section_name, text: item.content_instruction,
      context: item.report_name ?? "未命名报告", status: item.config_status ?? null,
      formats: item.formats.join("、"),
      details: [{ label: "输出格式", value: item.formats.join("、") || "未声明" }],
    }));
  }
});

const hasOutputFields = computed(() => activeGroup.value === "rules" && rows.value.some((row) => row.outputFields));

const filteredRows = computed(() => {
  const term = query.value.trim().toLocaleLowerCase();
  return rows.value.filter((row) => {
    const matchesStatus = !supportsStatus.value || statusFilter.value === "all"
      || (statusFilter.value === "enabled" && row.status !== "DISABLED" && row.status !== "PENDING")
      || row.status === statusFilter.value;
    return matchesStatus && (!term || [row.name, row.text, row.context, row.formula, row.materials, row.inputFields,
      row.sourceRules, row.outputFields, row.fieldType, row.unit, row.currency, row.formats].filter(Boolean).join(" ").toLocaleLowerCase().includes(term));
  });
});

function statusLabel(status: string | null): string {
  if (status === "DISABLED") return "暂停";
  if (status === "PENDING") return "待确认";
  if (!status || status === "CLEAR") return "启用";
  return status;
}

function statusTone(status: string | null): "success" | "warning" | "info" {
  return status === "DISABLED" ? "info" : status === "PENDING" ? "warning" : "success";
}

function openEditor(row: ProposalRow): void {
  clear();
  editing.value = row;
  editRevision.value = props.job.draft_revision;
  Object.assign(form, { name: row.name, text: row.text, formula: row.formula ?? "", required: row.required ?? false,
    role: row.role ?? "CHECK", status: row.status ?? "" });
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

async function changeStatus(row: ProposalRow, status: ImportProposalStatus): Promise<void> {
  await persist(row, row.group === "calculations" ? { status } : { config_status: status }, props.job.draft_revision);
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
    case "rules": changes = { name, requirement: form.text, rule_role: form.role }; break;
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

watch(activeGroup, () => { query.value = ""; statusFilter.value = "all"; });
watch(() => props.job.job_no, () => { editing.value = null; clear(); });
</script>

<template>
  <div class="proposal-workspace">
    <div class="proposal-heading">
      <div>
        <h3>核对解析配置</h3>
        <p>{{ readonly ? '已写入的解析结果，可到场景配置继续调整。' : '核对业务要求，调整后写入场景草稿。' }}</p>
      </div>
      <el-tag size="small" :type="readonly ? 'info' : 'success'" effect="plain">{{ readonly ? '已写入' : '可编辑' }}</el-tag>
    </div>

    <FailureNotice v-if="!editing" :error="failure" @dismiss="failure = null" />
    <el-tabs v-model="activeGroup" class="proposal-tabs">
      <el-tab-pane v-for="tab in tabs" :key="tab.group" :name="tab.group">
        <template #label><span class="proposal-tab">{{ tab.label }}<el-tag size="small" type="info" effect="plain">{{ tab.count }}</el-tag></span></template>
      </el-tab-pane>
    </el-tabs>

    <div class="proposal-toolbar">
      <el-input v-model="query" size="small" :prefix-icon="Search" clearable :placeholder="`搜索${groupLabels[activeGroup]}名称或要求`" :aria-label="`搜索${groupLabels[activeGroup]}`" class="proposal-search" />
      <el-select v-if="supportsStatus" v-model="statusFilter" size="small" aria-label="按配置状态筛选" class="proposal-filter">
        <el-option value="all" label="全部状态" />
        <el-option value="enabled" label="已启用" />
        <el-option value="DISABLED" label="已暂停" />
        <el-option value="PENDING" label="待确认" />
      </el-select>
      <span class="proposal-visible">显示 {{ filteredRows.length }} / {{ rows.length }} 项</span>
    </div>

    <el-table
      :data="filteredRows" row-key="ref" size="small" border stripe
      class="proposal-table" :tooltip-options="{ popperClass: 'proposal-content-tooltip' }"
      :row-class-name="proposalRowClass"
      :empty-text="rows.length ? '没有匹配的配置，请调整搜索或筛选条件' : `本次解析没有${groupLabels[activeGroup]}`"
    >
      <el-table-column type="index" label="序号" width="52" align="center" />
      <el-table-column prop="name" :label="`${groupLabels[activeGroup]}名称`" min-width="180" show-overflow-tooltip>
        <template #default="{ row }"><el-button link class="proposal-name" :disabled="pending" @click="openEditor(row)">{{ row.name }}</el-button></template>
      </el-table-column>
      <el-table-column v-if="activeGroup === 'rules'" prop="context" label="规则类型" width="94" />
      <el-table-column v-if="activeGroup === 'rules'" prop="materials" label="适用材料" min-width="130" show-overflow-tooltip>
        <template #default="{ row }">{{ row.materials || '—' }}</template>
      </el-table-column>
      <el-table-column v-if="activeGroup === 'rules' || activeGroup === 'calculations'" prop="inputFields" :label="activeGroup === 'rules' ? '输入字段' : '参数来源'" min-width="170" show-overflow-tooltip>
        <template #default="{ row }">{{ row.inputFields || '—' }}</template>
      </el-table-column>
      <el-table-column v-if="activeGroup === 'rules'" prop="sourceRules" label="依赖规则 / 计算" min-width="180" show-overflow-tooltip>
        <template #default="{ row }">{{ row.sourceRules || '—' }}</template>
      </el-table-column>
      <el-table-column v-if="hasOutputFields" prop="outputFields" label="输出字段" min-width="160" show-overflow-tooltip>
        <template #default="{ row }">{{ row.outputFields || '—' }}</template>
      </el-table-column>
      <el-table-column v-if="activeGroup === 'materials' || activeGroup === 'fields'" :label="activeGroup === 'materials' ? '上传要求' : '是否必需'" width="90">
        <template #default="{ row }">{{ row.required ? (activeGroup === 'materials' ? '必传' : '必需') : (activeGroup === 'materials' ? '按需上传' : '非必需') }}</template>
      </el-table-column>
      <el-table-column v-if="activeGroup === 'fields' || activeGroup === 'report_sections'" prop="context" :label="activeGroup === 'fields' ? '来源材料' : '所属报告'" min-width="140" show-overflow-tooltip />
      <el-table-column v-if="activeGroup === 'fields' || activeGroup === 'calculations'" prop="fieldType" :label="activeGroup === 'fields' ? '字段类型' : '结果类型'" width="110" show-overflow-tooltip />
      <el-table-column v-if="activeGroup === 'fields'" prop="unit" label="单位" width="80" show-overflow-tooltip>
        <template #default="{ row }">{{ row.unit || '—' }}</template>
      </el-table-column>
      <el-table-column v-if="activeGroup === 'fields'" prop="currency" label="币种" width="80" show-overflow-tooltip>
        <template #default="{ row }">{{ row.currency || '—' }}</template>
      </el-table-column>
      <el-table-column v-if="activeGroup === 'calculations'" prop="formula" label="计算方法" min-width="200" show-overflow-tooltip />
      <el-table-column v-if="activeGroup === 'report_sections'" prop="formats" label="输出格式" min-width="140" show-overflow-tooltip />
      <el-table-column prop="text" :label="textLabel" min-width="280" show-overflow-tooltip>
        <template #default="{ row }">{{ row.text || '未填写' }}</template>
      </el-table-column>
      <el-table-column v-if="supportsStatus" label="状态" width="142" align="center">
        <template #default="{ row }">
          <el-dropdown trigger="click" :disabled="readonly || pending" @command="changeStatus(row, $event)">
            <el-button link :disabled="readonly || pending" :aria-label="`${row.name}：${statusLabel(row.status)}，修改状态`">
              <el-tag size="small" :type="statusTone(row.status)" effect="light">{{ statusLabel(row.status) }}</el-tag>
              <el-icon v-if="!readonly" class="el-icon--right"><ArrowDown /></el-icon>
            </el-button>
            <template #dropdown><el-dropdown-menu>
              <el-dropdown-item command="CLEAR" :disabled="row.status === 'CLEAR'">启用</el-dropdown-item>
              <el-dropdown-item command="PENDING" :disabled="row.status === 'PENDING'">待确认（不执行）</el-dropdown-item>
              <el-dropdown-item command="DISABLED" :disabled="row.status === 'DISABLED'">暂停</el-dropdown-item>
            </el-dropdown-menu></template>
          </el-dropdown>
        </template>
      </el-table-column>
      <el-table-column label="操作" width="72" fixed="right" align="center">
        <template #default="{ row }"><el-button link size="small" type="primary" :icon="readonly ? undefined : Edit" :loading="savingRef === row.ref" :disabled="pending" @click="openEditor(row)">{{ readonly ? '查看' : '编辑' }}</el-button></template>
      </el-table-column>
    </el-table>

    <DrawerPanel :open="Boolean(editing)" :title="`${readonly ? '查看' : '编辑'}${editing ? groupLabels[editing.group] : ''}`" size="min(640px, 100vw)" @close="editing = null">
      <template v-if="editing">
        <FailureNotice :error="failure" @dismiss="failure = null" />
        <el-form label-position="top" :disabled="readonly || pending" class="proposal-edit-form" @submit.prevent="saveEditor">
          <el-form-item label="名称" required><el-input v-model="form.name" /></el-form-item>
          <div class="proposal-edit-meta">
            <el-form-item v-if="editing.group !== 'materials'" label="状态">
              <el-select v-model="form.status" placeholder="未标注（当前启用）">
                <el-option v-if="form.status && !['CLEAR', 'PENDING', 'DISABLED'].includes(form.status)" :value="form.status" :label="form.status" />
                <el-option value="CLEAR" label="启用" /><el-option value="PENDING" label="待确认（不执行）" /><el-option value="DISABLED" label="暂停" />
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
      </template>
      <template #footer><el-button @click="editing = null">{{ readonly ? '关闭' : '取消' }}</el-button><el-button v-if="!readonly" type="primary" :loading="pending" @click="saveEditor">保存修改</el-button></template>
    </DrawerPanel>
  </div>
</template>

<style scoped>
.proposal-heading { display: flex; align-items: center; justify-content: space-between; gap: 12px; margin-bottom: 10px; }
.proposal-heading h3 { margin: 0; font-size: 15px; font-weight: 600; }
.proposal-heading p { margin: 4px 0 0; color: var(--el-text-color-secondary); font-size: 12px; }
.proposal-tabs :deep(.el-tabs__header) { margin-bottom: 12px; }
.proposal-tabs :deep(.el-tabs__item) { height: 36px; }
.proposal-tab { display: inline-flex; align-items: center; gap: 6px; }
.proposal-toolbar { display: flex; align-items: center; flex-wrap: wrap; gap: 8px; margin-bottom: 10px; }
.proposal-search { width: min(100%, 300px); }
.proposal-filter { width: 140px; }
.proposal-visible { margin-left: auto; color: var(--el-text-color-secondary); font-size: 12px; }
.proposal-table { width: 100%; }
.proposal-table :deep(th.el-table__cell) { background: var(--el-fill-color-light); color: var(--el-text-color-regular); font-weight: 500; }
.proposal-table :deep(.el-table__cell) { padding: 8px 0; }
.proposal-table :deep(.cell) { line-height: 20px; }
.proposal-name { font-size: 13px; font-weight: 500; }
.proposal-table :deep(.proposal-row--paused) { color: var(--el-text-color-secondary); }
.proposal-table :deep(.proposal-row--paused) .proposal-name { color: var(--el-text-color-secondary); }
.proposal-edit-meta { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 16px; }
.proposal-edit-form :deep(.el-select) { width: 100%; }
.proposal-context { margin-top: 12px; }
.proposal-context :deep(.el-descriptions__content) { overflow-wrap: anywhere; white-space: pre-wrap; }
:global(.proposal-content-tooltip) { max-width: min(560px, calc(100vw - 32px)); max-height: 70vh; overflow-y: auto; white-space: pre-wrap; overflow-wrap: anywhere; }
@media (max-width: 640px) {
  .proposal-heading { align-items: flex-start; }
  .proposal-filter { flex: 1; }
  .proposal-search { width: 100%; }
  .proposal-visible { margin-left: 0; }
  .proposal-edit-meta { grid-template-columns: 1fr; gap: 0; }
}
</style>
