import { onScopeDispose, ref, type Ref } from "vue";

import { getTaskReport, getTaskRisks, listTaskExecutions } from "@/api/audits";
import type { ExecutionRow, Report, RiskList } from "@/api/contracts";
import type { DetailTab } from "@/constants/auditWorkspaces";

interface AuditTaskResultsOptions {
  taskNo: Readonly<Ref<string>>;
  generation: () => number;
  isStale: (token: number, taskNo: string) => boolean;
}

/** 风险、报告与执行记录按页签读取；任务切换后丢弃旧响应并清理全部结果。 */
export function useAuditTaskResults(options: AuditTaskResultsOptions) {
  const { taskNo } = options;
  let disposed = false;
  const isStale = (token: number, currentTaskNo: string) => disposed || options.isStale(token, currentTaskNo);

  const risks = ref<RiskList | null>(null);
  const risksError = ref<string | null>(null);
  const risksLoading = ref(false);
  let risksRequestGeneration = 0;

  const report = ref<Report | null>(null);
  const reportError = ref<string | null>(null);
  const reportLoading = ref(false);
  let selectedReportNo: string | undefined;
  let reportRequestGeneration = 0;
  /* 执行记录（页签） */
  const executions = ref<ExecutionRow[]>([]);
  const executionsError = ref<string | null>(null);
  const executionsLoading = ref(false);
  let executionsRequestGeneration = 0;

  async function loadExecutions(token: number): Promise<void> {
    const currentTaskNo = taskNo.value;
    if (isStale(token, currentTaskNo)) return;
    const request = ++executionsRequestGeneration;
    const stale = () => isStale(token, currentTaskNo) || request !== executionsRequestGeneration;
    executionsLoading.value = true;
    executionsError.value = null;
    try {
      const payload = await listTaskExecutions(currentTaskNo);
      if (stale()) return;
      executions.value = payload.items;
    } catch (error) {
      if (stale()) return;
      executions.value = [];
      executionsError.value = error instanceof Error ? error.message : "执行记录读取失败";
    } finally {
      if (!stale()) executionsLoading.value = false;
    }
  }

  /** 模板里的重试按钮：用当前页代次重新读取对应列表。 */
  function retryLoadExecutions(): void {
    void loadExecutions(options.generation());
  }

  /**
   * 已为当前任务加载过的页签。
   * 材料在任务详情里；风险、报告、执行记录只在用户真正切到对应页签时加载。
   * 加载失败时从集合中移除，用户再次进入该页签会重新请求，而不是停在错误态无法自救。
   */
  const loadedTabs = new Set<DetailTab>();

  async function ensureTabLoaded(target: DetailTab, token = options.generation()): Promise<void> {
    const currentTaskNo = taskNo.value;
    if (!currentTaskNo || isStale(token, currentTaskNo) || loadedTabs.has(target)) return;
    loadedTabs.add(target);
    if (target === "risks") {
      await loadRisks(currentTaskNo, token);
      if (isStale(token, currentTaskNo)) return;
      if (risksError.value) loadedTabs.delete(target);
    } else if (target === "report") {
      const loads = [loadReport(currentTaskNo, token)];
      // 报告页的风险结论说明依赖风险结果；首次打开报告页时一并加载。
      if (!loadedTabs.has("risks")) {
        loadedTabs.add("risks");
        loads.push(loadRisks(currentTaskNo, token));
      }
      await Promise.allSettled(loads);
      if (isStale(token, currentTaskNo)) return;
      if (reportError.value) loadedTabs.delete(target);
      if (risksError.value) loadedTabs.delete("risks");
    } else if (target === "executions") {
      await loadExecutions(token);
      if (isStale(token, currentTaskNo)) return;
      if (executionsError.value) loadedTabs.delete(target);
    }
  }

  async function loadRisks(currentTaskNo: string, token: number): Promise<void> {
    if (isStale(token, currentTaskNo)) return;
    const request = ++risksRequestGeneration;
    const stale = () => isStale(token, currentTaskNo) || request !== risksRequestGeneration;
    risksLoading.value = true;
    risksError.value = null;
    try {
      const value = await getTaskRisks(currentTaskNo);
      if (stale()) return;
      risks.value = value;
    } catch (error) {
      if (stale()) return;
      risks.value = null;
      risksError.value = error instanceof Error ? error.message : "未知原因";
    } finally {
      if (!stale()) risksLoading.value = false;
    }
  }

  async function loadReport(currentTaskNo: string, token: number, reportNo?: string): Promise<void> {
    if (isStale(token, currentTaskNo)) return;
    if (reportNo !== undefined) selectedReportNo = reportNo;
    const generation = ++reportRequestGeneration;
    const stale = () => isStale(token, currentTaskNo) || generation !== reportRequestGeneration;
    reportLoading.value = true;
    reportError.value = null;
    try {
      const value = await getTaskReport(currentTaskNo, selectedReportNo);
      if (stale()) return;
      report.value = value;
    } catch (error) {
      if (stale()) return;
      // 切换列表中的某份报告失败时保留已加载的报告清单，让弹窗显示读取失败原因并可关闭重试。
      if (reportNo === undefined) report.value = null;
      reportError.value = error instanceof Error ? error.message : "未知原因";
    } finally {
      if (!stale()) reportLoading.value = false;
    }
  }

  function reset(): void {
    loadedTabs.clear();
    risksRequestGeneration += 1;
    executionsRequestGeneration += 1;
    risks.value = null;
    risksError.value = null;
    risksLoading.value = false;
    report.value = null;
    selectedReportNo = undefined;
    reportRequestGeneration += 1;
    reportError.value = null;
    reportLoading.value = false;
    executions.value = [];
    executionsError.value = null;
    executionsLoading.value = false;
  }

  onScopeDispose(() => {
    disposed = true;
    reset();
  });

  async function refreshLoaded(currentTaskNo: string, token: number): Promise<void> {
    if (isStale(token, currentTaskNo)) return;
    const loads: Array<Promise<void>> = [];
    if (loadedTabs.has("risks")) loads.push(loadRisks(currentTaskNo, token));
    if (loadedTabs.has("report")) loads.push(loadReport(currentTaskNo, token));
    if (loadedTabs.has("executions")) loads.push(loadExecutions(token));
    await Promise.allSettled(loads);
  }

  return {
    risks,
    risksError,
    risksLoading,
    report,
    reportError,
    reportLoading,
    executions,
    executionsError,
    executionsLoading,
    loadReport,
    retryLoadExecutions,
    ensureTabLoaded,
    refreshLoaded,
    reset,
  };
}
