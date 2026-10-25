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

export function executionSummaryText(row: Pick<ExecutionRow, "result_summary">): string {
  const outcomes = executionOutcomes(row.result_summary);
  const missing = (row.result_summary?.missing_rule_keys as string[] | undefined)?.length ?? 0;
  if (!outcomes.length && !missing) return "尚未记录逐项结果";
  const abnormal = outcomes.filter((item) => item.status === "ABNORMAL").length;
  const undecided = outcomes.filter((item) => !COMPLETED_RESULT_STATUSES.has(item.status)).length + missing;
  return `已记录 ${outcomes.length} 项 · 异常 ${abnormal} 项 · 未形成结论 ${undecided} 项`;
}

export function executionDuration(ms: number | null): string {
  if (ms === null) return "—";
  if (ms < 1000) return `${ms} 毫秒`;
  if (ms < 60000) return `${(ms / 1000).toFixed(1)} 秒`;
  const seconds = Math.round(ms / 1000);
  const minutes = Math.floor(seconds / 60);
  return minutes < 60
    ? `${minutes} 分 ${seconds % 60} 秒`
    : `${Math.floor(minutes / 60)} 小时 ${minutes % 60} 分 ${seconds % 60} 秒`;
}
