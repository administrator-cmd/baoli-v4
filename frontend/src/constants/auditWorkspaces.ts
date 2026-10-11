/** 规则执行入口；材料、字段、报告等内容由各自页签展示。 */
export type DetailTab = "materials" | "fields" | "rules" | "reviews" | "risks" | "report" | "retrieval" | "executions";
export interface DisplayNode {
  node_code: string;
  node_type?: string | null;
  name?: string;
  sequence_no: number;
  enabled: boolean;
}
export interface AuditWorkspace {
  key: string;
  label: string;
  nodeType: string;
  tab: DetailTab;
  nodeCode: string;
}
export function workspacesOfNodes(nodes: readonly DisplayNode[] | null | undefined): AuditWorkspace[] {
  return (nodes ?? []).filter(node => node.enabled && node.node_type === "RULE_EXECUTION")
    .map(node => ({ key: "RULE_EXECUTION", label: "规则执行", nodeType: "RULE_EXECUTION", tab: "rules", nodeCode: node.node_code }));
}
export const detailTabs: readonly DetailTab[] = ["materials", "fields", "rules", "reviews", "risks", "report", "retrieval", "executions"];
export function isDetailTab(value: unknown): value is DetailTab {
  return typeof value === "string" && (detailTabs as readonly string[]).includes(value);
}
