/**
 * 审核流程的业务工作区。
 *
 * 工作区是**现有后端节点的一种组织方式**，不是新增的流程对象：每个工作区直接对应
 * `manifest` 里真实的节点编码（`NODE_DEFINITIONS`），并指向任务详情的一个页签。
 * 页面不在这里发明节点，也不假设后端存在没有的接口。
 *
 * `ruleNodeFilter` 是规则结果页的筛选值。它只有在服务端返回规则所属节点时才可用；
 * 当前任务详情接口没有返回 `node`，页面据此把该筛选显式标为「服务端未返回」，
 * 而不是按材料数量之类的前端推断去猜一条规则属于哪个校验阶段。
 */

export type DetailTab =
  | "materials"
  | "fields"
  | "rules"
  | "reviews"
  | "risks"
  | "report"
  | "retrieval"
  | "executions";

export interface AuditWorkspace {
  /** 稳定标识。 */
  key: string;
  /** 业务工作区名称。 */
  label: string;
  /** 对应的后端节点编码；节点中文名一律取 nodeLabel()，不在此另写一份。 */
  nodeCodes: string[];
  /** 打开哪个页签查看这个工作区。 */
  tab: DetailTab;
  /** 规则结果页按节点筛选时使用的值；为 null 表示该工作区不按节点筛选规则。 */
  ruleNodeFilter: string | null;
}

export const auditWorkspaces: readonly AuditWorkspace[] = [
  {
    key: "MATERIAL_CONFIRM",
    label: "材料确认",
    nodeCodes: ["MATERIAL_CHECK"],
    tab: "materials",
    ruleNodeFilter: null,
  },
  {
    key: "MATERIAL_FORMAT",
    label: "材料格式问题",
    nodeCodes: ["DOCUMENT_PREPARE"],
    tab: "materials",
    ruleNodeFilter: null,
  },
  {
    key: "FIELD_EXTRACT",
    label: "字段抽取",
    nodeCodes: ["FIELD_EXTRACTION"],
    tab: "fields",
    ruleNodeFilter: null,
  },
  {
    key: "INTERNAL_CHECK",
    label: "文件内部校验",
    nodeCodes: ["INTERNAL_VALIDATE"],
    tab: "rules",
    ruleNodeFilter: "INTERNAL_VALIDATE",
  },
  {
    key: "CROSS_CHECK",
    label: "跨文件校验",
    nodeCodes: ["CROSS_VALIDATE"],
    tab: "rules",
    ruleNodeFilter: "CROSS_VALIDATE",
  },
  {
    key: "RISK_RATING",
    label: "风险审核",
    nodeCodes: ["RISK_RATING"],
    tab: "risks",
    ruleNodeFilter: null,
  },
  {
    key: "REPORT_OUTPUT",
    label: "报告输出",
    nodeCodes: ["REPORT_OUTPUT"],
    tab: "report",
    ruleNodeFilter: null,
  },
] as const;

/**
 * 人工复核不是上面七个工作区之一：它是横跨流程的等待点，由「人工复核」页签承载。
 * 当前节点是人工复核时没有对应工作区（workspaceOfNode 返回 null），
 * 页签由审计任务详情页按 waiting-review 状态单独决定。
 */
export const manualReviewNodeCode = "MANUAL_REVIEW";

/** 节点编码 → 工作区；没有对应工作区的节点（含人工复核）返回 null。 */
export function workspaceOfNode(nodeCode: string | null | undefined): AuditWorkspace | null {
  if (!nodeCode) {
    return null;
  }
  return auditWorkspaces.find((item) => item.nodeCodes.includes(nodeCode)) ?? null;
}

export const detailTabs: readonly DetailTab[] = [
  "materials",
  "fields",
  "rules",
  "reviews",
  "risks",
  "report",
  "retrieval",
  "executions",
] as const;

export function isDetailTab(value: unknown): value is DetailTab {
  return typeof value === "string" && (detailTabs as readonly string[]).includes(value);
}
