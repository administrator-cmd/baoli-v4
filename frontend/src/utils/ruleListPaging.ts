/** 场景规则列表的翻页位置：打开规则编辑页后返回时恢复到同一页。 */
const pagingByScenario = new Map<string, { page: number; pageSize: number }>();

export function readRuleListPaging(scenarioCode: string | null): { page: number; pageSize: number } | undefined {
  return scenarioCode ? pagingByScenario.get(scenarioCode) : undefined;
}

export function rememberRuleListPaging(scenarioCode: string | null, page: number, pageSize: number): void {
  if (!scenarioCode) {
    return;
  }
  pagingByScenario.set(scenarioCode, { page, pageSize });
}
