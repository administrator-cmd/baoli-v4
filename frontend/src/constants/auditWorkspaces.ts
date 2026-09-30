/**
 * 审核流程的业务工作区。
 *
 * 工作区是**后端节点的一种组织方式**，不是新增的流程对象。每个工作区按节点的
 * **能力类型**（`node_type`）匹配，并指向任务详情的一个页签。
 *
 * 关键：工作区**由当前任务真实配置的节点决定**，不再是一份固定清单。
 * 任务详情接口返回 `display.nodes`（本场景实际配置的节点实例）与
 * `display.sheets`（本场景选用的 Sheet），页面据此只展示当前场景实际使用的内容——
 * 统一模板协议里未被本场景选中的节点与 Sheet 不出现。
 *
 * `ruleNodeFilter` 是规则结果页的阶段筛选值。它读规则结果里的节点信息；
 * 服务端未返回时页面把该筛选显式标为「服务端未返回」，不按材料数量之类的前端推断
 * 去猜一条规则属于哪个校验阶段。
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

/**
 * 能力类型 → 工作区定义。
 *
 * 这是**展示方式的通用表**：同一能力类型在任何场景下都用同一种方式展示。
 * 哪些工作区真的出现，由当前任务配置的节点决定，因此这不是固定阶段清单。
 */
export interface AuditWorkspaceDefinition {
  /** 稳定标识。 */
  key: string;
  /** 业务工作区名称。 */
  label: string;
  /** 匹配的节点**能力类型**（不是实例码）。 */
  nodeType: string;
  /** 打开哪个页签查看这个工作区。 */
  tab: DetailTab;
  /** 规则结果页按阶段筛选时使用的值；为 null 表示该工作区不按阶段筛选规则。 */
  ruleNodeFilter: string | null;
}

const workspaceDefinitions: readonly AuditWorkspaceDefinition[] = [
  { key: "MATERIAL_CONFIRM", label: "材料确认", nodeType: "MATERIAL_CHECK", tab: "materials", ruleNodeFilter: null },
  { key: "MATERIAL_FORMAT", label: "材料格式问题", nodeType: "DOCUMENT_PREPARE", tab: "materials", ruleNodeFilter: null },
  { key: "FIELD_EXTRACT", label: "字段抽取", nodeType: "FIELD_EXTRACTION", tab: "fields", ruleNodeFilter: null },
  { key: "INTERNAL_CHECK", label: "文件内部校验", nodeType: "INTERNAL_VALIDATE", tab: "rules", ruleNodeFilter: "INTERNAL_VALIDATE" },
  { key: "CROSS_CHECK", label: "跨文件校验", nodeType: "CROSS_VALIDATE", tab: "rules", ruleNodeFilter: "CROSS_VALIDATE" },
  { key: "CALCULATION", label: "计算", nodeType: "CALCULATION", tab: "rules", ruleNodeFilter: null },
  { key: "RISK_RATING", label: "风险审核", nodeType: "RISK_RATING", tab: "risks", ruleNodeFilter: null },
  { key: "REPORT_OUTPUT", label: "报告输出", nodeType: "REPORT_OUTPUT", tab: "report", ruleNodeFilter: null },
];

/**
 * 工作区推导所需的最小节点信息。
 *
 * 只要求这四个字段：任务详情的 `nodes` 与展示结构的 `display.nodes` 都能满足它，
 * 因此两条来源共用同一份推导逻辑。`node_type` 在旧任务上可能缺失——
 * 那时实例码本身就是能力类型，推导按同一口径回退。
 */
export interface DisplayNode {
  node_code: string;
  node_type?: string | null;
  name?: string;
  sequence_no: number;
  enabled: boolean;
}

/** 当前任务的一个工作区：通用定义 + 本任务真实配置的那个节点实例。 */
export interface AuditWorkspace extends AuditWorkspaceDefinition {
  /** 本任务里这个工作区对应的**节点实例码**。 */
  nodeCode: string;
}

/**
 * 按当前任务真实配置的节点算出工作区清单。
 *
 * 只返回当前场景**实际配置**的节点对应的那个工作区：未被本场景选中的能力类型
 * 不出现。停用的节点不进入执行范围。
 * 同一能力类型有多个实例时每个实例各占一个工作区，页面据此分开展示，
 * 不再把两条检查合进一张卡片。
 */
export function workspacesOfNodes(nodes: readonly DisplayNode[] | null | undefined): AuditWorkspace[] {
  if (!nodes?.length) {
    return [];
  }
  const byType = new Map<string, DisplayNode[]>();
  for (const node of nodes) {
    if (!node.enabled) {
      continue;
    }
    // 旧任务没有 node_type：那时实例码即能力类型，按同一口径回退。
    const nodeType = node.node_type || node.node_code;
    const list = byType.get(nodeType) ?? [];
    list.push(node);
    byType.set(nodeType, list);
  }
  const result: AuditWorkspace[] = [];
  for (const definition of workspaceDefinitions) {
    for (const node of (byType.get(definition.nodeType) ?? []).sort((a, b) => a.sequence_no - b.sequence_no)) {
      result.push({ ...definition, nodeCode: node.node_code });
    }
  }
  return result;
}

/**
 * 人工复核不是上面这些工作区之一：它是横跨流程的等待点，由「人工复核」页签承载。
 * 页签由审计任务详情页按 waiting-review 状态单独决定。
 */
export const manualReviewNodeType = "MANUAL_REVIEW";

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

/** URL 里的 tab 是否为合法的详情页签；非法值不写入页签状态。 */
export function isDetailTab(value: unknown): value is DetailTab {
  return typeof value === "string" && (detailTabs as readonly string[]).includes(value);
}
