<script setup lang="ts">
import { onMounted, ref } from "vue";
import { useRouter } from "vue-router";
import { listAdminSceneOptions, type ScenarioOption } from "@/api/scenes";
import PageHeader from "@/components/PageHeader.vue";

const router = useRouter();
const scenes = ref<ScenarioOption[]>([]);
const selected = ref("");
const loading = ref(false);
const error = ref("");

async function load(): Promise<void> {
  loading.value = true;
  error.value = "";
  try {
    scenes.value = (await listAdminSceneOptions()).items;
  } catch (failure) {
    error.value = failure instanceof Error ? failure.message : "场景读取失败";
  } finally {
    loading.value = false;
  }
}

function open(): void {
  if (selected.value) {
    void router.push({ name: "scene-detail", params: { scenarioCode: selected.value } });
  }
}

onMounted(() => void load());
</script>

<template>
  <div class="app-panel">
    <PageHeader eyebrow="场景管理" title="场景配置" :show-refresh="false" />
    <el-alert v-if="error" type="error" show-icon :closable="false" :title="error">
      <el-button size="small" @click="load">重试</el-button>
    </el-alert>
    <div class="app-toolbar">
      <div class="app-toolbar__left">
        <el-select v-model="selected" filterable :loading="loading" placeholder="选择场景" style="width: min(360px, 100%)" @change="open">
          <el-option v-for="scene in scenes" :key="scene.scenario_code" :value="scene.scenario_code" :label="scene.scenario_name" />
        </el-select>
      </div>
    </div>
    <el-empty v-if="!loading && !error && scenes.length === 0" description="暂无场景">
      <el-button type="primary" @click="router.push({ name: 'scenes' })">新建场景</el-button>
    </el-empty>
  </div>
</template>
