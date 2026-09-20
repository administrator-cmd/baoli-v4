<script setup lang="ts">
import { onMounted, ref } from "vue";

import SceneRulesPanel from "@/components/scenes/SceneRulesPanel.vue";
import SceneScopeBar from "@/components/scenes/SceneScopeBar.vue";
import PageHeader from "@/components/PageHeader.vue";
import { useSceneScope } from "@/composables/useSceneScope";

/** 规则配置（场景管理）：业务规则可编辑；自动抽取与报告生成规则只读。 */
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
      title="规则配置"
      subtitle="业务规则可增删改与启停；字段抽取与报告生成规则由系统派生，只能查看不能修改。"
      :show-refresh="false"
    />
    <SceneScopeBar
      :scenes="scenes"
      :scenario-code="scenarioCode"
      @change="onScopeChange"
    />
    <SceneRulesPanel :scenario-code="scenarioCode" :refresh-token="refreshToken" />
  </div>
</template>
