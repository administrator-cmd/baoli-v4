/** 业务规则库的展示、条目定位与编辑副本。内部身份不展示给用户。 */
import { toRaw } from "vue";
import type {
  GenerationDocument, GenerationEntry, GenerationSheet, GenerationSource, GenerationStatus,
} from "@/api/contracts";
import { newId } from "@/utils/ids";
import type { OperationView } from "@/api/http";

export function generationAssistantFailureMessage(
  operation: Pick<OperationView, "operation_type" | "failure_code" | "failure_message"> | null,
): string | undefined {
  if (operation?.operation_type === "RULEBOOK_GENERATE" && ["LLM_CONTENT_REVIEW_FAILED", "LLM_CONTRACT_INVALID", "LLM_SOURCE_REFERENCE_INVALID", "LLM_REVIEW_INVALID"].includes(operation.failure_code ?? "")) {
    return "AI 未能整理出可核对的规则库。原文件已保留，可重新编制。";
  }
  if (operation?.operation_type !== "RULEBOOK_REVISE") return undefined;
  if (["ASSISTANT_REVIEW_FAILED", "ASSISTANT_REVIEW_INVALID"].includes(operation.failure_code ?? "")) {
    return "AI 未能完成本次修改的内容核对，原版本已保留。可在助手中重试这条要求。";
  }
  if (["ASSISTANT_INVALID_EDIT", "ASSISTANT_INVALID_REPAIR", "ASSISTANT_ANSWER_NOT_APPLIED"].includes(operation.failure_code ?? "")) {
    return "AI 返回的修改不完整，原版本已保留。可在助手中重试这条要求。";
  }
  return undefined;
}

export const generationStatusLabels: Record<GenerationStatus, string> = {
  NOT_GENERATED: "尚未编制", PENDING: "等待 AI 处理", RUNNING: "AI 处理中", EDITABLE: "已编制",
  NO_DEFINITIONS: "未识别到业务内容", FAILED: "编制失败", INTERRUPTED: "编制中断", UNKNOWN: "状态未知",
};
export const generationStatusOptions = Object.entries(generationStatusLabels).map(
  ([value, label]) => ({ value: value as GenerationStatus, label }),
);
export function generationStatusLabel(status: string | null | undefined): string {
  return status && status in generationStatusLabels
    ? generationStatusLabels[status as GenerationStatus] : status || "状态未知";
}
export function generationStatusTone(status: string | null | undefined): "neutral" | "info" | "success" | "warning" | "danger" {
  if (status === "FAILED") return "danger";
  if (status === "NO_DEFINITIONS" || status === "INTERRUPTED") return "warning";
  if (status === "PENDING" || status === "RUNNING" || status === "EDITABLE") return "info";
  return "neutral";
}
export function generationSourceText(source: GenerationSource): string {
  const parts = [];
  if (source.origin) {
    parts.push(`《${source.origin.sheet_name}》第 ${source.origin.row_no} 行`);
    parts.push(...source.origin.related_rows.map((row) => `《${row.sheet_name}》第 ${row.row_no} 行`));
  }
  if (source.kind === "USER_INPUT") parts.push("用户补充 / 编辑");
  if (source.kind === "DERIVED") parts.push("依据原文整理");
  if (source.note) parts.push(source.note);
  return parts.join("；") || "原文资料";
}
export function newGenerationRef(prefix: string): string {
  return `${prefix}_${newId()}`;
}
export function newUserSource(note = "用户新增"): GenerationSource {
  return { kind: "USER_INPUT", origin: null, requirement_ids: [], note };
}
export function cloneValue<T>(value: T): T {
  // 文档只包含 JSON 数据；逐层展开响应式对象，编辑弹窗不能直接克隆 Vue Proxy。
  if (value === null || typeof value !== "object") return value;
  const raw = toRaw(value);
  if (Array.isArray(raw)) return raw.map((item) => cloneValue(item)) as T;
  return Object.fromEntries(
    Object.entries(raw).map(([key, item]) => [key, cloneValue(item)]),
  ) as T;
}

export function generationCounts(document: GenerationDocument | null) {
  return {
    sheets: document?.sheets.length ?? 0,
    entries: document?.sheets.reduce((count, sheet) => count + sheet.rows.length, 0) ?? 0,
    openIssues: document?.issues.filter((issue) => issue.status === "OPEN").length ?? 0,
  };
}
export function businessCodeColumn(sheet: GenerationSheet) {
  const prefixes = ["规则", "材料", "条目", "映射", "字典", "风险", "报告", "字段", "原始", "原", "材料规则", "质量规则", "抽取规则", "计算规则", "核验规则", "校验规则", "风险规则"];
  const names = new Set(["编号", "原编号", "原始编号", "业务编号", "编制编号", "id", ...prefixes.flatMap(prefix => [prefix + "编号", prefix + "id"])]);
  const candidates = sheet.columns.filter(column => column.code_role == null || column.code_role === "IDENTITY");
  const marked = candidates.find(column => column.code_role === "IDENTITY" || column.is_business_code);
  if (marked) return marked;
  const inferred = candidates.filter(column => names.has(column.name.replace(/\s+/g, "").toLowerCase()));
  return inferred.find(column => column.name.replace(/\s+/g, "") !== "编制编号") ?? inferred[0];
}
export function entryBusinessCode(sheet: GenerationSheet, row: GenerationEntry): string {
  const column = businessCodeColumn(sheet);
  return (column ? row.cells[column.column_id]?.trim() : "") || row.business_code?.trim() || "";
}
export function entryCellValue(sheet: GenerationSheet, row: GenerationEntry, columnId: string): string {
  return row.cells[columnId] || (businessCodeColumn(sheet)?.column_id === columnId ? entryBusinessCode(sheet, row) : "");
}
export function entryLabel(sheet: GenerationSheet, row: GenerationEntry): string {
  const values = sheet.columns.map(column => entryCellValue(sheet, row, column.column_id)).filter(value => value.trim());
  return (entryBusinessCode(sheet, row) ? values.slice(0, 2).join(" · ") : values.join(" · ").slice(0, 160)) || "空白条目";
}
export function entryReferenceLabel(sheet: GenerationSheet, row: GenerationEntry): string {
  const code = entryBusinessCode(sheet, row);
  if (code) return sheet.business_code_scope === "SHEET" ? `${sheet.name} · ${code}` : code;
  return `${sheet.name} · ${entryLabel(sheet, row)}`;
}
export function findEntry(document: GenerationDocument, reference: string) {
  for (const sheet of document.sheets) {
    const row = sheet.rows.find((entry) => entry.row_id === reference);
    if (row) return { sheet, row };
  }
  return null;
}
export function updateEntry(document: GenerationDocument, sheetId: string, row: GenerationEntry): GenerationDocument {
  return {
    ...document,
    sheets: document.sheets.map((sheet) => sheet.sheet_id === sheetId
      ? { ...sheet, rows: sheet.rows.some((item) => item.row_id === row.row_id)
        ? sheet.rows.map((item) => item.row_id === row.row_id ? row : item) : [...sheet.rows, row] }
      : sheet),
  };
}
