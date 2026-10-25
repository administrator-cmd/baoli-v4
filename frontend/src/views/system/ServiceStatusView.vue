<script setup lang="ts">
import { onMounted, ref } from "vue";

import type { SystemServices } from "@/api/contracts";
import { getSystemServices } from "@/api/system";
import PageHeader from "@/components/PageHeader.vue";
import { formatDate as formatDateTime, statusLabel, statusTagType } from "@/constants/labels";

/**
 * 服务状态：能力、配置状态、连接状态分开显示。
 * 只有真实探测过的连接（数据库）才显示“已连接”；外部服务只报告配置状态与“未验证”，
 * 不在状态页代替业务操作做供应商探测，也不写“服务就绪”。
 */
const payload = ref<SystemServices | null>(null);
const loading = ref(false);
const error = ref<string | null>(null);

async function load(): Promise<void> {
  loading.value = true;
  error.value = null;
  try {
    payload.value = await getSystemServices();
  } catch (failure) {
    payload.value = null;
    error.value = failure instanceof Error ? failure.message : "服务状态读取失败";
  } finally {
    loading.value = false;
  }
}

function connectionText(status: string): string {
  if (status === "CONNECTED") return "已连接（本次真实探测）";
  if (status === "FAILED") return "连接失败";
  if (status === "UNVERIFIED") return "未验证";
  return "—";
}

function connectionTone(status: string): "success" | "danger" | "warning" | "info" {
  if (status === "CONNECTED") return "success";
  if (status === "FAILED") return "danger";
  if (status === "UNVERIFIED") return "warning";
  return "info";
}

function formatTime(value: string | null): string {
  return value ? formatDateTime(value) : "—";
}

onMounted(() => {
  void load();
});
</script>

<template>
  <div class="app-panel">
    <PageHeader
      eyebrow="系统管理"
      title="服务状态"
      :show-refresh="false"
    />
    <div class="app-toolbar">
      <div class="app-toolbar__left">
        <span v-if="payload" class="text-muted">
          数据库：{{ payload.database === "CONNECTED" ? "已连接" : "连接失败" }}
        </span>
      </div>
      <div class="app-toolbar__right">
        <el-button :loading="loading" @click="load">重新检查</el-button>
      </div>
    </div>

    <el-alert v-if="error" type="error" show-icon :closable="false" :title="`读取失败：${error}`">
      <el-button size="small" @click="load">重试</el-button>
    </el-alert>

    <el-alert
      v-else
      class="block-gap-sm"
      type="info"
      show-icon
      :closable="false"
      title="“已配置，未验证”只表示必需配置项已填写，不代表供应商服务已经连通"
    >
      外部服务的真实连通性在执行具体操作时验证；失败会返回明确错误，不会静默降级。
    </el-alert>

    <el-table :data="payload?.services ?? []" border stripe v-loading="loading" empty-text="暂无数据">
      <el-table-column prop="name" label="服务名称" width="140" />
      <el-table-column prop="capability" label="能力" min-width="240" show-overflow-tooltip />
      <el-table-column label="配置状态" width="150">
        <template #default="{ row }">
          <el-tag :type="statusTagType(row.configuration_status)" size="small" effect="light" disable-transitions>
            {{ statusLabel(row.configuration_status) }}
          </el-tag>
          <div v-if="row.missing?.length" class="text-hint">缺少：{{ row.missing.join("、") }}</div>
        </template>
      </el-table-column>
      <el-table-column label="连接状态" width="160">
        <template #default="{ row }">
          <el-tag :type="connectionTone(row.connection_status)" size="small" effect="light" disable-transitions>
            {{ connectionText(row.connection_status) }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column label="最近检查时间" width="170">
        <template #default="{ row }">{{ formatTime(row.last_checked_at) }}</template>
      </el-table-column>
      <el-table-column prop="note" label="说明" min-width="260" show-overflow-tooltip>
        <template #default="{ row }">{{ row.note || "—" }}</template>
      </el-table-column>
    </el-table>

  </div>
</template>
