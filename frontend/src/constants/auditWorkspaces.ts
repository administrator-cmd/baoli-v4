/** 规则执行入口；材料、字段、报告等内容由各自页签展示。 */
export type DetailTab = "materials" | "fields" | "rules" | "risks" | "report" | "executions";
export const detailTabs: readonly DetailTab[] = ["materials", "fields", "rules", "risks", "report", "executions"];
export function isDetailTab(value: unknown): value is DetailTab {
  return typeof value === "string" && (detailTabs as readonly string[]).includes(value);
}
