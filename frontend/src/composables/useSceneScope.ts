import { ref, watch } from "vue";
import { useRoute, useRouter } from "vue-router";

import { listSceneOptions, type ScenarioOption } from "@/api/scenes";

/**
 * 场景配置页共用的“场景”选择。
 *
 * 场景配置只有一份（编辑即生效），因此这里没有草稿 / 已发布版本的作用域：
 * URL 只带 ?scenario=CODE，不再有 ?scope=。
 *
 * URL 没有指定场景时默认落在第一个可用场景上：这三个页面是配置入口，
 * 打开就该看到真实配置，而不是一个要求用户先选场景的空壳。
 * 没有可选场景（列表为空）时保持未选中，由面板显示“请先选择场景”，
 * 这是真实状态，不伪造一个不存在的场景。
 */
export function useSceneScope() {
  const route = useRoute();
  const router = useRouter();

  const scenes = ref<ScenarioOption[]>([]);
  const scenesError = ref<string | null>(null);
  const scenarioCode = ref<string | null>(null);

  async function loadScenes(): Promise<void> {
    try {
      const payload = await listSceneOptions();
      scenes.value = payload.items;
      scenesError.value = null;
    } catch (error) {
      scenes.value = [];
      scenesError.value = error instanceof Error ? error.message : "场景列表读取失败";
      throw error;
    }
  }

  function apply(code: string | null): void {
    const query: Record<string, string> = { ...(route.query as Record<string, string>) };
    if (code) {
      query.scenario = code;
    } else {
      delete query.scenario;
    }
    // 旧链接可能带着 ?scope=：场景层级已无版本概念，读到即清理，避免留下无效参数。
    delete query.scope;
    void router.replace({ query });
  }

  function resetToDefault(): void {
    // 没有可用场景时保持未选中；有场景时落在默认场景上（已停用的不优先）。
    const fallback = scenes.value.find((item) => item.enabled) ?? scenes.value[0] ?? null;
    scenarioCode.value = fallback?.scenario_code ?? null;
    apply(scenarioCode.value);
  }

  function syncFromRoute(): void {
    const code = typeof route.query.scenario === "string" && route.query.scenario ? route.query.scenario : null;
    scenarioCode.value = code;
    if (typeof route.query.scope === "string") {
      // 旧链接的 scope 已无意义，清掉它并把 URL 与真实场景同步。
      apply(code);
      return;
    }
    if (!code) {
      resetToDefault();
    }
  }

  watch(
    () => route.query.scenario,
    () => {
      syncFromRoute();
    },
  );

  return {
    scenes,
    scenesError,
    scenarioCode,
    loadScenes,
    apply,
    syncFromRoute,
  };
}
