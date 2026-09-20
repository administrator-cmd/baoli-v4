<script setup lang="ts">
import { computed } from "vue";

import type { ExternalServiceConfiguration, SystemStatus } from "@/api/contracts";
import { configurationItemLabel, externalServiceDescription, statusTagType } from "@/constants/labels";

/**
 * 系统状态入口（顶部栏）。
 *
 * 主视图只回答「现在能不能用」：数据库是否连通、各外部服务是否配好。
 * 具体缺哪些配置项收在展开区域，主视图不堆技术字段。
 * 「已配置，未验证」只表示必填项已填，不代表服务已连通。
 */
const props = defineProps<{
  status: SystemStatus | null;
  errorMessage: string | null;
  loading: boolean;
}>();

defineEmits<{
  refresh: [];
}>();

const LABELS: Record<string, string> = {
  CONFIGURED_UNVERIFIED: "已配置，未验证",
  CONFIGURATION_INCOMPLETE: "配置不完整",
  UNCONFIGURED: "未配置",
};

const services = computed(() =>
  Object.entries(props.status?.external_services ?? {}).map(([name, configuration]: [string, ExternalServiceConfiguration]) => ({
    name,
    description: externalServiceDescription(name),
    label: LABELS[configuration.status] ?? "状态未知",
    tagType: statusTagType(configuration.status),
    missing: (configuration.missing ?? []).map((item) => configurationItemLabel(item)),
  })),
);

/** 数据库是否连通是唯一「立刻影响使用」的状态，用它做入口标签的底色。 */
const overallTagType = computed(() => {
  if (!props.status) {
    return "info";
  }
  if (props.status.database !== "CONNECTED") {
    return "danger";
  }
  return services.value.some((item) => item.missing.length) ? "warning" : "success";
});

const overallText = computed(() => {
  if (props.loading && !props.status) {
    return "系统状态";
  }
  if (!props.status) {
    return "系统状态未知";
  }
  if (props.status.database !== "CONNECTED") {
    return "数据库未连接";
  }
  return services.value.some((item) => item.missing.length) ? "部分服务未配置完整" : "服务就绪";
});
</script>

<template>
  <el-popover placement="bottom-end" :width="380" trigger="click" popper-class="system-status__popper">
    <template #reference>
      <el-tag :type="overallTagType" class="system-status__trigger" disable-transitions>
        系统状态 · {{ overallText }}
      </el-tag>
    </template>

    <div class="system-status">
      <div class="system-status__head">
        <strong>系统状态</strong>
        <el-button link type="primary" size="small" :loading="loading" @click="$emit('refresh')">刷新</el-button>
      </div>

      <el-alert
        v-if="errorMessage"
        type="error"
        show-icon
        :closable="false"
        :title="errorMessage"
        class="block-gap-sm"
      />

      <template v-else-if="status">
        <div class="system-status__row">
          <span class="system-status__name">数据库</span>
          <el-tag :type="status.database === 'CONNECTED' ? 'success' : 'danger'" size="small" disable-transitions>
            {{ status.database === "CONNECTED" ? "已连接" : "连接失败" }}
          </el-tag>
        </div>

        <div v-for="service in services" :key="service.name" class="system-status__row">
          <span class="system-status__name" :title="service.description">{{ service.name }}</span>
          <el-tag :type="service.tagType" size="small" disable-transitions>{{ service.label }}</el-tag>
        </div>

        <p class="text-hint block-gap-sm">
          外部服务未配置也可以查看任务与状态；真正依赖它们的操作会返回明确错误。
          「已配置，未验证」只表示必填项已填写，尚未实际连通验证。
        </p>

        <el-collapse v-if="services.some((item) => item.missing.length)" class="system-status__detail">
          <el-collapse-item title="查看缺失配置" name="missing">
            <div v-for="service in services.filter((item) => item.missing.length)" :key="service.name">
              <div class="system-status__name">{{ service.name }}</div>
              <ul>
                <li v-for="item in service.missing" :key="item">{{ item }}</li>
              </ul>
            </div>
          </el-collapse-item>
        </el-collapse>
      </template>

      <p v-else class="text-hint">{{ loading ? "正在读取系统状态…" : "尚未读取到系统状态。" }}</p>
    </div>
  </el-popover>
</template>

<style scoped>
.system-status__trigger {
  cursor: pointer;
}

.system-status__head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 8px;
}
</style>
