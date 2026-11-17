<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from "vue";
import { useRoute } from "vue-router";
import type { MenuInstance } from "element-plus";
import {
  ChartNoAxesCombined,
  ClipboardList,
  FolderOpen,
  Layers,
  ListChecks,
  PanelLeftClose,
  PanelLeftOpen,
  Scale,
  Settings,
} from "lucide-vue-next";

const route = useRoute();

/** 六组业务菜单。 */
const menuGroups = [
  {
    name: "场景管理",
    icon: Layers,
    items: [
      { path: "/scenes", title: "场景列表" },
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
    name: "任务管理",
    icon: ListChecks,
    items: [
      { path: "/audit-tasks", title: "审核任务" },
    ],
  },
  {
    name: "资料管理",
    icon: FolderOpen,
    items: [
      { path: "/documents", title: "审核材料" },
      { path: "/rulebooks", title: "规则库管理" },
    ],
  },
  {
    name: "报告管理",
    icon: ChartNoAxesCombined,
    items: [
      { path: "/reports", title: "审核报告" },
      { path: "/report-templates", title: "报告模板" },
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

/** 菜单分组独立展开；路由切换时只展开目标分组，不收起其他分组。 */
const menuRef = ref<MenuInstance | null>(null);
const openedGroups = ref<string[]>([]);

function onGroupOpen(index: string): void {
  if (!openedGroups.value.includes(index)) openedGroups.value = [...openedGroups.value, index];
}

function onGroupClose(index: string): void {
  openedGroups.value = openedGroups.value.filter((name) => name !== index);
}

watch(
  () => route.fullPath,
  () => {
    const group = menuGroups.find((item) => item.items.some((entry) => entry.path === activeMenu.value));
    if (group) {
      onGroupOpen(group.name);
      menuRef.value?.open(group.name);
    }
  },
  { immediate: true },
);

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
});

watch(
  collapsed,
  (value) => {
    // Dialog 弹到 body 下时，让需要在工作区居中的弹窗读取当前侧栏宽度。
    document.body.style.setProperty("--app-dialog-main-center-offset", value ? "32px" : "110px");
  },
  { immediate: true },
);

onUnmounted(() => {
  mediaQuery?.removeEventListener("change", onViewportChange);
  document.body.style.removeProperty("--app-dialog-main-center-offset");
});
</script>

<template>
  <el-container class="app-shell">
    <el-aside :width="collapsed ? 'var(--app-aside-collapsed-width)' : 'var(--app-aside-width)'" class="app-aside">
      <div class="app-brand">
        <span class="app-brand__mark"><Scale :size="18" aria-hidden="true" /></span>
        <span v-if="!collapsed" class="app-brand__text">
          <strong>资料审核系统</strong>
        </span>
      </div>

      <el-menu
        ref="menuRef"
        class="app-menu"
        :default-active="activeMenu"
        :collapse="collapsed"
        :collapse-transition="false"
        :default-openeds="openedGroups"
        router
        @open="onGroupOpen"
        @close="onGroupClose"
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
      </el-header>

      <el-main class="app-main">
        <div class="app-main__inner">
          <RouterView />
        </div>
      </el-main>
    </el-container>
  </el-container>
</template>
