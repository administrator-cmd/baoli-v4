import type { JsonValue } from "@/api/contracts";
import type { StatusTone } from "@/constants/labels";

/** 审核结果状态的稳定业务口径。 */
export type AuditResultStatus =
  | "PASSED"
  | "ABNORMAL"
  | "COMPUTED"
  | "NOT_APPLICABLE"
  | "EVIDENCE_INSUFFICIENT"
  | "NOT_EXECUTED"
  | "PENDING_REVIEW"
  | "REVIEWED"
  | "RUNNING"
  | "SKIPPED";

/** 审核结果状态的中文标签；COMPUTED 表示产出完成，不代指审核通过。 */
export const AUDIT_RESULT_LABELS: Record<AuditResultStatus, string> = {
  PASSED: "通过",
  ABNORMAL: "异常",
  COMPUTED: "已完成产出",
  NOT_APPLICABLE: "不适用",
  EVIDENCE_INSUFFICIENT: "证据不足",
  NOT_EXECUTED: "未执行",
  PENDING_REVIEW: "未确认",
  REVIEWED: "已复核",
  RUNNING: "执行中",
  SKIPPED: "已跳过",
};

export const AUDIT_RESULT_TONES: Record<AuditResultStatus, StatusTone> = {
  PASSED: "success",
  ABNORMAL: "danger",
  COMPUTED: "info",
  NOT_APPLICABLE: "neutral",
  EVIDENCE_INSUFFICIENT: "warning",
  NOT_EXECUTED: "neutral",
  PENDING_REVIEW: "warning",
  REVIEWED: "success",
  RUNNING: "info",
  SKIPPED: "neutral",
};

export const COMPLETED_RESULT_STATUSES: ReadonlySet<string> = new Set([
  "PASSED",
  "ABNORMAL",
  "COMPUTED",
  "NOT_APPLICABLE",
]);

/** 抽取规则形成产出的状态；沿用页面原有口径，避免把异常判定当作抽取完成。 */
export const EXTRACTION_RESULT_STATUSES: ReadonlySet<string> = new Set(["COMPUTED", "PASSED"]);

export const UNDECIDED_RESULT_STATUSES: ReadonlySet<string> = new Set([
  "EVIDENCE_INSUFFICIENT",
  "PENDING_REVIEW",
  "NOT_EXECUTED",
]);

/** 与后端状态目录同口径；服务端目录可用时仍以目录为准。 */
export const AUDIT_RESULT_CATEGORIES: Record<AuditResultStatus, string> = {
  COMPUTED: "COMPUTED",
  PASSED: "DECIDED",
  ABNORMAL: "DECIDED",
  REVIEWED: "DECIDED",
  EVIDENCE_INSUFFICIENT: "UNDECIDED",
  PENDING_REVIEW: "UNDECIDED",
  NOT_EXECUTED: "UNDECIDED",
  RUNNING: "UNDECIDED",
  SKIPPED: "UNDECIDED",
  NOT_APPLICABLE: "NOT_APPLICABLE",
};

/** 新结果显示业务短句；旧结果只投影已保存的状态与取值。 */
export function auditResultSummary(status: string, message: string, result: Record<string, JsonValue> | null): string {
  if (result?.presentation_version === 2 && message) return message;
  const extracted = result?.extracted_values;
  if (Array.isArray(extracted) && extracted.length) {
    const fields = extracted.filter(
      (item): item is Record<string, JsonValue> => Boolean(item) && typeof item === "object" && !Array.isArray(item),
    );
    const statements = fields.map((field) => {
      const name = typeof field.field_name === "string" ? field.field_name : "字段";
      if (field.status === "NOT_PRESENT") return `未找到${name}`;
      if (field.normalized_value !== null && field.normalized_value !== undefined && field.normalized_value !== "") {
        return `已抽取${name}`;
      }
      return `未取得${name}的有效值`;
    });
    if (statements.length) return statements.join("；");
  }
  const summaries: Record<string, string> = {
    PASSED: "符合要求",
    ABNORMAL: "不符合要求",
    COMPUTED: "已完成计算或生成",
    NOT_APPLICABLE: "本次不适用",
    EVIDENCE_INSUFFICIENT: "依据不足，无法判断",
    NOT_EXECUTED: "未完成执行",
    PENDING_REVIEW: "尚未确认",
    REVIEWED: "已人工复核",
  };
  return summaries[status] ?? "尚无明确结论";
}
