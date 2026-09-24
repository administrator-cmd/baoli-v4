/**
 * 统一的分页/筛选查询串构造。
 *
 * 后端把 `*_from` / `*_to` 声明为 `datetime`，并在服务层用闭区间比较
 * （例如 `created_at <= created_to`）。只含日期的 `*_to` 原样提交会解析成当天
 * 00:00:00，结束日当天的记录整体漏掉，因此这里把它补成当天最后一刻；
 * 已经带时间的值原样保留，调用方仍可以表达精确时刻。
 */
const DATE_ONLY = /^\d{4}-\d{2}-\d{2}$/;
const END_OF_DAY = "T23:59:59";

export function toQuery(params: Record<string, string | number | boolean | undefined>): string {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value === undefined || value === "") {
      continue;
    }
    const text = String(value);
    search.set(key, key.endsWith("_to") && DATE_ONLY.test(text) ? `${text}${END_OF_DAY}` : text);
  }
  const query = search.toString();
  return query ? `?${query}` : "";
}
