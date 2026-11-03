import type { AuditFieldView } from "@/api/audits";
import { formatBusinessValue } from "@/constants/labels";

/** 只格式化声明为数值的字段；按字符串分组，保留全部小数位且不改变计算精度。 */
export function formatFieldValue(field: AuditFieldView): string {
  return formatTypedValue(field.normalized_value, field.value_type);
}

const PERCENT_TYPES = new Set(["PERCENT", "PERCENTAGE", "RATE", "PERCENT_RATE"]);
const MULTIPLE_TYPES = new Set(["MULTIPLE", "TIMES", "MULTIPLIER"]);
const DECIMAL_TYPES = new Set(["AMOUNT", "DECIMAL", "DECIMAL_CN", "NUMBER", "INTEGER", "FLOAT"]);

/**
 * 按声明的取值类型格式化；百分比未声明数值尺度，保留服务端值，
 * 不按数值大小猜测或换算，也不改变底层计算值。
 */
export function formatTypedValue(value: unknown, valueType: string | null | undefined): string {
  if (value === null || value === undefined || value === "") {
    return formatBusinessValue(value);
  }
  if (typeof value !== "number" && typeof value !== "string") {
    return formatBusinessValue(value);
  }
  const type = (valueType ?? "").toUpperCase();
  if (PERCENT_TYPES.has(type)) {
    const text = String(value).trim();
    if (!text) return formatBusinessValue(value);
    if (text.endsWith("%")) return text;
    return text;
  }
  if (MULTIPLE_TYPES.has(type)) {
    const text = String(value).trim();
    if (!text) return formatBusinessValue(value);
    return /^[+-]?\d+(?:\.\d+)?$/.test(text.replace(/,/g, "")) ? `${text}倍` : formatBusinessValue(value);
  }
  if (!DECIMAL_TYPES.has(type)) {
    return formatBusinessValue(value);
  }
  const text = typeof value === "number" || typeof value === "string" ? String(value) : "";
  const decimal = /^([+-]?)(0|[1-9]\d*)(\.\d+)?$/.exec(text);
  if (!decimal) return formatBusinessValue(value);
  const [, sign, integer, fraction = ""] = decimal;
  return `${sign}${integer!.replace(/\B(?=(\d{3})+(?!\d))/g, ",")}${fraction}`;
}

/** 有效值包含 0 和 false；仅有候选值或原文不代表已经形成有效事实。 */
export function hasEffectiveFieldValue(field: AuditFieldView): boolean {
  return ["PRESENT", "REVIEWED"].includes(field.status)
    && field.normalized_value !== null && field.normalized_value !== undefined && field.normalized_value !== "";
}

export function fieldSourceFiles(field: AuditFieldView): string[] {
  return [...new Set([
    ...(field.source_files ?? []),
    ...field.evidence.map((item) => item.document_name),
  ].filter((name): name is string => Boolean(name)))];
}

/**
 * 字段结果的展示形态。
 *
 * 由服务端状态和实际取值共同判定，不根据 UI 文案反推：
 * - VALUE：有可展示的取值
 * - NOT_FOUND：材料中确认没有该字段
 * - UNCERTAIN：读到原文但不足以形成取值
 * - FAILED：抽取执行失败
 * - UNNORMALIZED：有原文但标准化失败
 * - NOT_RUN：本次没有执行抽取
 */
export type FieldMode = "VALUE" | "NOT_FOUND" | "UNCERTAIN" | "FAILED" | "UNNORMALIZED" | "NOT_RUN" | "NOT_APPLICABLE";

function hasRawValue(field: AuditFieldView): boolean {
  return field.raw_value !== null && field.raw_value !== undefined && field.raw_value !== "";
}

export function fieldMode(field: AuditFieldView): FieldMode {
  if (field.status === "NOT_APPLICABLE") return "NOT_APPLICABLE";
  if (field.status === "NOT_EXECUTED" || field.status === "PENDING") return "NOT_RUN";
  if (field.status === "EXTRACTION_FAILED") return "FAILED";
  if (field.status === "NORMALIZATION_FAILED") return "UNNORMALIZED";
  if (field.status === "EVIDENCE_INSUFFICIENT" || field.status === "EVIDENCE_UNCLEAR") return "UNCERTAIN";
  if (field.status === "NOT_PRESENT") return "NOT_FOUND";
  if (field.status === "REVIEWED") return hasEffectiveFieldValue(field) ? "VALUE" : "NOT_FOUND";
  if (field.status === "PRESENT") return "VALUE";
  return hasRawValue(field) ? "VALUE" : "NOT_RUN";
}

/** 未形成取值时，直接展示给用户的原因。优先使用服务端保存的抽取说明。 */
export function fieldReason(field: AuditFieldView): string {
  const mode = fieldMode(field);
  if (mode === "UNNORMALIZED") return "已取得原文，但无法按字段类型标准化";
  if (field.extraction_message) return field.extraction_message;
  switch (mode) {
    case "NOT_APPLICABLE":
      return "本字段本次不适用";
    case "NOT_FOUND":
      return field.status === "REVIEWED" ? "人工已确认该字段在材料中不存在" : "材料中未找到该字段";
    case "UNCERTAIN":
      return "读取到的原文不足以确认该字段";
    case "FAILED":
      return "抽取执行失败，未取得取值";
    case "NOT_RUN":
      if (field.extraction_enabled === false) return "本方案未启用该字段的自动抽取";
      if (field.material_uploaded === false) return "材料未上传，尚未执行抽取";
      if (field.status === "PENDING") return "等待字段抽取";
      return "尚未取得该字段的抽取结果";
    default:
      return "未记录说明";
  }
}

/** 原文区块的标题，按它在本次结果中的作用命名。 */
export function evidenceTitle(mode: FieldMode): string {
  switch (mode) {
    case "NOT_APPLICABLE":
      return "已有原文";
    case "VALUE":
    case "UNNORMALIZED":
      return "引用原文";
    case "NOT_FOUND":
      return "检查到的原文";
    case "UNCERTAIN":
    case "FAILED":
      return "已读取的原文";
    case "NOT_RUN":
      return "已有原文";
  }
}

/** 原文区块的说明，避免把检查片段误读成字段取值。 */
export function evidenceNote(mode: FieldMode): string {
  switch (mode) {
    case "NOT_APPLICABLE":
      return "以下为已保存的原文片段，本字段本次不适用。";
    case "VALUE":
    case "UNNORMALIZED":
      return "";
    case "NOT_FOUND":
      return "以下为系统在材料中检查到的原文片段，不代表该字段的取值。";
    case "UNCERTAIN":
      return "以下为系统读取到的原文片段，但不足以形成可靠取值。";
    case "FAILED":
      return "以下为抽取失败前已读取到的原文片段。";
    case "NOT_RUN":
      return "以下为任务中已有的原文片段，本次未参与抽取。";
  }
}

/** 原文是否可以作为字段取值的依据。 */
export function evidenceIsValue(mode: FieldMode): boolean {
  return mode === "VALUE" || mode === "UNNORMALIZED";
}

/** 未形成取值时的原因区块标题。 */
export function reasonTitle(mode: FieldMode): string {
  switch (mode) {
    case "NOT_APPLICABLE":
      return "不适用说明";
    case "NOT_FOUND":
      return "检查结论";
    case "UNCERTAIN":
      return "未能形成取值";
    case "FAILED":
      return "抽取失败";
    case "UNNORMALIZED":
      return "标准化失败";
    case "NOT_RUN":
      return "未执行原因";
    default:
      return "结果说明";
  }
}
