import type { AuditRule, JsonValue, RuleCheckComparison, RuleInputSnapshot, RuleStandard } from "@/api/contracts";
import { EXTRACTION_RESULT_STATUSES, riskResultLabel } from "@/constants/auditStatus";
import { evidenceRouteLabel, formatBusinessValue, judgeRouteLabel } from "@/constants/labels";

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

/** 通用产出状态在抽取、计算、报告语境中分别命名，不改变保存的审核结论。 */
export function resultStatusLabel(rule: AuditRule): string | null {
  if (rule.is_risk_rule) return riskResultLabel(manualEffectiveStatus(rule) ?? rule.status);
  if (manuallyReviewed(rule)) return null;
  if (rule.kind === "EXTRACTION") {
    const fields = extractedOf(rule);
    if (fields.length && fields.every((field) => field.status === "NOT_PRESENT")) return "未找到";
    if (EXTRACTION_RESULT_STATUSES.has(rule.status) && fields.length && fields.every((field) =>
      field.normalized_value !== null && field.normalized_value !== undefined && field.normalized_value !== "",
    )) return "已抽取";
    if (rule.status === "COMPUTED") return "抽取已完成";
  }
  if (rule.kind === "REPORT" && rule.status === "COMPUTED") return "正文已生成";
  return null;
}

export function standardOf(rule: AuditRule): RuleStandard | null {
  const value = payloadOf(rule).standard;
  return value && typeof value === "object" && !Array.isArray(value) ? (value as RuleStandard) : null;
}

export function comparisonOf(rule: AuditRule): RuleCheckComparison | null {
  const value = payloadOf(rule).comparison;
  return value && typeof value === "object" && !Array.isArray(value) ? (value as RuleCheckComparison) : null;
}

export function inputsOf(rule: AuditRule): RuleInputSnapshot[] {
  const value = payloadOf(rule).inputs;
  return Array.isArray(value) ? (value as RuleInputSnapshot[]) : [];
}

/** 抽取结果：本规则实际从材料里取到的字段原文与标准化值。 */
export function extractedOf(rule: AuditRule): RuleInputSnapshot[] {
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
  const comparison = comparisonOf(rule);
  for (const label of [comparison?.left_label, comparison?.right_label, ...(comparison?.addend_labels ?? [])]) {
    const name = typeof label === "string" ? label.split("·")[0] : "";
    if (name) {
      names.add(name);
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
  if (standard.missing) {
    return `标准未明确：${standard.missing}`;
  }
  if (standard.operator_label) {
    return `按「${standard.operator_label}」判定`;
  }
  return "本条结果没有记录审核标准";
}

/** 审核标准之下的一行判据明细：检查项目与合格要求、或比较方式与允许差值。 */
export function standardDetail(rule: AuditRule): string {
  const standard = standardOf(rule);
  if (!standard) {
    return "";
  }
  const parts: string[] = [];
  if (standard.check_item_label) {
    parts.push(
      standard.check_requirement_label
        ? `检查项目：${standard.check_item_label}（合格要求：${standard.check_requirement_label}）`
        : `检查项目：${standard.check_item_label}`,
    );
    if (standard.threshold !== null && standard.threshold !== undefined) {
      parts.push(`标准值：${standard.threshold}${standard.threshold_unit ?? ""}`);
    }
    return parts.join(" · ");
  }
  if (standard.comparison_label) {
    parts.push(`比较方式：${standard.comparison_label}`);
  }
  if (standard.threshold !== null && standard.threshold !== undefined) {
    parts.push(`允许差值 ${standard.threshold}${standard.threshold_unit ?? ""}`);
  }
  return parts.join(" · ");
}

/**
 * 判断原因：无法判断要说清究竟缺什么，不能统一写成「证据不足」。
 * 「能力未实现」与「证据不足」是两种不同的缺什么：前者要改配置或等能力接入，
 * 后者要补原文或补字段，混成一句话会让人去补根本没有缺的东西。
 */
export function reasonText(rule: AuditRule): string | null {
  const payload = payloadOf(rule);
  const blocked = payload.blocked_reason;
  const missing = payload.missing_inputs;
  if (blocked === "STANDARD_UNDEFINED") {
    return "标准不明确：缺少已确认的换算口径，系统不代填默认条件";
  }
  if (blocked === "EVIDENCE_REJECTED") {
    return "依据不可用：依赖的材料已被人工确认与材料位不符";
  }
  if (blocked === "MATERIAL_NOT_PREPARED") {
    return "缺少内容：依赖材料的文件处理尚未完成";
  }
  if (blocked === "NO_FIELD_FACT" || blocked === "NO_SUBJECT_VALUE") {
    return "缺少内容：规则引用的字段尚未形成有效取值";
  }
  if (blocked === "SUBJECT_CANDIDATE_MISSING") {
    return "依据不可用：本次没有可用的印章主体候选";
  }
  if (Array.isArray(missing) && missing.length) {
    const inputs = inputsOf(rule);
    const names = missing.map((key) => {
      const input = inputs.find((item) => item.field_key === key);
      return input?.field_name
        ? `${input.material_name || "材料"}·${input.field_name}`
        : "未取得有效值的信息项";
    });
    return `缺少内容：${names.join("、")} 没有可用的取值`;
  }
  return null;
}

/** 本条结果引用的能力：标识、版本与是否已实现。 */
export function capabilityOf(rule: AuditRule): Record<string, JsonValue> | null {
  const value = payloadOf(rule).capability;
  return value && typeof value === "object" && !Array.isArray(value) ? (value as Record<string, JsonValue>) : null;
}

export function materialTypeNotice(rule: AuditRule): string | null {
  const notice = payloadOf(rule).material_type_notice;
  return typeof notice === "string" && notice ? notice : null;
}

export function routeText(rule: AuditRule): string {
  const route = payloadOf(rule).route;
  if (!route || typeof route !== "object" || Array.isArray(route)) {
    return "取证方式未记录";
  }
  const record = route as Record<string, JsonValue>;
  return `${evidenceRouteLabel(record.evidence as string | null)} · ${judgeRouteLabel(record.judge as string | null)}`;
}

export function applicabilityText(rule: AuditRule): string {
  const applicability = payloadOf(rule).applicability;
  if (!applicability || typeof applicability !== "object" || Array.isArray(applicability)) {
    return "适用条件未记录";
  }
  const record = applicability as Record<string, JsonValue>;
  return typeof record.label === "string" && record.label ? record.label : "适用条件未记录";
}

export function manualNote(rule: AuditRule): string | null {
  const note = payloadOf(rule).manual_business_note;
  return typeof note === "string" && note ? note : null;
}

export function manuallyReviewed(rule: AuditRule): boolean {
  return typeof payloadOf(rule).manual_review_no === "string";
}

/** 人工改判后的有效状态；服务端写入 manual_effective_status。 */
export function manualEffectiveStatus(rule: AuditRule): string | null {
  const value = payloadOf(rule).manual_effective_status;
  return typeof value === "string" && value ? value : null;
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
