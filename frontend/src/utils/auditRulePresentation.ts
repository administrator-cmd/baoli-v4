import type { AuditRule, JsonValue, RuleInputSnapshot, RuleStandard } from "@/api/contracts";
import { formatBusinessValue } from "@/constants/labels";
/* 结果负载读取：全部来自服务端 result_json，不做前端推断 */

export function payloadOf(rule: AuditRule): Record<string, JsonValue> {
  return rule.result && typeof rule.result === "object" ? rule.result : {};
}
export function hasBusinessResult(rule: AuditRule): boolean { return payloadOf(rule).presentation_version === 2; }

/** 显示真实业务编号；抽取和报告继承来源编号，内部身份不用于业务定位。 */
export function ruleCodeOf(rule: AuditRule): string | null {
  if (rule.kind === "EXTRACTION" || rule.kind === "REPORT") return rule.source_ref || null;
  const code = standardOf(rule)?.rule_code;
  return (code && code !== rule.rule_key ? code : rule.source_ref) || null;
}

function standardOf(rule: AuditRule): RuleStandard | null {
  const value = payloadOf(rule).standard;
  return value && typeof value === "object" && !Array.isArray(value) ? (value as RuleStandard) : null;
}

function inputsOf(rule: AuditRule): RuleInputSnapshot[] {
  const value = payloadOf(rule).inputs;
  return Array.isArray(value) ? (value as RuleInputSnapshot[]) : [];
}

/** 抽取结果：本规则实际从材料里取到的字段原文与标准化值。 */
function extractedOf(rule: AuditRule): RuleInputSnapshot[] {
  const value = payloadOf(rule).extracted_values;
  return Array.isArray(value) ? (value as RuleInputSnapshot[]) : [];
}

/** 比较规则用 inputs；抽取规则用 extracted_values（它没有参与比较的输入，只有产出）。 */
export function valueSourcesOf(rule: AuditRule): RuleInputSnapshot[] {
  const inputs = inputsOf(rule);
  return inputs.length ? inputs : extractedOf(rule);
}

/** 这条规则是否按材料逐份判定（文件质量检查、材料检查）：此时输入是材料而非字段。 */
export function materialRowsOf(rule: AuditRule): RuleInputSnapshot[] {
  return inputsOf(rule).filter((item) => item.kind === "MATERIAL");
}

export function materialsOf(rule: AuditRule): string {
  const names = new Set<string>(rule.material_names ?? []);
  for (const item of [...inputsOf(rule), ...extractedOf(rule)]) {
    if (item.material_name) {
      names.add(item.material_name);
    }
  }
  return Array.from(names).join("、");
}

/** 审核标准：只在标准不明确时才提示缺什么，不代填默认条件。 */
export function standardText(rule: AuditRule): string {
  const standard = standardOf(rule);
  if (!standard) {
    return rule.configured_requirement
      ? `任务配置要求：${rule.configured_requirement}`
      : "本条结果没有记录审核标准";
  }
  if (standard.requirement) {
    return standard.requirement;
  }
  if (standard.operator_label) {
    return `按「${standard.operator_label}」判定`;
  }
  return "本条结果没有记录审核标准";
}

/** 审核标准之下的一行判据明细：比较方式与允许差值。 */
export function standardDetail(rule: AuditRule): string {
  const standard = standardOf(rule);
  if (!standard) {
    return "";
  }
  const parts: string[] = [];
  if (standard.comparison_label) {
    parts.push(`比较方式：${standard.comparison_label}`);
  }
  if (standard.threshold !== null && standard.threshold !== undefined) {
    parts.push(`允许差值 ${standard.threshold}${standard.threshold_unit ?? ""}`);
  }
  return parts.join(" · ");
}

export function formatInputValue(item: RuleInputSnapshot): string {
  const raw = item.raw_value;
  const normalized = item.normalized_value ?? item.value;
  if (raw && normalized !== null && normalized !== undefined && String(raw) !== String(normalized)) {
    return `${raw}（标准化 ${formatBusinessValue(normalized)}）`;
  }
  const shown = raw ?? normalized;
  if (shown === null || shown === undefined || shown === "") {
    // 没有取值时说明是哪种情况，不统一写成「未提供」：
    // 材料级输入本来就没有字段取值，与字段确实没抽到是两件事。
    return item.kind === "MATERIAL" ? "按材料判定，无字段取值" : "未取得取值";
  }
  return formatBusinessValue(shown);
}
