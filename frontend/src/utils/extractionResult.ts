import type { AuditRule } from "@/api/contracts";

export const extractionLabels = { EXTRACTED: "已抽取", NO_VALUE: "未抽取到" } as const;

export function isExtractionRule(rule: AuditRule): boolean {
  return rule.kind === "EXTRACTION" || rule.execution_mode === "EXTRACT";
}

/** 一条字段抽取规则对应一个字段。优先读取保存的原抽取产物，避免把候选值当成有效值。 */
export function ruleExtractionOutcome(rule: AuditRule): keyof typeof extractionLabels | null {
  if (!isExtractionRule(rule)) return null;
  if (rule.status === "NOT_EXECUTED") return (rule.turn_count ?? 0) > 0 ? "NO_VALUE" : null;
  if (!["COMPUTED", "ABNORMAL", "PASSED", "REVIEWED", "EVIDENCE_INSUFFICIENT", "PENDING_REVIEW"].includes(rule.status)) return null;
  const fields = rule.result?.extracted_values;
  if (Array.isArray(fields) && fields.length) {
    return fields.every(item => item && typeof item === "object" && !Array.isArray(item)
      && (item.status == null || item.status === "PRESENT" || item.status === "REVIEWED")
      && item.normalized_value != null && item.normalized_value !== "") ? "EXTRACTED" : "NO_VALUE";
  }
  // 旧任务可能只保留规则结论，没有字段明细。
  return rule.status === "COMPUTED" ? "EXTRACTED" : "NO_VALUE";
}
