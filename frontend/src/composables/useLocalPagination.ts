import { computed, ref, watch } from "vue";

import { DEFAULT_PAGE_SIZE } from "@/constants/pagination";

/** 对已筛选、排序的本地列表分页。 */
export function useLocalPagination<T>(items: () => readonly T[]) {
  const page = ref(1);
  const pageSize = ref(DEFAULT_PAGE_SIZE);
  const total = computed(() => items().length);
  const pageItems = computed(() => {
    const start = (page.value - 1) * pageSize.value;
    return items().slice(start, start + pageSize.value);
  });

  watch(pageSize, () => {
    page.value = 1;
  });

  watch(total, (count) => {
    // 删除当前页最后一条后，回到最后一个有效页。
    const lastPage = Math.max(1, Math.ceil(count / pageSize.value));
    if (page.value > lastPage) page.value = lastPage;
  });

  return { page, pageSize, pageItems };
}
