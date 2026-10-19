import { computed, ref } from "vue";

import type { ConfigOrigin } from "@/api/contracts";

interface SourcedRule {
  origin?: ConfigOrigin | null;
}

const SHEET_PREFIX = "sheet:";
const MISSING_SOURCE = "missing";
const MISSING_SOURCE_LABEL = "未记录来源";

function sourceKey(origin: ConfigOrigin | null | undefined): string {
  return origin?.sheet_name ? `${SHEET_PREFIX}${origin.sheet_name}` : MISSING_SOURCE;
}

export function sourceSheetLabel(origin: ConfigOrigin | null | undefined): string {
  return origin?.sheet_name || MISSING_SOURCE_LABEL;
}

/** 两类规则都按主来源工作表筛选；辅助引用不改变规则归属。 */
export function useRuleSourceFilter(items: () => readonly SourcedRule[]) {
  const selectedSheet = ref("");
  const sourceSheetOptions = computed(() => {
    const counts = new Map<string, number>();
    for (const item of items()) {
      const key = sourceKey(item.origin);
      counts.set(key, (counts.get(key) ?? 0) + 1);
    }
    // 删除某表的最后一条规则后，仍明确显示当前选择及其零条结果。
    if (selectedSheet.value && !counts.has(selectedSheet.value)) {
      counts.set(selectedSheet.value, 0);
    }
    return Array.from(counts, ([value, count]) => ({
      value,
      label: `${value === MISSING_SOURCE ? MISSING_SOURCE_LABEL : value.slice(SHEET_PREFIX.length)}（${count}）`,
    }));
  });

  function matchesSourceSheet(item: SourcedRule): boolean {
    return !selectedSheet.value || sourceKey(item.origin) === selectedSheet.value;
  }

  return { selectedSheet, sourceSheetOptions, matchesSourceSheet };
}
