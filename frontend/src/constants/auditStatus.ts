import type { JsonValue } from "@/api/contracts";

/** 审核结果状态的稳定业务口径。中文文案与颜色由后端状态词表按语境提供。 */
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

export const COMPLETED_RESULT_STATUSES: ReadonlySet<string> = new Set([
  "PASSED",
  "ABNORMAL",
  "COMPUTED",
  "NOT_APPLICABLE",
]);

export const UNDECIDED_RESULT_STATUSES: ReadonlySet<string> = new Set([
  "EVIDENCE_INSUFFICIENT",
  "PENDING_REVIEW",
  "NOT_EXECUTED",
]);

/** 后端状态词表不可用时的兜底分档；词表可用时以词表为准。 */
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
    COMPUTED: "已产出中间值，本条没有合格或异常结论",
    NOT_APPLICABLE: "本次不适用",
    EVIDENCE_INSUFFICIENT: "依据不足，尚未形成结论",
    NOT_EXECUTED: "未执行",
    PENDING_REVIEW: "业务结论未确认",
    REVIEWED: "已人工复核",
  };
  return summaries[status] ?? "尚无明确结论";
}
