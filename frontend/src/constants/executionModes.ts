/** 与业务无关的规则执行方式；业务内容仍在 requirement/parameters 中。 */
export type ExecutionMode =
  | "MATERIAL_CHECK"
  | "VALUE_COMPARE"
  | "CROSS_SCOPE_COMPARE"
  | "AGGREGATE"
  | "DERIVE_RISK"
  | "EXTRACT"
  | "CALCULATE"
  | "REPORT"
  | "UNKNOWN";

export const EXECUTION_MODE_OPTIONS: Array<{ value: ExecutionMode; label: string }> = [
  { value: "MATERIAL_CHECK", label: "材料检查" },
  { value: "VALUE_COMPARE", label: "字段比较" },
  { value: "CROSS_SCOPE_COMPARE", label: "跨范围比较" },
  { value: "AGGREGATE", label: "组合汇总" },
  { value: "DERIVE_RISK", label: "风险推导" },
  { value: "EXTRACT", label: "字段抽取" },
  { value: "CALCULATE", label: "计算" },
  { value: "REPORT", label: "报告生成" },
  { value: "UNKNOWN", label: "未判定" },
];

const LABELS = new Map(EXECUTION_MODE_OPTIONS.map((item) => [item.value, item.label]));

export function executionModeLabel(value: string | null | undefined): string {
  return LABELS.get((value ?? "UNKNOWN") as ExecutionMode) ?? String(value);
}
