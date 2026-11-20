import type { AuditRule, Report } from "@/api/contracts";

export const REPORT_GENERATED_LABEL = "已生成";

export function isReportRule(rule: AuditRule): boolean {
  return rule.kind === "REPORT" || rule.execution_mode === "REPORT";
}

/** 报告规则的 COMPUTED 表示正文生成成功，导出格式和审核覆盖范围另有状态。 */
export function isReportRuleGenerated(rule: AuditRule): boolean {
  return isReportRule(rule) && rule.status === "COMPUTED";
}

export const REPORT_EXECUTION_NOTE = "表示生成报告时，生效审核项是否都有执行记录。证据不足也计入已执行，不代表审核通过；报告格式缺项另行说明。";

export function reportRiskState(report: Pick<Report, "risk_configured" | "risk_conclusion_available">) {
  if (!report.risk_configured) return { label: "未配置风险检查", type: "info", tone: "neutral" } as const;
  if (report.risk_conclusion_available) return { label: "结论已齐全", type: "success", tone: "success" } as const;
  return { label: "结论未齐全", type: "warning", tone: "warning" } as const;
}
