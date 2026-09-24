import { reactive, ref } from "vue";

import type { Paged } from "@/api/contracts";

/**
 * 统一的后端分页列表状态。
 *
 * - 筛选、页码与每页条数按列表页 key 记忆：离开再返回时恢复上次的查询条件与页码；
 * - 读取失败时清空当前项并显式报错（页面显示错误与重试），绝不显示成“暂无数据”；
 * - total 一律用服务端返回值，不用当前页数组长度。
 */
const listStateStore = new Map<string, { filters: Record<string, unknown>; page: number; pageSize: number }>();

export interface PagedListOptions<T, F extends Record<string, unknown>> {
  /** 列表页状态键（通常是路由名）。 */
  key: string;
  initialFilters: F;
  defaultPageSize?: number;
  fetch: (params: F & { page: number; page_size: number }) => Promise<Paged<T>>;
}

export function usePagedList<T, F extends Record<string, unknown>>(options: PagedListOptions<T, F>) {
  const persisted = listStateStore.get(options.key);
  const filters = reactive({ ...options.initialFilters, ...(persisted?.filters as Partial<F> ?? {}) }) as F;
  const page = ref(persisted?.page ?? 1);
  const pageSize = ref(persisted?.pageSize ?? options.defaultPageSize ?? 20);
  const total = ref(0);
  const items = ref<T[]>([]);
  const loading = ref(false);
  const error = ref<string | null>(null);
  /** 最近一次成功响应的完整负载（含分页之外的附加字段，例如风险命中数与等级集合）。 */
  const lastResponse = ref<unknown>(null);
  let latestRequest = 0;

  function persist(): void {
    listStateStore.set(options.key, { filters: { ...filters }, page: page.value, pageSize: pageSize.value });
  }

  async function load(): Promise<void> {
    const request = ++latestRequest;
    loading.value = true;
    error.value = null;
    try {
      const payload = await options.fetch({ ...filters, page: page.value, page_size: pageSize.value });
      if (request !== latestRequest) {
        return;
      }
      items.value = payload.items;
      total.value = payload.total;
      page.value = payload.page;
      pageSize.value = payload.page_size;
      lastResponse.value = payload;
    } catch (failure) {
      if (request !== latestRequest) {
        return;
      }
      items.value = [];
      total.value = 0;
      lastResponse.value = null;
      error.value = failure instanceof Error ? failure.message : "列表读取失败";
    } finally {
      if (request === latestRequest) {
        loading.value = false;
        persist();
      }
    }
  }

  function search(): void {
    page.value = 1;
    void load();
  }

  function reset(): void {
    Object.assign(filters, options.initialFilters);
    page.value = 1;
    void load();
  }

  function setPage(value: number): void {
    page.value = value;
    void load();
  }

  function setPageSize(value: number): void {
    pageSize.value = value;
    page.value = 1;
    void load();
  }

  return { filters, page, pageSize, total, items, loading, error, lastResponse, load, search, reset, setPage, setPageSize };
}

/** 清空某个列表页键下记忆的筛选与页码；再次进入该列表时按初始筛选重新开始。 */
export function clearListState(key: string): void {
  listStateStore.delete(key);
}
