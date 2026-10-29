import { getCurrentScope, onScopeDispose, reactive, ref } from "vue";

import type { Paged } from "@/api/contracts";
import { DEFAULT_PAGE_SIZE } from "@/constants/pagination";

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
  const pageSize = ref(persisted?.pageSize ?? options.defaultPageSize ?? DEFAULT_PAGE_SIZE);
  const total = ref(0);
  const items = ref<T[]>([]);
  const loading = ref(false);
  const error = ref<string | null>(null);
  /** 最近一次成功响应的完整负载（含分页之外的附加字段，例如风险命中数与等级集合）。 */
  const lastResponse = ref<unknown>(null);
  let latestRequest = 0;
  /** 组件卸载后旧请求不得写回列表，也不得覆盖共享的筛选记忆。 */
  let disposed = false;
  if (getCurrentScope()) {
    onScopeDispose(() => {
      disposed = true;
    });
  }

  function persist(): void {
    if (disposed) return;
    listStateStore.set(options.key, { filters: { ...filters }, page: page.value, pageSize: pageSize.value });
  }

  async function load(allowPageFallback = true): Promise<void> {
    const request = ++latestRequest;
    loading.value = true;
    error.value = null;
    try {
      const payload = await options.fetch({ ...filters, page: page.value, page_size: pageSize.value });
      if (disposed || request !== latestRequest) {
        return;
      }
      items.value = payload.items;
      total.value = payload.total;
      page.value = payload.page;
      pageSize.value = payload.page_size;
      lastResponse.value = payload;
      // 删除当前页最后一条后，服务端仍按请求页码返回空页：按真实 total 收敛到最后一个
      // 有效页再读一次，否则用户会停在空页上，看不到前面还有数据。
      const lastPage = Math.max(1, Math.ceil(payload.total / payload.page_size));
      if (allowPageFallback && payload.items.length === 0 && payload.total > 0 && payload.page > lastPage) {
        page.value = lastPage;
        await load(false);
        return;
      }
    } catch (failure) {
      if (disposed || request !== latestRequest) {
        return;
      }
      items.value = [];
      total.value = 0;
      lastResponse.value = null;
      error.value = failure instanceof Error ? failure.message : "列表读取失败";
    } finally {
      if (!disposed && request === latestRequest) {
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
