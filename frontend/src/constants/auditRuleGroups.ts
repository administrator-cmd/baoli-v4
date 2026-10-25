import type { AuditRule } from "@/api/contracts";

/** 规则来源缺失时的显式分组，不能静默并入其他组。 */
export const UNKNOWN_RULE_GROUP = "未标注来源";

/**
 * 分组只依赖任务冻结的真实来源，不按场景或规则编号写死。
 * 来源工作表由导入阶段写入 manifest，合同、财报等场景各自不同。
 */
export function ruleGroupOf(rule: AuditRule): string {
  if (rule.source_sheet) {
    return rule.source_sheet;
  }
  if (rule.kind === "EXTRACTION") {
    return "字段抽取";
  }
  if (rule.kind === "REPORT") {
    return "报告生成";
  }
  return UNKNOWN_RULE_GROUP;
}

/** 抽取与报告是产出类结果，状态码与审核结论不同义；组头计数不把它们算作异常。 */
export function isOutputRule(rule: AuditRule): boolean {
  return rule.kind === "EXTRACTION" || rule.kind === "REPORT";
}

/** 整组都是抽取/报告产出时，默认收起；它们在字段结果、报告页签已有完整视图。 */
export function isOutputRuleGroup(rules: AuditRule[]): boolean {
  return rules.length > 0 && rules.every(isOutputRule);
}
