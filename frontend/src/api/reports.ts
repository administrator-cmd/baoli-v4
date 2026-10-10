import type { Paged, ReportDetail, ReportRow, RiskResultRow } from "./contracts";
import { apiFileUrl, request } from "./http";
import { toQuery } from "./query";

export interface ReportQuery {
  name?: string;
  task_no?: string;
  scenario_code?: string;
  plan_code?: string;
  archived?: boolean;
  generated_from?: string;
  generated_to?: string;
  page: number;
  page_size: number;
}

/** 审核报告分页列表。 */
export function listReports(query: ReportQuery): Promise<Paged<ReportRow>> {
  return request<Paged<ReportRow>>(`/reports${toQuery({ ...query })}`);
}

/** 报告列表导出地址；与当前筛选同一作用域。 */
export function reportExportUrl(query: Omit<ReportQuery, "page" | "page_size">): string {
  return apiFileUrl(`/reports/export${toQuery({ ...query })}`);
}

/** 报告详情：正文、三信号与结构化结论。 */
export function getReport(reportNo: string): Promise<ReportDetail> {
  return request<ReportDetail>(`/reports/${encodeURIComponent(reportNo)}`);
}

/** 只修改报告备注，不动正文、三信号与快照。 */
export function updateReportRemark(reportNo: string, remark: string | null): Promise<ReportDetail> {
  return request<ReportDetail>(`/reports/${encodeURIComponent(reportNo)}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ remark }),
  });
}

/** 归档 / 取消归档报告快照（只改归档状态，不删除快照）。 */
export function setReportArchived(reportNo: string, archived: boolean): Promise<ReportDetail> {
  return request<ReportDetail>(
    `/reports/${encodeURIComponent(reportNo)}/${archived ? "archive" : "unarchive"}`,
    { method: "POST" },
  );
}

/** 报告正文（Markdown）的下载地址。 */
export function reportDownloadUrl(reportNo: string): string {
  return apiFileUrl(`/reports/${encodeURIComponent(reportNo)}/download`);
}

export interface RiskQuery {
  task_no?: string;
  scenario_code?: string;
  plan_code?: string;
  risk_level?: string;
  status?: string;
  page: number;
  page_size: number;
}

export interface RiskPage extends Paged<RiskResultRow> {
  /** 当前筛选作用域下 ABNORMAL 的条数；不能用总数冒充命中数。 */
  hit_count: number;
  /**
   * 当前筛选作用域内真实出现过的风险等级。页面筛选下拉从这里取，不硬编码——
   * 各场景可以定义自己的等级名称。
   */
  risk_levels?: string[];
}

/** 风险结果分页列表；命中数取服务端 hit_count，不用总数冒充。 */
export function listRiskResults(query: RiskQuery): Promise<RiskPage> {
  return request<RiskPage>(`/risk-results${toQuery({ ...query })}`);
}

/** 风险结果导出地址；与当前筛选同一作用域。 */
export function riskExportUrl(query: Omit<RiskQuery, "page" | "page_size">): string {
  return apiFileUrl(`/risk-results/export${toQuery({ ...query })}`);
}
