<script setup lang="ts">
import type { ScenarioOption } from "@/api/scenes";

/**
 * 场景配置页的作用域选择条：只选择场景。
 *
 * 场景配置只有一份，因此这里没有“配置版本”下拉——不生成“第 N 版”这类
 * 页面曾经需要、现在已不存在的选项。
 */
const props = defineProps<{
  scenes: ScenarioOption[];
  scenarioCode: string | null;
}>();

const emit = defineEmits<{
  change: [payload: { scenarioCode: string | null }];
}>();

function onSceneChange(value: string): void {
  emit("change", { scenarioCode: value || null });
}
</script>

<template>
  <el-form inline class="app-query">
    <el-form-item label="所属场景">
      <el-select
        :model-value="scenarioCode ?? ''"
        placeholder="请选择场景"
        filterable
        style="width: 220px"
        @update:model-value="onSceneChange(String($event))"
      >
        <el-option v-for="item in scenes" :key="item.scenario_code" :value="item.scenario_code" :label="item.scenario_name" />
      </el-select>
    </el-form-item>
    <el-form-item>
      <span class="text-hint">配置修改保存后立即生效；已建方案与任务使用创建时的快照，不受影响。</span>
    </el-form-item>
  </el-form>
</template>
