import type { ImportSheet } from "@/api/contracts";

/** 当前场景的实际节点，用于展示候选字段、规则和计算的归属。 */
export interface NodeOption {
  nodeCode: string;
  nodeType: string;
  label: string;
}

/** 工作表是否参与本次解析。 */
export interface SheetBindingDraft {
  excluded: boolean;
}

/** 页面显示和未提交改动比较共用的工作表选择初值。 */
export function initialBindingOf(
  sheet: ImportSheet,
  systemExcluded: boolean,
): SheetBindingDraft {
  if (systemExcluded) {
    return { excluded: true };
  }
  return {
    excluded: sheet.status === "EXCLUDED",
  };
}
