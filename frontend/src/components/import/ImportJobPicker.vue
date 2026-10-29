<script setup lang="ts">
import type { ImportJobSummary } from "@/api/contracts";
import StateBlock from "@/components/StateBlock.vue";
import StatusBadge from "@/components/StatusBadge.vue";
import { formatDate } from "@/constants/labels";

/** 未选择具体作业时的入口：接着处理一个已有导入作业。 */
defineProps<{
  jobs: ImportJobSummary[];
  pending: boolean;
  loading: boolean;
}>();

defineEmits<{
  select: [job: ImportJobSummary];
  refresh: [];
}>();
</script>

<template>
  <el-card shadow="never">
    <template #header>
      <div class="card-header">
        <span>继续规则库导入</span>
        <el-button size="small" :loading="pending" @click="$emit('refresh')">刷新列表</el-button>
      </div>
    </template>

    <StateBlock v-if="loading" kind="loading" />
    <StateBlock
      v-else-if="!jobs.length"
      kind="empty"
      title="还没有导入作业"
      description="上传 .xlsx 或 .xlsm 规则库文件后，系统读取工作表并按原文生成执行配置。"
    />
    <el-table v-else :data="jobs" border @row-click="$emit('select', $event)">
      <el-table-column label="规则库文件" prop="source_filename" min-width="240" show-overflow-tooltip />
      <el-table-column label="上传时间" width="200">
        <template #default="{ row }"><span class="table-nowrap">{{ formatDate(row.created_at) }}</span></template>
      </el-table-column>
      <el-table-column label="状态" width="140">
        <template #default="{ row }">
          <StatusBadge :status="row.status" />
        </template>
      </el-table-column>
      <el-table-column label="操作" width="100" align="center">
        <template #default="{ row }">
          <el-button link type="primary" @click.stop="$emit('select', row)">继续处理</el-button>
        </template>
      </el-table-column>
    </el-table>
  </el-card>
</template>
