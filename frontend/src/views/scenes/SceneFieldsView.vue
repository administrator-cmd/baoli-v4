<script setup lang="ts">
import { onMounted, ref } from "vue";

import SceneFieldsPanel from "@/components/scenes/SceneFieldsPanel.vue";
import SceneScopeBar from "@/components/scenes/SceneScopeBar.vue";
import PageHeader from "@/components/PageHeader.vue";
import { useSceneScope } from "@/composables/useSceneScope";

/** 字段配置（场景管理）：字段属于材料位；“抽取要求”随字段一起进入生效配置。 */
const scopeState = useSceneScope();
const { scenes, scenarioCode, loadScenes, apply, syncFromRoute } = scopeState;
const refreshToken = ref(0);

function onScopeChange(payload: { scenarioCode: string | null }): void {
  apply(payload.scenarioCode);
}

onMounted(async () => {
  // 顺序固定：先取到场景列表，再按 URL 决定作用域。
  // 反过来的话，没有 ?scenario= 时列表还是空的，默认场景就选不出来。
  await loadScenes();
  syncFromRoute();
});
</script>

<template>
  <div class="app-panel">
    <PageHeader
      eyebrow="场景管理"
      title="字段配置"
      subtitle="维护所选场景的字段定义与抽取要求；字段被规则或报告章节引用时不能删除。"
      :show-refresh="false"
    />
    <SceneScopeBar
      :scenes="scenes"
      :scenario-code="scenarioCode"
      @change="onScopeChange"
    />
    <SceneFieldsPanel :scenario-code="scenarioCode" :refresh-token="refreshToken" />
  </div>
</template>
