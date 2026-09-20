/**
 * 统一的分页/筛选查询串构造。
 *
 * 后端把 `*_from` / `*_to` 声明为 `datetime`，并在服务层用闭区间比较
 * （例如 `created_at <= created_to`）。日期选择器提交的是 `YYYY-MM-DD`，
 * 解析后是当天 00:00:00，于是「结束日期当天」的记录会被整体排除：
 *
 *   created_to=2026-09-11  →  2026-09-11T00:00:00  →  当天 14:30 的记录不满足 <=
 *
 * 这里把只含日期的 `*_to` 参数补成当天最后一刻，与后端的闭区间语义一致；
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
