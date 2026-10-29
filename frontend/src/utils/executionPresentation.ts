import type { ExecutionRow, JsonValue } from "@/api/contracts";
import { COMPLETED_RESULT_STATUSES } from "@/constants/auditStatus";

/** 本批次保存的逐项结果；不使用任务当前结果替代历史批次。 */
export interface ExecutionOutcome {
  rule_key: string;
  rule_name: string;
  status: string;
  loop_status: string;
  unresolved_items: Array<{ kind: string; message?: string }>;
}

export function executionOutcomes(summary: Record<string, JsonValue> | null): ExecutionOutcome[] {
  return (summary?.rule_outcomes as unknown as ExecutionOutcome[] | undefined) ?? [];
}

/** 缺项与未完成原因：一行展示，不把原始结构摊在表格里。 */
export function unresolvedText(outcome: Pick<ExecutionOutcome, "unresolved_items">): string {
  return outcome.unresolved_items.map((reason) => reason.message ?? reason.kind).join("；") || "—";
}

export function executionSummaryText(row: Pick<ExecutionRow, "result_summary">): string {
  const outcomes = executionOutcomes(row.result_summary);
  const missing = (row.result_summary?.missing_rule_keys as string[] | undefined)?.length ?? 0;
  if (!outcomes.length && !missing) return "尚未记录逐项结果";
  const abnormal = outcomes.filter((item) => item.status === "ABNORMAL").length;
  const undecided = outcomes.filter((item) => !COMPLETED_RESULT_STATUSES.has(item.status)).length + missing;
  return `已记录 ${outcomes.length} 项 · 异常 ${abnormal} 项 · 未形成结论 ${undecided} 项`;
}
