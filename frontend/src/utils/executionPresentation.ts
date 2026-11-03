import type { ExecutionModelTurns, ExecutionRow } from "@/api/contracts";
import { statusLabel, statusTone, type StatusTone } from "@/constants/labels";

/**
 * 执行记录的展示口径。
 *
 * 执行完成不代表审核通过；等待字段核对是正常等待点，不是技术故障。列表与筛选共用
 * 这里的名称，统计数字全部来自后端同一份结构化结果，不从中文错误文本推断分类。
 */

export const EXECUTION_INVALIDATED_LABEL = "已失效";

const executionStatusLabels: Record<string, string> = {
  RUNNING: "执行中",
  WAITING_FIELD_REVIEW: "等待字段核对",
  SUCCEEDED: "执行完成",
  BLOCKED: "执行阻断",
  FAILED: "执行失败",
  SKIPPED: "已跳过",
  INVALIDATED: EXECUTION_INVALIDATED_LABEL,
};

const executionStatusTones: Record<string, StatusTone> = {
  RUNNING: "info",
  WAITING_FIELD_REVIEW: "warning",
  SUCCEEDED: "success",
  BLOCKED: "danger",
  FAILED: "danger",
  SKIPPED: "neutral",
  INVALIDATED: "neutral",
};

export function executionStatusLabel(status: string | null | undefined): string {
  if (!status) return statusLabel(status);
  return executionStatusLabels[status] ?? statusLabel(status);
}

export function executionStatusTone(status: string | null | undefined): StatusTone {
  if (!status) return "neutral";
  return executionStatusTones[status] ?? statusTone(status);
}

export const executionStatusOptions = [
  "RUNNING",
  "WAITING_FIELD_REVIEW",
  "SUCCEEDED",
  "BLOCKED",
  "FAILED",
  "SKIPPED",
  "INVALIDATED",
].map((value) => ({ value, label: executionStatusLabel(value) }));

/** 列表里的执行概况：以进度与当前阻断为主。 */
export function executionProgressText(
  row: Pick<ExecutionRow, "status" | "invalidated" | "progress" | "current_blocked">,
): string {
  const { executed, blocked, not_executed } = row.progress;
  const parts = [`已执行 ${executed} 项`];
  if (row.invalidated) {
    if (blocked) parts.push(`历史规则阻断 ${blocked} 项`);
    if (not_executed) parts.push(`未执行 ${not_executed} 项`);
    return `原结局 · ${parts.join(" · ")}`;
  }
  if (row.current_blocked) parts.push(`当前执行阻断 ${row.current_blocked} 项`);
  if (not_executed) parts.push(`待执行 ${not_executed} 项`);
  if (row.status === "WAITING_FIELD_REVIEW") return `等待字段核对 · ${parts.join(" · ")}`;
  return parts.join(" · ");
}

export const MODEL_TURNS_HINT = "按 evaluation_id 去重：同一批量抽取的字段共享一次模型请求，修正轮也只计一次；这是模型调用轮次，不是 token 数或网络重试次数。";

export function modelTurnsText(turns: ExecutionModelTurns): string {
  return turns.available && turns.count !== null
    ? `模型请求轮次 ${turns.count}`
    : "模型请求轮次统计不可用";
}
