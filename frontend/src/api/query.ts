/**
 * 统一的分页/筛选查询串构造。
 *
 * 后端把 `*_from` / `*_to` 声明为 `datetime`，数据库时间按 UTC 存储，而日期选择器
 * 给出的是用户本地日期。这里把只含日期的区间两端换算成本地当天 00:00 / 23:59:59.999
 * 对应的 UTC 时刻，避免按本地时间比较造成整体偏移；只含日期的 `*_to` 也不会
 * 解析成当天 00:00:00 而漏掉结束日的记录。已经带时间的值原样保留，
 * 调用方仍可以表达精确时刻。
 */
const DATE_ONLY = /^\d{4}-\d{2}-\d{2}$/;

/** 本地日期 → UTC 时刻；日期非法时返回 null，由调用方原样提交。 */
function localDayBoundUtc(text: string, endOfDay: boolean): string | null {
  const [year, month, day] = text.split("-").map(Number);
  if (!year || !month || !day) return null;
  const date = new Date(
    year, month - 1, day,
    endOfDay ? 23 : 0, endOfDay ? 59 : 0, endOfDay ? 59 : 0, endOfDay ? 999 : 0,
  );
  return Number.isNaN(date.getTime()) ? null : date.toISOString();
}

export function toQuery(params: Record<string, string | number | boolean | undefined>): string {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value === undefined || value === "") {
      continue;
    }
    let text = String(value);
    if ((key.endsWith("_from") || key.endsWith("_to")) && DATE_ONLY.test(text)) {
      text = localDayBoundUtc(text, key.endsWith("_to")) ?? text;
    }
    search.set(key, text);
  }
  const query = search.toString();
  return query ? `?${query}` : "";
}
