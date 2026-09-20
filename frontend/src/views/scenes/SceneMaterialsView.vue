<script setup lang="ts">
import { onMounted, ref } from "vue";

import SceneMaterialsPanel from "@/components/scenes/SceneMaterialsPanel.vue";
import SceneScopeBar from "@/components/scenes/SceneScopeBar.vue";
import PageHeader from "@/components/PageHeader.vue";
import { useSceneScope } from "@/composables/useSceneScope";

/**
 * 材料配置（场景管理）：维护所选场景的材料位定义，保存即生效。
 * 实际上传文件在资料管理中按任务维护；这里不向旧任务自动增加材料位。
 */
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
      title="材料配置"
      subtitle="维护所选场景的材料位定义，保存即生效；任务的文件上传在资料管理按材料位进行。"
      :show-refresh="false"
    />
    <SceneScopeBar
      :scenes="scenes"
      :scenario-code="scenarioCode"
      @change="onScopeChange"
    />
    <SceneMaterialsPanel :scenario-code="scenarioCode" :refresh-token="refreshToken" />
  </div>
</template>
