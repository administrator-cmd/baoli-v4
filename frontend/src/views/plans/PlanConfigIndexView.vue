<script setup lang="ts">
import { onMounted, ref } from "vue";
import { useRouter } from "vue-router";

import { listPlans } from "@/api/plans";
import type { PlanListItem } from "@/api/contracts";
import PageHeader from "@/components/PageHeader.vue";

const router = useRouter();
const plans = ref<PlanListItem[]>([]);
const selected = ref("");
const loading = ref(false);
const error = ref("");

async function load(): Promise<void> {
  loading.value = true;
  error.value = "";
  try {
    const all: PlanListItem[] = [];
    let page = 1;
    let total = 0;
    do {
      const result = await listPlans({ page, page_size: 100, deleted: false });
      all.push(...result.items);
      total = result.total;
      page += 1;
    } while (all.length < total);
    plans.value = all;
  } catch (failure) {
    error.value = failure instanceof Error ? failure.message : "方案读取失败";
  } finally {
    loading.value = false;
  }
}

function open(): void {
  if (selected.value) {
    void router.push({ name: "plan-edit", params: { planCode: selected.value } });
  }
}

onMounted(() => void load());
</script>

<template>
  <div class="app-panel">
    <PageHeader eyebrow="方案管理" title="方案编辑" subtitle="选择方案后调整材料、字段、规则和报告章节。" :show-refresh="false">
      <template #actions>
        <el-button @click="router.push({ name: 'plans' })">返回方案列表</el-button>
      </template>
    </PageHeader>

    <el-alert v-if="error" type="error" show-icon :closable="false" :title="error">
      <el-button size="small" @click="load">重试</el-button>
    </el-alert>

    <div class="app-toolbar block-gap-sm">
      <div class="app-toolbar__left">
        <el-select v-model="selected" filterable :loading="loading" placeholder="选择要编辑的方案" style="width: min(460px, 100%)" @change="open">
          <el-option
            v-for="plan in plans"
            :key="plan.plan_code"
            :value="plan.plan_code"
            :label="`${plan.plan_name} · ${plan.scenario_name}`"
          />
        </el-select>
      </div>
    </div>

    <el-empty v-if="!loading && !error && plans.length === 0" description="暂无可编辑方案">
      <el-button type="primary" @click="router.push({ name: 'plan-new' })">新建方案</el-button>
    </el-empty>
  </div>
</template>
