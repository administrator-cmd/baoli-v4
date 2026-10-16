import { createRouter, createWebHistory, type RouteRecordRaw } from "vue-router";

import AuditTaskDetailView from "@/views/audits/AuditTaskDetailView.vue";
import AuditTaskListView from "@/views/audits/AuditTaskListView.vue";
import ExecutionListView from "@/views/audits/ExecutionListView.vue";
import DocumentListView from "@/views/documents/DocumentListView.vue";
import EvidenceListView from "@/views/documents/EvidenceListView.vue";
import ImportWorkbenchView from "@/views/imports/ImportWorkbenchView.vue";
import NotFoundView from "@/views/NotFoundView.vue";
import PlanEditView from "@/views/plans/PlanEditView.vue";
import PlanConfigIndexView from "@/views/plans/PlanConfigIndexView.vue";
import PlanListView from "@/views/plans/PlanListView.vue";
import ReportListView from "@/views/reports/ReportListView.vue";
import RiskResultListView from "@/views/reports/RiskResultListView.vue";
import SceneDetailView from "@/views/scenes/SceneDetailView.vue";
import SceneConfigIndexView from "@/views/scenes/SceneConfigIndexView.vue";
import SceneImportsView from "@/views/scenes/SceneImportsView.vue";
import SceneListView from "@/views/scenes/SceneListView.vue";
import SceneRuleEditView from "@/views/scenes/SceneRuleEditView.vue";
import OperationLogView from "@/views/system/OperationLogView.vue";
import ServiceStatusView from "@/views/system/ServiceStatusView.vue";

/**
 * 路由与侧边栏六组菜单一一对应。
 * meta.group 决定侧边栏分组；meta.menu 决定所属列表菜单。
 * 详情页高亮所属分组与列表菜单。
 */
/**
 * 场景配置类页面与场景详情 tab 的关系。
 *
 * 材料、字段、规则这些配置属于某个场景，因此它们是场景详情的 tab，
 * 不是独立页面：独立的「不选场景就能进」的路由会让用户绕过
 * 「建场景 → 上传规则库 → 选择解析工作表 → 核对候选 → 保存草稿」的主流程，
 * 直接改到某一份配置上。场景身份只在路径里（`:scenarioCode`）。
 */
const routes: RouteRecordRaw[] = [
  { path: "/", redirect: "/audit-tasks" },

  // 场景管理
  { path: "/scenes", name: "scenes", component: SceneListView, meta: { title: "场景列表", group: "场景管理", menu: "/scenes" } },
  { path: "/scene-config", name: "scene-config", component: SceneConfigIndexView, meta: { title: "场景配置", group: "场景管理", menu: "/scene-config" } },
  {
    path: "/scenes/:scenarioCode",
    name: "scene-detail",
    component: SceneDetailView,
    meta: { title: "场景配置", group: "场景管理", menu: "/scene-config" },
  },
  {
    // 规则编辑是场景上下文页面：路径里已经带固定的场景编码，
    // 不存在「不选场景就能改规则」这条路径。
    path: "/scene-rules/edit/:scenarioCode/:ruleKey",
    name: "scene-rule-edit",
    component: SceneRuleEditView,
    meta: { title: "规则编辑", group: "场景管理", menu: "/scene-config" },
  },
  {
    // 导入作业列表与工作台是场景上下文页面：上传必须以场景为起点，
    // 页面可从侧栏进入，也可通过 `?scenario=` 指定场景；作业记录所属场景。
    path: "/scene-imports",
    name: "scene-imports",
    component: SceneImportsView,
    meta: { title: "规则库导入", group: "场景管理", menu: "/scene-imports" },
  },
  {
    path: "/scene-imports/:jobNo",
    name: "scene-import-detail",
    component: ImportWorkbenchView,
    meta: { title: "导入详情", group: "场景管理", menu: "/scene-imports" },
  },

  // 方案管理
  { path: "/plans", name: "plans", component: PlanListView, meta: { title: "方案列表", group: "方案管理", menu: "/plans" } },
  { path: "/plans/new", name: "plan-new", component: PlanEditView, meta: { title: "新增方案", group: "方案管理", menu: "/plans" } },
  { path: "/plans/edit", name: "plan-config", component: PlanConfigIndexView, meta: { title: "方案编辑", group: "方案管理", menu: "/plans/edit" } },
  { path: "/plans/:planCode", name: "plan-edit", component: PlanEditView, meta: { title: "方案编辑", group: "方案管理", menu: "/plans/edit" } },

  // 审核管理
  { path: "/audit-tasks", name: "audit-tasks", component: AuditTaskListView, meta: { title: "审核任务", group: "审核管理", menu: "/audit-tasks" } },
  {
    path: "/audit-tasks/:taskNo",
    name: "audit-task-detail",
    component: AuditTaskDetailView,
    meta: { title: "任务详情", group: "审核管理", menu: "/audit-tasks" },
  },
  { path: "/executions", name: "executions", component: ExecutionListView, meta: { title: "执行记录", group: "审核管理", menu: "/executions" } },

  // 资料管理
  { path: "/documents", name: "documents", component: DocumentListView, meta: { title: "审核材料", group: "资料管理", menu: "/documents" } },
  { path: "/evidence", name: "evidence", component: EvidenceListView, meta: { title: "证据记录", group: "资料管理", menu: "/evidence" } },

  // 报告管理
  { path: "/reports", name: "reports", component: ReportListView, meta: { title: "审核报告", group: "报告管理", menu: "/reports" } },
  { path: "/risk-results", name: "risk-results", component: RiskResultListView, meta: { title: "风险结果", group: "报告管理", menu: "/risk-results" } },

  // 系统管理
  { path: "/system/services", name: "system-services", component: ServiceStatusView, meta: { title: "服务状态", group: "系统管理", menu: "/system/services" } },
  { path: "/system/operations", name: "system-operations", component: OperationLogView, meta: { title: "操作日志", group: "系统管理", menu: "/system/operations" } },

  { path: "/:pathMatch(.*)*", name: "not-found", component: NotFoundView, meta: { title: "页面不存在", group: "" } },
];

const router = createRouter({
  history: createWebHistory(),
  routes,
});

export default router;
