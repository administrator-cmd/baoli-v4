import { createRouter, createWebHistory, type LocationQuery, type RouteRecordRaw } from "vue-router";

import AuditTaskDetailView from "@/views/audits/AuditTaskDetailView.vue";
import AuditTaskListView from "@/views/audits/AuditTaskListView.vue";
import ExecutionListView from "@/views/audits/ExecutionListView.vue";
import ReviewListView from "@/views/audits/ReviewListView.vue";
import DocumentListView from "@/views/documents/DocumentListView.vue";
import EvidenceListView from "@/views/documents/EvidenceListView.vue";
import ImportWorkbenchView from "@/views/imports/ImportWorkbenchView.vue";
import NotFoundView from "@/views/NotFoundView.vue";
import PlanEditView from "@/views/plans/PlanEditView.vue";
import PlanListView from "@/views/plans/PlanListView.vue";
import ReportListView from "@/views/reports/ReportListView.vue";
import RiskResultListView from "@/views/reports/RiskResultListView.vue";
import SceneDetailView from "@/views/scenes/SceneDetailView.vue";
import SceneFieldsView from "@/views/scenes/SceneFieldsView.vue";
import SceneImportsView from "@/views/scenes/SceneImportsView.vue";
import SceneListView from "@/views/scenes/SceneListView.vue";
import SceneMaterialsView from "@/views/scenes/SceneMaterialsView.vue";
import SceneRuleEditView from "@/views/scenes/SceneRuleEditView.vue";
import SceneRulesView from "@/views/scenes/SceneRulesView.vue";
import OperationLogView from "@/views/system/OperationLogView.vue";
import ServiceStatusView from "@/views/system/ServiceStatusView.vue";

/**
 * 路由与侧边栏六组菜单一一对应。
 * meta.group 决定侧边栏分组；meta.menu 决定所属列表菜单。
 * 详情页高亮所属分组与列表菜单。
 */
/** 旧链接重定向时保留除对象参数以外的查询参数（operation/tab 等）。 */
function forwardQuery(source: LocationQuery, drop: string): Record<string, string> {
  const query: Record<string, string> = {};
  for (const [key, value] of Object.entries(source)) {
    if (key === drop || typeof value !== "string") {
      continue;
    }
    query[key] = value;
  }
  return query;
}

const routes: RouteRecordRaw[] = [
  { path: "/", redirect: "/audit-tasks" },

  // ---- 场景管理 ----
  { path: "/scenes", name: "scenes", component: SceneListView, meta: { title: "场景列表", group: "场景管理", menu: "/scenes" } },
  {
    path: "/scenes/:scenarioCode",
    name: "scene-detail",
    component: SceneDetailView,
    meta: { title: "场景详情", group: "场景管理", menu: "/scenes" },
  },
  { path: "/scene-materials", name: "scene-materials", component: SceneMaterialsView, meta: { title: "材料配置", group: "场景管理", menu: "/scene-materials" } },
  { path: "/scene-fields", name: "scene-fields", component: SceneFieldsView, meta: { title: "字段配置", group: "场景管理", menu: "/scene-fields" } },
  { path: "/scene-rules", name: "scene-rules", component: SceneRulesView, meta: { title: "规则配置", group: "场景管理", menu: "/scene-rules" } },
  {
    path: "/scene-rules/edit/:scenarioCode/:ruleKey",
    name: "scene-rule-edit",
    component: SceneRuleEditView,
    meta: { title: "规则编辑", group: "场景管理", menu: "/scene-rules" },
  },
  { path: "/scene-imports", name: "scene-imports", component: SceneImportsView, meta: { title: "规则库导入", group: "场景管理", menu: "/scene-imports" } },
  {
    path: "/scene-imports/:jobNo",
    name: "scene-import-detail",
    component: ImportWorkbenchView,
    meta: { title: "导入详情", group: "场景管理", menu: "/scene-imports" },
  },

  // ---- 方案管理 ----
  { path: "/plans", name: "plans", component: PlanListView, meta: { title: "方案列表", group: "方案管理", menu: "/plans" } },
  { path: "/plans/new", name: "plan-new", component: PlanEditView, meta: { title: "新增方案", group: "方案管理", menu: "/plans" } },
  { path: "/plans/:planCode", name: "plan-edit", component: PlanEditView, meta: { title: "方案编辑", group: "方案管理", menu: "/plans" } },

  // ---- 审核管理 ----
  { path: "/audit-tasks", name: "audit-tasks", component: AuditTaskListView, meta: { title: "审核任务", group: "审核管理", menu: "/audit-tasks" } },
  {
    path: "/audit-tasks/:taskNo",
    name: "audit-task-detail",
    component: AuditTaskDetailView,
    meta: { title: "任务详情", group: "审核管理", menu: "/audit-tasks" },
  },
  { path: "/reviews", name: "reviews", component: ReviewListView, meta: { title: "人工复核", group: "审核管理", menu: "/reviews" } },
  { path: "/executions", name: "executions", component: ExecutionListView, meta: { title: "执行记录", group: "审核管理", menu: "/executions" } },

  // ---- 资料管理 ----
  { path: "/documents", name: "documents", component: DocumentListView, meta: { title: "审核材料", group: "资料管理", menu: "/documents" } },
  { path: "/evidence", name: "evidence", component: EvidenceListView, meta: { title: "证据记录", group: "资料管理", menu: "/evidence" } },

  // ---- 报告管理 ----
  { path: "/reports", name: "reports", component: ReportListView, meta: { title: "审核报告", group: "报告管理", menu: "/reports" } },
  { path: "/risk-results", name: "risk-results", component: RiskResultListView, meta: { title: "风险结果", group: "报告管理", menu: "/risk-results" } },

  // ---- 系统管理 ----
  { path: "/system/services", name: "system-services", component: ServiceStatusView, meta: { title: "服务状态", group: "系统管理", menu: "/system/services" } },
  { path: "/system/operations", name: "system-operations", component: OperationLogView, meta: { title: "操作日志", group: "系统管理", menu: "/system/operations" } },

  // ---- 旧链接兼容：显式重定向并保留对象与其余查询参数（operation / tab），不跳到别的记录 ----
  {
    path: "/imports",
    redirect: (to) =>
      typeof to.query.job === "string" && to.query.job
        ? { name: "scene-import-detail", params: { jobNo: to.query.job }, query: forwardQuery(to.query, "job") }
        : { name: "scene-imports" },
  },
  {
    path: "/audits",
    redirect: (to) =>
      typeof to.query.task === "string" && to.query.task
        ? { name: "audit-task-detail", params: { taskNo: to.query.task }, query: forwardQuery(to.query, "task") }
        : { name: "audit-tasks" },
  },

  { path: "/:pathMatch(.*)*", name: "not-found", component: NotFoundView, meta: { title: "页面不存在", group: "" } },
];

const router = createRouter({
  history: createWebHistory(),
  routes,
});

export default router;
