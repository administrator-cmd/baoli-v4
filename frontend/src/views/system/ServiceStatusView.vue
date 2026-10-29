<script setup lang="ts">
import { onMounted, ref } from "vue";

import type { SystemServices } from "@/api/contracts";
import { getSystemServices } from "@/api/system";
import { formatDate as formatDateTime } from "@/constants/labels";

/**
 * 服务状态：数据库与大模型各执行一次真实探测。
 * 配置缺失时不发请求；连接失败保留原因与耗时，不写“服务就绪”。
 * 60 秒内的重复查看复用上一次探测结论，页面显示的是那次探测的时间。
 */
const payload = ref<SystemServices | null>(null);
const loading = ref(false);
const error = ref<string | null>(null);

async function load(refresh = false): Promise<void> {
  loading.value = true;
  error.value = null;
  try {
    payload.value = await getSystemServices(refresh);
  } catch (failure) {
    payload.value = null;
    error.value = failure instanceof Error ? failure.message : "服务状态读取失败";
  } finally {
    loading.value = false;
  }
}

function connectionText(status: string): string {
  if (status === "CONNECTED") return "已连接";
  if (status === "FAILED") return "连接失败";
  if (status === "NOT_CONFIGURED") return "未配置";
  // 未知状态显示原始码，不能归入“未配置”掩盖新增状态。
  return status;
}

function connectionTone(status: string): "success" | "danger" | "warning" | "info" {
  if (status === "CONNECTED") return "success";
  if (status === "FAILED") return "danger";
  if (status === "NOT_CONFIGURED") return "info";
  return "warning";
}

function formatLatency(value: number | null): string {
  return value === null ? "—" : `${value} ms`;
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
    <div class="app-toolbar">
      <div class="app-toolbar__left">
        <span class="text-muted">数据库与大模型均为真实探测；点「重新检查」立即重测</span>
      </div>
      <div class="app-toolbar__right">
        <el-button :loading="loading" @click="load(true)">重新检查</el-button>
      </div>
    </div>

    <el-alert v-if="error" type="error" show-icon :closable="false" :title="`读取失败：${error}`">
      <el-button size="small" @click="load(true)">重试</el-button>
    </el-alert>

    <el-table v-else :data="payload?.services ?? []" border stripe v-loading="loading" empty-text="暂无数据">
      <el-table-column prop="name" label="服务" width="160" />
      <el-table-column label="连接状态" width="140">
        <template #default="{ row }">
          <el-tag :type="connectionTone(row.connection_status)" size="small" effect="light" disable-transitions>
            {{ connectionText(row.connection_status) }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column label="耗时" width="120">
        <template #default="{ row }">{{ formatLatency(row.latency_ms) }}</template>
      </el-table-column>
      <el-table-column label="最近检查时间" width="200">
        <template #default="{ row }">{{ formatTime(row.last_checked_at) }}</template>
      </el-table-column>
      <el-table-column label="说明" min-width="320" show-overflow-tooltip>
        <template #default="{ row }">{{ row.note || "—" }}</template>
      </el-table-column>
    </el-table>
  </div>
</template>
