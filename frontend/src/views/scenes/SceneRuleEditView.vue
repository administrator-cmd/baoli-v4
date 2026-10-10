<script setup lang="ts">
import { computed, onMounted, reactive, ref } from "vue";
import { useRoute, useRouter } from "vue-router";
import { ElMessage } from "element-plus";
import { Plus, Trash2 } from "lucide-vue-next";

import type { CapabilityItem, DraftCalculationItem, DraftFieldItem, DraftMaterialItem, DraftNodeItem, DraftRuleItem, JsonValue } from "@/api/contracts";
import type { ToolCatalogItem } from "@/api/contracts/system";
import { createDraftRule, getSceneDraft, updateDraftRule, type DraftRulePayload } from "@/api/scenes";
import { getCapabilityCatalog } from "@/api/system";
import BackButton from "@/components/BackButton.vue";
import {
  artifactTypeOptions,
  dateCompareOperatorOptions,
  decimalCompareOperatorOptions,
  decimalSumCompareOperatorOptions,
  riskSourceNodeOptions,
} from "@/constants/labels";

/**
 * 规则编辑（独立页面）：基本信息 → 实际输入对象 → 审核要求与参数 → 适用条件 →（风险规则）来源。
 * 页面只提供后端真正支持的能力：中文下拉与数值表单，不要求填写英文算子、ID 或 JSON。
 */
const route = useRoute();
const router = useRouter();

const scenarioCode = computed(() => String(route.params.scenarioCode ?? ""));
const ruleKey = computed(() => String(route.params.ruleKey ?? "new"));
const isCreate = computed(() => ruleKey.value === "new");

const materials = ref<DraftMaterialItem[]>([]);
const fields = ref<DraftFieldItem[]>([]);
const rules = ref<DraftRuleItem[]>([]);
const calculations = ref<DraftCalculationItem[]>([]);
const nodes = ref<DraftNodeItem[]>([]);
const draftRevision = ref<number | null>(null);
const loading = ref(false);
const loadError = ref<string | null>(null);
const saving = ref(false);
/**
 * 打开既有规则时它的完整参数。表单只呈现一部分键（文件检查的检查项目/标准值/单位、
 * 非 ABS_DIFF_LTE 的金额容差等）。检查方式未变时，保存以此为基础保留未呈现的键。
 */
const loadedParameters = ref<Record<string, JsonValue>>({});
const loadedOperator = ref<string | null>(null);
const activeOperator = ref("FIELD_EQUAL");

/**
 * 检查方式与可选检查阶段：由后端能力目录派生，不在页面维护第二份名单。
 * 目录读取失败时不回退到本地默认名单：那会让页面显示一份后端并不支持的能力集合。
 * 此时明确报错并禁用表单，规则仍可在规则列表里查看。
 */
const catalog = ref<CapabilityItem[]>([]);
const catalogError = ref<string | null>(null);
const catalogLoading = ref(true);

/** 已导入的显式抽取规则可以编辑；新增规则不提供系统派生算子。 */
const SYSTEM_OPERATORS = new Set(["LLM_EXTRACT", "REPORT_RENDER"]);
/** 新业务校验由模型解释方法；已有程序规则保留其当前方式，避免编辑时改写含义。 */
const businessCapabilities = computed(() =>
  catalog.value.filter((item) =>
    item.kind === "OPERATOR" &&
    (item.new_rule_nodes.length > 0 ||
      (!isCreate.value && loadedOperator.value === item.capability_id)) &&
    (!SYSTEM_OPERATORS.has(item.capability_id) ||
      (!isCreate.value && loadedOperator.value === "LLM_EXTRACT" && item.capability_id === "LLM_EXTRACT")),
  ),
);
const OPERATOR_OPTIONS = computed(() =>
  businessCapabilities.value.map((item) => ({
    value: item.capability_id,
    label: item.implemented ? item.label : `${item.label}（当前不可判定）`,
    // 当前不可判定的能力仍可配置，但页面必须写明原因，不能让人以为它会给结论。
    unimplementedReason: item.implemented ? null : item.unimplemented_reason,
  })),
);
const OPERATOR_NODES = computed<Record<string, string[]>>(() =>
  Object.fromEntries(businessCapabilities.value.map((item) => [item.capability_id,
    !isCreate.value && loadedOperator.value === item.capability_id ? item.nodes : item.new_rule_nodes])),
);

/** 风险来源节点 → 允许的来源规则所在节点（与后端一致）。 */
const SOURCE_NODE_RULE_NODES: Record<string, string[]> = {
  MATERIAL_QUALITY: ["MATERIAL_CHECK", "DOCUMENT_PREPARE"],
  INTERNAL_VALIDATE: ["INTERNAL_VALIDATE"],
  CROSS_VALIDATE: ["CROSS_VALIDATE"],
};

const form = reactive({
  name: "",
  source_ref: "",
  operator: "LLM_VALIDATE",
  node: "",
  enabled: true,
  // 材料规则
  material_keys: [] as string[],
  output_field_keys: [] as string[],
  // 比较 / 智能判断输入
  left_material_key: "",
  left_field_key: "",
  right_material_key: "",
  right_field_key: "",
  addends: [] as Array<{ material_key: string; field_key: string }>,
  llm_inputs: [] as Array<{ material_key: string; field_key: string }>,
  evidence_source: "",
  // 参数
  date_operator: "EQ",
  decimal_operator: "EQ",
  threshold: "",
  threshold_unit: "",
  instruction: "",
  artifact_type: "DOCUMENT_PARSE",
  requires_manual_review: false,
  // 审核要求与适用条件
  requirement: "",
  check_level: "",
  applicability_kind: "ALWAYS",
  // 风险规则
  source_node: "",
  source_rule_keys: [] as string[],
  source_rule_codes: [] as string[],
  risk_condition: "",
  risk_level: "",
  risk_action: "",
});

/** 当前检查方式的能力定义；用于展示参数口径与实现状态。 */
const currentCapability = computed(
  () => businessCapabilities.value.find((item) => item.capability_id === form.operator) ?? null,
);

/**
 * 工具目录：模型在规则评估循环里可以选择哪些工具。
 *
 * 它与「能力已实现」是两件事，页面必须分开显示：
 * 材料齐全性、文件质量子项由确定性节点执行（`agent_selectable=false`），
 * 即使已实现也不会出现在模型的工具清单里。把它们显示成「模型可以调用」
 * 会让使用者以为模型能替这些检查做决定。
 */
const toolCatalog = ref<ToolCatalogItem[]>([]);

/** 当前检查方式对应的工具项；未注册时返回 null（页面如实说明）。 */
const currentTool = computed(
  () => toolCatalog.value.find((item) => item.tool_id === form.operator) ?? null,
);

/** 当前检查方式是否会被 Agent 在循环里选择调用。 */
const currentAgentSelectable = computed(() => currentTool.value?.agent_selectable === true);

const nodeTypeByCode = computed(() => new Map(nodes.value.map((node) => [node.node_code, node.node_type])));
const currentNodeType = computed(() => nodeTypeByCode.value.get(form.node) ?? null);
const nodeOptions = computed(() =>
  nodes.value
    .filter((node) => (OPERATOR_NODES.value[form.operator] ?? []).includes(node.node_type))
    .map((node) => ({
      value: node.node_code,
      label: node.name === node.node_type_label ? node.name : `${node.name}（${node.node_type_label}）`,
      disabled: !node.enabled,
    })),
);
function nodeName(code: string): string {
  return nodes.value.find((node) => node.node_code === code)?.name ?? code;
}
/**
 * 表单分支按能力的输入形态与执行方式判断，不按算子码逐个列举——
 * 硬编码名单会让「后端新增算子」变成「能保存但表单少了一块输入」。
 */
const inputKind = computed(() => currentCapability.value?.input_kind ?? null);
/** 需要字段输入的能力（比较、非空、条款存在性、智能判断、金额加总）。 */
const needsFieldInputs = computed(() =>
  ["FIELD_ONE", "FIELD_PAIR", "FIELD_LIST", "FIELD_SET"].includes(inputKind.value ?? ""),
);
/** 按材料位判定、没有字段输入的能力（材料齐全性、文件质量检查）。 */
const isMaterialRule = computed(() => inputKind.value === "MATERIAL_SCOPE");
const isExplicitExtraction = computed(() => !isCreate.value && form.operator === "LLM_EXTRACT");
/** 输入形态是「字段对」的能力；智能判断用 FIELD_SET，加总比较用 FIELD_LIST。 */
const isPairInput = computed(() => inputKind.value === "FIELD_PAIR");
const isAddends = computed(() => form.operator === "DECIMAL_SUM_COMPARE");
const isComparison = computed(() => needsFieldInputs.value && !isLlmValidate.value);
const isLlmValidate = computed(() => currentCapability.value?.execution === "LLM_VALIDATE");
const isRisk = computed(() => currentCapability.value?.execution === "RISK_TRIGGER");
/** 单输入能力按目录声明，不将智能判断限制为一个字段。 */
const singleInput = computed(() => inputKind.value === "FIELD_ONE");
/** 文件检查由导入维护的口径参数：页面只读展示，保存时原样保留。 */
const qualityParameters = computed<Record<string, string>>(() => {
  const result: Record<string, string> = {};
  for (const key of ["standard_value", "unit", "requirement", "criterion"]) {
    const value = loadedParameters.value[key];
    if (typeof value === "string" && value) result[key] = value;
  }
  return result;
});

const leftFields = computed(() => fields.value.filter((item) => item.material_key === form.left_material_key));
const rightFields = computed(() => fields.value.filter((item) => item.material_key === form.right_material_key));

const sourceRuleOptions = computed(() =>
  rules.value
    .filter(
      (item) =>
        !item.system &&
        item.operator !== "RISK_TRIGGER" &&
        (!form.source_node || (SOURCE_NODE_RULE_NODES[form.source_node] ?? []).includes(nodeTypeByCode.value.get(item.node) ?? "")),
    )
    .map((item) => ({ value: item.rule_key, label: `${item.name}（${nodeName(item.node)}）` })),
);
const calculationSourceOptions = computed(() =>
  calculations.value
    .filter((item) => item.result_code && item.status !== "DISABLED")
    .map((item) => ({ value: item.result_code as string, label: `${item.name || item.rule_code || item.result_code}（${item.result_code}）` })),
);

function onOperatorChange(): void {
  if (form.operator !== activeOperator.value) {
    form.threshold = "";
    form.threshold_unit = "";
    form.date_operator = "EQ";
    form.decimal_operator = "EQ";
    form.requires_manual_review = false;
    activeOperator.value = form.operator;
  }
  const available = nodeOptions.value.filter((item) => !item.disabled);
  if (!available.some((item) => item.value === form.node)) {
    form.node = available[0]?.value ?? "";
  }
  // 输入形态按能力声明清空：换成不需要字段输入的能力时，把上一条规则的字段选择清掉，
  // 否则它会以「这条规则选了别的检查用过的字段」的形式留在提交内容里。
  if (!needsFieldInputs.value) {
    form.left_field_key = "";
    form.right_field_key = "";
    form.left_material_key = "";
    form.right_material_key = "";
  }
  if (isLlmValidate.value && !form.llm_inputs.length) {
    form.llm_inputs = [{ material_key: "", field_key: "" }];
  }
  if (isAddends.value) {
    form.right_field_key = "";
    form.right_material_key = "";
    if (!decimalSumCompareOperatorOptions.some((item) => item.value === form.decimal_operator)) {
      form.decimal_operator = "EQ";
    }
    if (!form.addends.length) {
      form.addends = [{ material_key: "", field_key: "" }];
    }
  } else {
    form.addends = [];
  }
  if (inputKind.value === "FIELD_ONE") {
    // 单输入能力不允许右侧输入：留着会让「恰好两个输入」的校验通过并提交多余输入。
    form.right_field_key = "";
    form.right_material_key = "";
  }
  // 阈值与单位只属于数值/日期比较：换成别的检查方式时清掉表单值，
  // 避免把上一条检查的容差带到另一条规则上。
  if (form.operator !== "DATE_COMPARE" && form.operator !== "DECIMAL_COMPARE" && !isAddends.value) {
    form.threshold = "";
    form.threshold_unit = "";
  }
}

function onSourceNodeChange(): void {
  const available = new Set(sourceRuleOptions.value.map((item) => item.value));
  form.source_rule_keys = form.source_rule_keys.filter((key) => available.has(key));
}

async function load(): Promise<void> {
  if (!scenarioCode.value) {
    loadError.value = "缺少场景参数";
    return;
  }
  loading.value = true;
  loadError.value = null;
  try {
    // 能力目录先于草稿加载：检查方式下拉必须来自后端目录，
    // 否则打开一条使用新能力的规则时下拉里找不到它的当前取值。
    await loadCatalog();
    const draft = await getSceneDraft(scenarioCode.value);
    draftRevision.value = draft.revision_no;
    materials.value = draft.materials;
    fields.value = draft.fields;
    rules.value = draft.rules.filter((item) => !item.system);
    calculations.value = draft.calculations;
    nodes.value = draft.nodes;
    if (!isCreate.value) {
      const existing = draft.rules.find((item) => item.rule_key === ruleKey.value);
      if (!existing || existing.system) {
        loadError.value = existing?.system
          ? "系统派生规则（字段抽取 / 报告生成）不能编辑"
          : "规则不存在或已被删除";
        return;
      }
      applyRule(existing);
    } else {
      if (typeof route.query.from === "string" && route.query.from) {
        const source = draft.rules.find((item) => item.rule_key === route.query.from);
        if (source) {
          applyRule(source);
          form.name = `${source.name} 副本`;
          form.source_ref = "";
        }
      }
      onOperatorChange();
    }
  } catch (error) {
    loadError.value = error instanceof Error ? error.message : "草稿读取失败";
  } finally {
    loading.value = false;
  }
}

async function loadCatalog(): Promise<void> {
  catalogLoading.value = true;
  catalogError.value = null;
  try {
    const response = await getCapabilityCatalog();
    catalog.value = response.items;
    toolCatalog.value = response.tools ?? [];
  } catch (error) {
    // 读不到目录时不回退到本地名单：那会让页面显示一份后端并不支持的能力集合，
    // 用户据此保存的规则会在发布时才失败。这里明确报错并由页面禁用保存。
    catalog.value = [];
    toolCatalog.value = [];
    catalogError.value = error instanceof Error ? error.message : "审核能力目录读取失败";
  } finally {
    catalogLoading.value = false;
  }
}

function applyRule(rule: DraftRuleItem): void {
  form.name = rule.name;
  form.source_ref = rule.source_ref ?? "";
  form.operator = rule.operator;
  activeOperator.value = rule.operator;
  loadedOperator.value = rule.operator;
  form.node = rule.node;
  form.enabled = rule.enabled;
  form.requirement = rule.requirement;
  form.check_level = rule.check_level ?? "";
  form.applicability_kind = rule.applicability.kind === "MATERIALS_UPLOADED" ? "MATERIALS_UPLOADED" : "ALWAYS";
  form.material_keys = rule.materials.map((item) => item.material_key);
  form.output_field_keys = rule.outputs.map((item) => item.field_key);
  form.llm_inputs = rule.operator === "LLM_VALIDATE"
    ? rule.inputs.map((item) => ({ material_key: item.material_key ?? "", field_key: item.field_key ?? "" }))
    : [];
  if (rule.inputs.length) {
    form.left_material_key = rule.inputs[0]?.material_key ?? "";
    form.left_field_key = rule.inputs[0]?.field_key ?? "";
    if (rule.inputs.length > 1) {
      form.right_material_key = rule.inputs[1]?.material_key ?? "";
      form.right_field_key = rule.inputs[1]?.field_key ?? "";
    }
    if (rule.operator === "DECIMAL_SUM_COMPARE") {
      form.addends = rule.inputs.length > 1
        ? rule.inputs.slice(1).map((item) => ({ material_key: item.material_key ?? "", field_key: item.field_key ?? "" }))
        : [{ material_key: "", field_key: "" }];
    }
  }
  const parameters = rule.parameters;
  // 打开既有规则时先记住它的完整参数：表单只呈现一部分键，保存时必须把其余键原样带回，
  // 否则一次保存就会丢掉文件检查的标准值与合格要求、非 ABS_DIFF_LTE 的金额容差等配置。
  loadedParameters.value = { ...parameters };
  form.evidence_source = typeof parameters.evidence_source === "string" ? parameters.evidence_source : "";
  // 比较方向存在参数的 operator 键里（与后端契约一致）：日期与金额分别回填到各自的表单字段，
  // 不能按不存在的 date_operator 读取，否则打开规则后保存会静默把方向改成默认值。
  if (rule.operator === "DATE_COMPARE" && typeof parameters.operator === "string") {
    form.date_operator = parameters.operator;
  }
  if ((rule.operator === "DECIMAL_COMPARE" || rule.operator === "DECIMAL_SUM_COMPARE") && typeof parameters.operator === "string") {
    form.decimal_operator = parameters.operator;
  }
  if (parameters.threshold !== undefined && parameters.threshold !== null) {
    form.threshold = String(parameters.threshold);
  }
  if (typeof parameters.threshold_unit === "string") {
    form.threshold_unit = parameters.threshold_unit;
  }
  if (typeof parameters.instruction === "string" && (rule.operator === "LLM_VALIDATE" || rule.operator === "LLM_EXTRACT")) {
    form.instruction = parameters.instruction;
  }
  if (typeof parameters.artifact_type === "string") {
    form.artifact_type = parameters.artifact_type;
  }
  form.requires_manual_review = parameters.requires_manual_review === true;
  if (rule.source_node) {
    form.source_node = rule.source_node;
  }
  form.source_rule_keys = rule.source_rule_keys?.length ? [...rule.source_rule_keys] : rule.source_rule_key ? [rule.source_rule_key] : [];
  form.source_rule_codes = [...(rule.source_rule_codes ?? [])];
  form.risk_condition = rule.risk_condition ?? "";
  if (rule.risk_level) {
    form.risk_level = rule.risk_level;
  }
  if (rule.risk_action) {
    form.risk_action = rule.risk_action;
  }
}

function buildPayload(): DraftRulePayload | string {
  if (!form.name.trim()) {
    return "请填写规则名称";
  }
  if (!nodeOptions.value.some((item) => item.value === form.node && !item.disabled)) {
    return "请选择当前场景中可用的检查节点";
  }
  const inputs: Array<{ field_key: string; material_key: string }> = [];
  if (isLlmValidate.value) {
    if (!form.llm_inputs.length || form.llm_inputs.some((item) => !item.material_key || !item.field_key)) {
      return "智能校验至少需要一个完整的输入字段";
    }
    inputs.push(...form.llm_inputs.map((item) => ({ field_key: item.field_key, material_key: item.material_key })));
    if (currentNodeType.value === "CROSS_VALIDATE" && inputs.length < 2) {
      return "文件交叉校验至少需要两个输入字段";
    }
  } else if (needsFieldInputs.value) {
    if (!form.left_field_key || !form.left_material_key) {
      return "请选择实际输入字段（左侧/唯一输入）";
    }
    inputs.push({ field_key: form.left_field_key, material_key: form.left_material_key });
    if (isAddends.value) {
      if (!form.addends.length || form.addends.some((item) => !item.material_key || !item.field_key)) {
        return "金额加总比较至少需要一个完整的加数字段";
      }
      inputs.push(...form.addends.map((item) => ({ field_key: item.field_key, material_key: item.material_key })));
    } else if (!singleInput.value) {
      if (!form.right_field_key || !form.right_material_key) {
        return "请选择右侧输入字段";
      }
      inputs.push({ field_key: form.right_field_key, material_key: form.right_material_key });
    }
    // 输入形态声明为「恰好两个字段」的能力必须真的取到两个输入：
    // 少一个时后端会在发布期以输入数量不合法拒绝，那时代码已经离开这个页面。
    if (isPairInput.value && !singleInput.value && inputs.length !== 2) {
      return "该检查方式需要左右各一个字段";
    }
    if (
      (isPairInput.value || isAddends.value) &&
      !singleInput.value &&
      currentNodeType.value === "INTERNAL_VALIDATE" &&
      new Set(inputs.map((item) => item.material_key)).size > 1
    ) {
      return "文件内部校验的左右字段必须属于同一份材料";
    }
  }
  // 以打开时的完整参数为基础：未在表单呈现的键（标准值、合格要求、criterion、
  // 非 ABS_DIFF_LTE 的金额容差等）原样保留，只覆盖本次真正由表单决定的方向与阈值。
  const parameters: Record<string, JsonValue> = form.operator === loadedOperator.value ? { ...loadedParameters.value } : {};
  for (const key of ["operator", "threshold", "threshold_unit"]) {
    // 先清掉本表单负责的键，再由下面的分支按当前表单值重新写入；
    // 不清掉会让「改成别的方向」后残留旧方向的阈值与单位。
    delete parameters[key];
  }
  if (isMaterialRule.value && !form.material_keys.length) {
    return "请选择适用材料";
  }
  if (isExplicitExtraction.value) {
    if (!form.source_ref.trim()) return "显式字段抽取规则必须保留业务编号";
    if (!form.material_keys.length) return "请选择适用材料";
    if (!form.output_field_keys.length) return "请选择输出字段";
    if (form.output_field_keys.some((key) => !fields.value.some((field) => field.field_key === key && form.material_keys.includes(field.material_key)))) {
      return "输出字段必须属于适用材料";
    }
    if (!form.instruction.trim()) return "请填写字段抽取指令";
    parameters.instruction = form.instruction.trim();
  }
  if (form.operator === "DATE_COMPARE") {
    parameters.operator = form.date_operator;
    // 日期容差（天）与单位在表单里维护：填了才提交，留空表示这条规则不做容差。
    if (form.threshold.trim()) {
      const days = Number(form.threshold);
      if (!Number.isFinite(days) || days < 0) {
        return "日期允许差值必须是不小于 0 的数值（单位：天）";
      }
      parameters.threshold = days;
      parameters.threshold_unit = form.threshold_unit.trim() || "天";
    }
  }
  if (form.operator === "DECIMAL_COMPARE") {
    parameters.operator = form.decimal_operator;
    // 等于 / 不大于 / 不小于同样支持非负允许差值，不只是 ABS_DIFF_LTE：
    // 只在 ABS_DIFF_LTE 分支写阈值会让其它方向的容差在保存时被清掉。
    if (form.threshold.trim()) {
      const threshold = Number(form.threshold);
      if (!Number.isFinite(threshold) || threshold < 0) {
        return "金额允许差值必须是不小于 0 的数值";
      }
      parameters.threshold = form.threshold.trim();
      if (form.threshold_unit.trim()) {
        parameters.threshold_unit = form.threshold_unit.trim();
      }
    } else if (form.decimal_operator === "ABS_DIFF_LTE") {
      return "金额绝对差比较必须填写有效的数值阈值";
    }
  }
  if (isAddends.value) {
    parameters.operator = form.decimal_operator;
    const threshold = Number(form.threshold);
    if (!form.threshold.trim() || !Number.isFinite(threshold) || threshold < 0) {
      return "金额加总比较必须填写不小于 0 的允许差值";
    }
    parameters.threshold = form.threshold.trim();
    if (form.threshold_unit.trim()) parameters.threshold_unit = form.threshold_unit.trim();
  }
  if (form.operator === "LLM_VALIDATE") {
    if (!form.instruction.trim()) {
      return "智能判断必须填写审核要求（instruction）";
    }
    if (!["FIELD_FACTS", "RETRIEVAL"].includes(form.evidence_source)) {
      return "请选择智能校验的判断依据";
    }
    parameters.instruction = form.instruction.trim();
    parameters.evidence_source = form.evidence_source;
  }
  if (form.operator === "FILE_QUALITY_CHECK") {
    parameters.artifact_type = form.artifact_type;
  }
  if ((needsFieldInputs.value || isLlmValidate.value) && form.requires_manual_review) {
    parameters.requires_manual_review = true;
  }
  if (isRisk.value) {
    if (!form.source_rule_keys.length && !form.source_rule_codes.length) {
      return "风险规则必须选择来源检查或计算结果";
    }
    if (!form.risk_condition.trim()) {
      return "请填写完整的风险成立条件";
    }
    if (!form.risk_level.trim()) {
      return "请填写本场景的风险等级";
    }
    if (!form.risk_action.trim()) {
      return "风险规则必须填写处理建议";
    }
    parameters.risk_level = form.risk_level.trim();
    parameters.action = form.risk_action.trim();
  }
  return {
    name: form.name.trim(),
    source_ref: form.source_ref.trim() || null,
    node: form.node,
    operator: form.operator,
    enabled: form.enabled,
    inputs,
    outputs: isExplicitExtraction.value ? [...form.output_field_keys] : [],
    materials: isMaterialRule.value || isExplicitExtraction.value || isLlmValidate.value ? [...form.material_keys] : [],
    parameters,
    requirement: form.requirement,
    check_level: form.check_level.trim() || null,
    risk_condition: isRisk.value ? form.risk_condition : null,
    applicability: { kind: form.applicability_kind },
    source_node: isRisk.value ? form.source_node || null : null,
    source_rule_key: isRisk.value ? form.source_rule_keys[0] ?? null : null,
    source_rule_keys: isRisk.value ? [...form.source_rule_keys] : [],
    source_rule_codes: isRisk.value ? [...form.source_rule_codes] : [],
  };
}

async function submit(): Promise<void> {
  const payload = buildPayload();
  if (typeof payload === "string") {
    ElMessage.warning(payload);
    return;
  }
  saving.value = true;
  try {
    if (isCreate.value) {
      await createDraftRule(scenarioCode.value, payload);
      ElMessage.success("规则已新增到草稿");
    } else {
      await updateDraftRule(scenarioCode.value, ruleKey.value, payload);
      ElMessage.success("规则已保存到草稿");
    }
    await back();
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : "保存失败");
  } finally {
    saving.value = false;
  }
}

/** 返回场景详情的规则 tab。规则配置是场景详情的 tab，不是独立页面。 */
async function back(): Promise<void> {
  await router.push({
    name: "scene-detail",
    params: { scenarioCode: scenarioCode.value },
    query: { tab: "rules" },
  });
}

onMounted(load);
</script>

<template>
  <div class="app-panel app-reading">
    <div class="app-detail-head">
      <BackButton label="返回规则列表" @click="back" />
      <span class="app-detail-head__name">{{ isCreate ? "新增规则" : `编辑规则：${form.name}` }}</span>
      <span v-if="draftRevision !== null" class="text-hint">草稿修订 {{ draftRevision }}</span>
    </div>

    <el-alert v-if="loadError" type="error" show-icon :closable="false" :title="loadError">
      <BackButton label="返回规则列表" @click="back" />
    </el-alert>
    <el-alert
      v-else-if="catalogError"
      type="error"
      show-icon
      :closable="false"
      title="审核能力目录读取失败，暂时不能编辑规则"
      :description="`${catalogError}。页面不会退回内置的检查方式名单——那会显示一份后端并不支持的能力集合。`"
    >
      <el-button size="small" @click="loadCatalog">重新读取能力目录</el-button>
    </el-alert>

    <el-form v-else v-loading="loading || catalogLoading" label-width="120px" class="block-gap-sm">
      <h3 class="app-section-title">基本信息</h3>
      <el-form-item label="规则名称" required>
        <el-input v-model="form.name" maxlength="255" placeholder="按业务填写，例如：结算总额与加总核对" />
      </el-form-item>
      <el-form-item label="规则编号">
        <el-input v-model="form.source_ref" maxlength="80" placeholder="与规则库中的规则编号一致" />
      </el-form-item>
      <el-form-item label="检查方式" required>
        <el-select v-model="form.operator" style="width: 260px" @change="onOperatorChange">
          <el-option v-for="item in OPERATOR_OPTIONS" :key="item.value" :value="item.value" :label="item.label" />
        </el-select>
      </el-form-item>
      <el-form-item label="执行节点" required>
        <el-select v-model="form.node" :disabled="nodeOptions.filter((item) => !item.disabled).length === 1" style="width: 260px">
          <el-option v-for="item in nodeOptions" :key="item.value" :value="item.value" :label="item.label" :disabled="item.disabled" />
        </el-select>
        <span class="text-hint">规则所属的系统执行节点。</span>
        <span v-if="!nodeOptions.some((item) => !item.disabled)" class="text-hint">当前场景没有支持该检查方式的可用节点</span>
      </el-form-item>

      <el-alert
        v-if="currentCapability && !currentCapability.implemented"
        class="block-gap-sm"
        type="warning"
        show-icon
        :closable="false"
        title="该检查方式当前没有判定实现"
      >
        {{ currentCapability.unimplemented_reason || "系统未说明原因" }}。
        规则可以保存，但发布前必须逐条确认，运行时这条检查会明确报「不可判定」，不会给出结论。
      </el-alert>

      <!-- 「已实现」与「模型可以调用」是两件事，分开如实显示：
           由确定性节点执行的检查（材料齐全性、文件质量子项）不会出现在模型的工具清单里。 -->
      <el-alert
        v-if="currentCapability && currentCapability.implemented && !currentAgentSelectable"
        class="block-gap-sm"
        type="info"
        show-icon
        :closable="false"
        title="该检查由固定节点执行，不作为模型可选工具"
      >
        {{ currentTool?.agent_unselectable_reason || "模型在规则评估循环里没有调用它的入口" }}。
        判定仍按确定性程序完成，结论不受模型影响。
      </el-alert>


      <h3 class="app-section-title">实际输入对象</h3>
      <template v-if="isExplicitExtraction">
        <el-form-item label="适用材料" required>
          <el-select v-model="form.material_keys" multiple filterable style="width: 100%" placeholder="选择抽取所用材料">
            <el-option v-for="item in materials" :key="item.material_key" :value="item.material_key" :label="item.name" />
          </el-select>
        </el-form-item>
        <el-form-item label="输出字段" required>
          <el-select v-model="form.output_field_keys" multiple filterable style="width: 100%" placeholder="选择要抽取的字段">
            <el-option v-for="item in fields.filter((field) => form.material_keys.includes(field.material_key))" :key="item.field_key" :value="item.field_key" :label="item.name" />
          </el-select>
        </el-form-item>
      </template>
      <template v-else-if="isMaterialRule">
        <el-form-item label="适用材料" required>
          <el-select v-model="form.material_keys" multiple filterable style="width: 100%" placeholder="选择这条检查覆盖的材料位">
            <el-option v-for="item in materials" :key="item.material_key" :value="item.material_key" :label="item.name" />
          </el-select>
        </el-form-item>
        <el-form-item v-if="form.operator === 'FILE_QUALITY_CHECK'" label="处理产物" required>
          <el-select v-model="form.artifact_type" style="width: 260px">
            <el-option v-for="item in artifactTypeOptions" :key="item.value" :value="item.value" :label="item.label" />
          </el-select>
        </el-form-item>
      </template>

      <template v-else-if="isLlmValidate">
        <el-form-item v-for="(input, index) in form.llm_inputs" :key="index" :label="`输入字段 ${index + 1}`" required>
          <div class="rule-addend-row">
            <el-select v-model="input.material_key" placeholder="材料" class="rule-addend-select" @change="input.field_key = ''">
              <el-option v-for="item in materials" :key="item.material_key" :value="item.material_key" :label="item.name" />
            </el-select>
            <el-select v-model="input.field_key" placeholder="字段" class="rule-addend-select">
              <el-option v-for="item in fields.filter((field) => field.material_key === input.material_key)" :key="item.field_key" :value="item.field_key" :label="item.name" />
            </el-select>
            <el-button :icon="Trash2" :aria-label="`移除输入字段 ${index + 1}`" :title="`移除输入字段 ${index + 1}`" @click="form.llm_inputs.splice(index, 1)" />
          </div>
        </el-form-item>
        <el-form-item>
          <el-button :icon="Plus" @click="form.llm_inputs.push({ material_key: '', field_key: '' })">添加输入字段</el-button>
        </el-form-item>
        <el-form-item label="判断依据" required>
          <el-radio-group v-model="form.evidence_source">
            <el-radio value="FIELD_FACTS">已抽取字段</el-radio>
            <el-radio value="RETRIEVAL">字段与材料正文检索</el-radio>
          </el-radio-group>
        </el-form-item>
        <el-form-item v-if="form.evidence_source === 'RETRIEVAL'" label="补充检索材料">
          <el-select v-model="form.material_keys" multiple filterable style="width: 100%" placeholder="按需补充输入字段所属材料之外的检索范围">
            <el-option v-for="item in materials" :key="item.material_key" :value="item.material_key" :label="item.name" />
          </el-select>
        </el-form-item>
      </template>
      <template v-else-if="isComparison">
        <el-form-item :label="singleInput ? '输入字段' : '左侧字段'" required>
          <el-select v-model="form.left_material_key" placeholder="材料" style="width: 220px" @change="form.left_field_key = ''">
            <el-option v-for="item in materials" :key="item.material_key" :value="item.material_key" :label="item.name" />
          </el-select>
          <el-select v-model="form.left_field_key" placeholder="字段" style="width: 220px; margin-left: 8px">
            <el-option v-for="item in leftFields" :key="item.field_key" :value="item.field_key" :label="item.name" />
          </el-select>
        </el-form-item>
        <template v-if="isAddends">
          <el-form-item v-for="(addend, index) in form.addends" :key="index" :label="`加数字段 ${index + 1}`" required>
            <div class="rule-addend-row">
              <el-select v-model="addend.material_key" placeholder="材料" class="rule-addend-select" @change="addend.field_key = ''">
                <el-option v-for="item in materials" :key="item.material_key" :value="item.material_key" :label="item.name" />
              </el-select>
              <el-select v-model="addend.field_key" placeholder="字段" class="rule-addend-select">
                <el-option
                  v-for="item in fields.filter((field) => field.material_key === addend.material_key)"
                  :key="item.field_key"
                  :value="item.field_key"
                  :label="item.name"
                />
              </el-select>
              <el-button
                :icon="Trash2"
                :disabled="form.addends.length === 1"
                :aria-label="`移除加数字段 ${index + 1}`"
                :title="`移除加数字段 ${index + 1}`"
                @click="form.addends.splice(index, 1)"
              />
            </div>
          </el-form-item>
          <el-form-item>
            <el-button :icon="Plus" @click="form.addends.push({ material_key: '', field_key: '' })">添加加数字段</el-button>
          </el-form-item>
        </template>
        <el-form-item v-else-if="!singleInput" label="右侧字段" required>
          <el-select v-model="form.right_material_key" placeholder="材料" style="width: 220px" @change="form.right_field_key = ''">
            <el-option v-for="item in materials" :key="item.material_key" :value="item.material_key" :label="item.name" />
          </el-select>
          <el-select v-model="form.right_field_key" placeholder="字段" style="width: 220px; margin-left: 8px">
            <el-option v-for="item in rightFields" :key="item.field_key" :value="item.field_key" :label="item.name" />
          </el-select>
        </el-form-item>
      </template>

      <h3 class="app-section-title">审核要求与参数</h3>
      <el-form-item v-if="form.operator === 'DATE_COMPARE'" label="比较方式" required>
        <el-select v-model="form.date_operator" style="width: 220px">
          <el-option v-for="item in dateCompareOperatorOptions" :key="item.value" :value="item.value" :label="item.label" />
        </el-select>
      </el-form-item>
      <template v-if="form.operator === 'DECIMAL_COMPARE'">
        <el-form-item label="比较方式" required>
          <el-select v-model="form.decimal_operator" style="width: 220px">
            <el-option v-for="item in decimalCompareOperatorOptions" :key="item.value" :value="item.value" :label="item.label" />
          </el-select>
        </el-form-item>
        <el-form-item label="差额阈值">
          <el-input v-model="form.threshold" style="width: 220px" placeholder="例如：0.05（保留原样提交，留空表示不做容差）" />
          <span class="text-hint" style="margin-left: 8px">系统按数值比较，不做单位换算</span>
        </el-form-item>
        <el-form-item label="阈值单位">
          <el-input v-model="form.threshold_unit" style="width: 220px" placeholder="可选，例如：元" />
        </el-form-item>
      </template>
      <template v-if="isAddends">
        <el-form-item label="比较方式" required>
          <el-select v-model="form.decimal_operator" style="width: 220px">
            <el-option v-for="item in decimalSumCompareOperatorOptions" :key="item.value" :value="item.value" :label="item.label" />
          </el-select>
        </el-form-item>
        <el-form-item label="允许差值" required>
          <el-input v-model="form.threshold" style="width: 220px" placeholder="请输入非负数" />
        </el-form-item>
        <el-form-item label="差值单位">
          <el-input v-model="form.threshold_unit" style="width: 220px" placeholder="例如：元" />
        </el-form-item>
      </template>
      <template v-if="form.operator === 'DATE_COMPARE'">
        <el-form-item label="允许差值（天）">
          <el-input v-model="form.threshold" style="width: 220px" placeholder="例如：1（留空表示不做容差）" />
          <span class="text-hint" style="margin-left: 8px">日期差值以天为单位，0 也是有效配置</span>
        </el-form-item>
      </template>
      <el-form-item v-if="isMaterialRule && form.operator === 'FILE_QUALITY_CHECK'" label="检查项目">
        <span class="text-hint">
          检查项目、合格要求、标准值与检查口径由规则库导入维护；页面不改动这些业务口径，
          保存时会原样保留。
        </span>
        <div class="text-hint">
          {{ qualityParameters.standard_value ? `标准值：${qualityParameters.standard_value}` : "标准值：未配置" }}
          <template v-if="qualityParameters.unit"> ｜ 单位：{{ qualityParameters.unit }}</template>
          <template v-if="qualityParameters.requirement"> ｜ 合格要求：{{ qualityParameters.requirement }}</template>
        </div>
      </el-form-item>
      <el-form-item v-if="form.operator === 'LLM_VALIDATE'" label="判断要求" required>
        <el-input
          v-model="form.instruction"
          type="textarea"
          :rows="3"
          maxlength="4000"
          placeholder="写给模型判断的要求：判断什么、根据什么材料与字段"
        />
      </el-form-item>
      <el-form-item v-if="isExplicitExtraction" label="抽取指令" required>
        <el-input v-model="form.instruction" type="textarea" :rows="3" maxlength="4000" />
      </el-form-item>
      <el-form-item v-if="isComparison || isLlmValidate" label="人工复核">
        <el-checkbox v-model="form.requires_manual_review">异常时需要人工复核</el-checkbox>
      </el-form-item>

      <el-form-item label="审核要求" :required="!isRisk">
        <el-input
          v-model="form.requirement"
          type="textarea"
          :rows="3"
          maxlength="4000"
          placeholder="业务上对这条检查的要求原文（会随规则一起进入报告与导出）"
        />
        <div class="text-hint">智能校验按提供的字段、结构化记录与业务方法判断；缺少外部数据或尚未实现的能力会明确报错。</div>
      </el-form-item>
      <el-form-item v-if="!isRisk" label="检查等级">
        <el-input v-model="form.check_level" clearable style="width: 220px" placeholder="填写规则库定义的检查等级" />
      </el-form-item>

      <el-form-item label="适用条件">
        <el-radio-group v-model="form.applicability_kind">
          <el-radio value="ALWAYS">始终适用</el-radio>
          <el-radio value="MATERIALS_UPLOADED">所引用材料均已上传</el-radio>
        </el-radio-group>
      </el-form-item>

      <template v-if="isRisk">
        <h3 class="app-section-title">风险来源与建议</h3>
        <el-form-item label="来源节点类型">
          <el-select v-model="form.source_node" clearable style="width: 260px" placeholder="全部可用类型" @change="onSourceNodeChange">
            <el-option v-for="item in riskSourceNodeOptions" :key="item.value" :value="item.value" :label="item.label" />
          </el-select>
        </el-form-item>
        <el-form-item label="来源检查">
          <el-select v-model="form.source_rule_keys" multiple filterable style="width: 100%" placeholder="选择来源检查规则">
            <el-option v-for="item in sourceRuleOptions" :key="item.value" :value="item.value" :label="item.label" />
          </el-select>
        </el-form-item>
        <el-form-item v-if="calculationSourceOptions.length || form.source_rule_codes.length" label="计算结果">
          <el-select v-model="form.source_rule_codes" multiple filterable style="width: 100%" placeholder="选择来源计算结果">
            <el-option v-for="item in calculationSourceOptions" :key="item.value" :value="item.value" :label="item.label" />
          </el-select>
        </el-form-item>
        <el-form-item label="成立条件" required>
          <el-input v-model="form.risk_condition" type="textarea" :rows="3" placeholder="填写规则库中的完整风险成立条件" />
        </el-form-item>
        <el-form-item label="风险等级" required>
          <el-input v-model="form.risk_level" placeholder="填写本场景的风险等级" />
        </el-form-item>
        <el-form-item label="处理建议" required>
          <el-input v-model="form.risk_action" maxlength="500" placeholder="命中风险时的处理建议（会显示在风险结果与报告中）" />
        </el-form-item>
      </template>

      <el-form-item label="启用状态">
        <el-switch v-model="form.enabled" active-text="启用" inactive-text="停用" />
      </el-form-item>

      <div class="app-toolbar">
        <div class="app-toolbar__left">
          <el-button type="primary" :loading="saving" @click="submit">保存到草稿</el-button>
          <el-button @click="back">取消</el-button>
        </div>
        <div class="app-toolbar__right">
          <span class="text-hint">保存只写入草稿；发布为场景版本后才会用于新任务。</span>
        </div>
      </div>
    </el-form>
  </div>
</template>

<style scoped>
.rule-addend-row {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
  width: 100%;
}

.rule-addend-select {
  width: min(220px, 100%);
}
</style>
