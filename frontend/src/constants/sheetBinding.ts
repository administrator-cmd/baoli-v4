import type { ImportSheet } from "@/api/contracts";
import type { ProfileSheet } from "@/api/imports";

/**
 * Sheet 绑定下拉里的一项：节点**实例码** + 展示名。
 *
 * 实例码是用户配置的（`scenario_draft_node.node_code`），能力类型来自统一模板协议；
 * 两者不能混用——规则归属、Sheet 绑定与依赖都按实例码走。
 */
export interface NodeOption {
  nodeCode: string;
  nodeType: string;
  label: string;
}

/** 一张工作表在绑定步骤里的编辑值。 */
export interface SheetBindingDraft {
  role: string | null;
  /** 绑定的**节点实例码**（来自当前场景的节点配置）。一张 Sheet 只能绑定一个节点。 */
  nodeCode: string | null;
  headerRow: number | null;
  excluded: boolean;
}

/**
 * 表头行默认值：系统生成的 base Excel 把表头写在每张业务表的**第 1 行**
 * （`scene_template_service.build_scene_template` 建表后第一件事就是写表头）。
 * 因此这是构造上确定的，不是识别出来的——外部文件表头不在第 1 行时可以改。
 */
export const DEFAULT_HEADER_ROW = 1;

/** 某个用途允许绑定的节点**能力类型**：来自协议声明，页面不自行维护名单。 */
function allowedNodeTypes(profileSheets: ProfileSheet[], role: string | null | undefined): Set<string> {
  if (!role) {
    return new Set();
  }
  const sheet = profileSheets.find((item) => item.sheet_role === role);
  return new Set(sheet?.node_types ?? []);
}

/**
 * 该用途下**可选的实际节点实例**：按能力类型过滤，避免绑到不相干的节点上。
 *
 * 绑定表的下拉与「默认选哪个」必须是同一条规则——各写一份会让下拉里有的选项
 * 和默认值对不上。
 */
export function nodeOptionsForRole(
  profileSheets: ProfileSheet[],
  nodeOptions: NodeOption[],
  role: string | null | undefined,
): NodeOption[] {
  const allowed = allowedNodeTypes(profileSheets, role);
  return nodeOptions.filter((item) => allowed.has(item.nodeType));
}

/**
 * 该用途下的默认节点实例码：**只有一个可选实例时**返回它，其余一律返回 null。
 *
 * 0 个（场景还没配该能力类型的节点）与多个（同类节点有多个实例，谁归哪张表是业务决定）
 * 都不猜，留给用户在下拉里选。
 */
export function defaultNodeCodeForRole(
  profileSheets: ProfileSheet[],
  nodeOptions: NodeOption[],
  role: string | null | undefined,
): string | null {
  const options = nodeOptionsForRole(profileSheets, nodeOptions, role);
  return options.length === 1 ? options[0].nodeCode : null;
}

/**
 * 一张工作表的绑定初值。**只能有一份**：绑定表渲染用的值与作业的「是否有未提交改动」
 * 比较用的值必须是同一个口径，各算一套会让页面在没有任何操作时谎报「有未保存的改动」。
 *
 * 三个默认值的出处：用途取系统按表头识别出的结果（未识别时留空由人选）；
 * 表头行取 `DEFAULT_HEADER_ROW`；节点只在该用途下唯一可选时预选。
 * 系统已排除的工作表（说明页等）不参与绑定，三项都为 null。
 */
export function initialBindingOf(
  sheet: ImportSheet,
  profileSheets: ProfileSheet[],
  nodeOptions: NodeOption[],
  systemExcluded: boolean,
): SheetBindingDraft {
  if (systemExcluded) {
    return { role: null, nodeCode: null, headerRow: null, excluded: true };
  }
  const role = sheet.role ?? sheet.suggested_role ?? null;
  return {
    role,
    nodeCode: sheet.node_code ?? defaultNodeCodeForRole(profileSheets, nodeOptions, role),
    headerRow: sheet.header_row ?? DEFAULT_HEADER_ROW,
    excluded: sheet.status === "EXCLUDED",
  };
}
