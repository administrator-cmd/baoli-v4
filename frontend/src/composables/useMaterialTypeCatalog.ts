import { reactive } from "vue";

import type { MaterialTypeItem } from "@/api/contracts";
import { ApiError } from "@/api/http";
import { getMaterialTypes } from "@/api/system";

export function useMaterialTypeCatalog() {
  const state = reactive({
    items: [] as MaterialTypeItem[],
    error: null as string | null,
    loading: false,
    available: false,
  });

  async function reload(): Promise<void> {
    if (state.loading) return;
    state.loading = true;
    try {
      state.items = await getMaterialTypes();
      state.error = null;
      state.available = true;
    } catch (error) {
      state.items = [];
      state.available = false;
      state.error = error instanceof ApiError
        ? `材料类型目录读取失败：${error.message}`
        : "材料类型目录读取失败";
    } finally {
      state.loading = false;
    }
  }

  return { state, reload };
}

export type MaterialTypeCatalog = ReturnType<typeof useMaterialTypeCatalog>;
