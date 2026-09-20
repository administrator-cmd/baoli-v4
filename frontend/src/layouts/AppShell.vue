<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from "vue";
import { useRoute } from "vue-router";
import {
  ChartNoAxesCombined,
  ClipboardList,
  FolderOpen,
  Layers,
  ListChecks,
  PanelLeftClose,
  PanelLeftOpen,
  RefreshCw,
  Scale,
  Settings,
} from "lucide-vue-next";

import type { SystemStatus } from "@/api/contracts";
import type { ApiError } from "@/api/http";
import { getSystemStatus } from "@/api/system";
import SystemStatusPopover from "@/components/SystemStatusPopover.vue";

const route = useRoute();

/** 六组菜单与十七个子页面；顺序与设计规范一致。 */
const menuGroups = [
  {
    name: "场景管理",
    icon: Layers,
    items: [
      { path: "/scenes", title: "场景列表" },
      { path: "/scene-materials", title: "材料配置" },
      { path: "/scene-fields", title: "字段配置" },
      { path: "/scene-rules", title: "规则配置" },
      { path: "/scene-imports", title: "规则库导入" },
    ],
  },
  {
    name: "方案管理",
    icon: ClipboardList,
    items: [
      { path: "/plans", title: "方案列表" },
    ],
  },
  {
    name: "审核管理",
    icon: ListChecks,
    items: [
      { path: "/audit-tasks", title: "审核任务" },
      { path: "/reviews", title: "人工复核" },
      { path: "/executions", title: "执行记录" },
    ],
  },
  {
    name: "资料管理",
    icon: FolderOpen,
    items: [
      { path: "/documents", title: "审核材料" },
      { path: "/evidence", title: "证据记录" },
    ],
  },
  {
    name: "报告管理",
    icon: ChartNoAxesCombined,
    items: [
      { path: "/reports", title: "审核报告" },
      { path: "/risk-results", title: "风险结果" },
    ],
  },
  {
    name: "系统管理",
    icon: Settings,
    items: [
      { path: "/system/services", title: "服务状态" },
      { path: "/system/operations", title: "操作日志" },
    ],
  },
];

/** 当前高亮的菜单路径：详情页高亮所属列表菜单。 */
const activeMenu = computed(() => (typeof route.meta.menu === "string" ? route.meta.menu : route.path));

/** 当前路由所在的菜单分组：默认展开该组。 */const openedGroups = ref<string[]>([]);

watch(
  () => route.fullPath,
  () => {
    const group = menuGroups.find((item) => item.items.some((entry) => entry.path === activeMenu.value));
    openedGroups.value = group ? [group.name] : [];
  },
  { immediate: true },
);

const systemStatus = ref<SystemStatus | null>(null);
const statusError = ref<string | null>(null);
const statusLoading = ref(false);

const collapsed = ref(false);
const isNarrow = ref(false);
let mediaQuery: MediaQueryList | null = null;

function applyViewport(matches: boolean): void {
  isNarrow.value = matches;
  collapsed.value = matches;
}

function onViewportChange(event: MediaQueryListEvent): void {
  applyViewport(event.matches);
}

onMounted(() => {
  mediaQuery = window.matchMedia("(max-width: 992px)");
  applyViewport(mediaQuery.matches);
  mediaQuery.addEventListener("change", onViewportChange);
  void refreshStatus();
});

onUnmounted(() => {
  mediaQuery?.removeEventListener("change", onViewportChange);
});

function setFailure(error: unknown): void {
  if (error instanceof Error && "code" in error && "requestId" in error) {
    statusError.value = (error as ApiError).message;
    return;
  }
  statusError.value = "无法连接到服务，系统状态暂时不可用。";
}

/** 系统状态只在顶部状态入口展示；页面级错误由页面自己显示，不重复出现。 */
async function refreshStatus(): Promise<void> {
  statusLoading.value = true;
  statusError.value = null;
  try {
    systemStatus.value = await getSystemStatus();
  } catch (error) {
    setFailure(error);
  } finally {
    statusLoading.value = false;
  }
}
</script>

<template>
  <el-container class="app-shell">
    <el-aside :width="collapsed ? 'var(--app-aside-collapsed-width)' : 'var(--app-aside-width)'" class="app-aside">
      <div class="app-brand">
        <span class="app-brand__mark"><Scale :size="18" aria-hidden="true" /></span>
        <span v-if="!collapsed" class="app-brand__text">
          <strong>保理合同审核系统</strong>
        </span>
      </div>

      <el-menu
        class="app-menu"
        :default-active="activeMenu"
        :collapse="collapsed"
        :collapse-transition="false"
        :default-openeds="openedGroups"
        router
        unique-opened
        background-color="var(--app-menu-bg)"
        text-color="var(--app-menu-text)"
        active-text-color="var(--app-menu-text-active)"
      >
        <el-sub-menu v-for="group in menuGroups" :key="group.name" :index="group.name">
          <template #title>
            <el-icon><component :is="group.icon" /></el-icon>
            <span>{{ group.name }}</span>
          </template>
          <el-menu-item v-for="item in group.items" :key="item.path" :index="item.path">
            {{ item.title }}
          </el-menu-item>
        </el-sub-menu>
      </el-menu>
    </el-aside>

    <el-container class="app-body">
      <el-header class="app-header">
        <div class="app-header__left">
          <el-button
            class="app-header__toggle"
            text
            :aria-label="collapsed ? '展开菜单' : '折叠菜单'"
            :title="collapsed ? '展开菜单' : '折叠菜单'"
            @click="collapsed = !collapsed"
          >
            <el-icon><PanelLeftClose v-if="!collapsed" /><PanelLeftOpen v-else /></el-icon>
          </el-button>
        </div>

        <div class="app-header__right">
          <el-tooltip content="刷新系统状态" placement="bottom">
            <el-button :loading="statusLoading" aria-label="刷新系统状态" @click="refreshStatus">
              <el-icon><RefreshCw /></el-icon>
            </el-button>
          </el-tooltip>
          <SystemStatusPopover
            :status="systemStatus"
            :error-message="statusError"
            :loading="statusLoading"
            @refresh="refreshStatus"
          />
        </div>
      </el-header>

      <el-main class="app-main">
        <div class="app-main__inner">
          <RouterView />
        </div>
      </el-main>
    </el-container>
  </el-container>
</template>
