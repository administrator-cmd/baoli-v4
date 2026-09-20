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

export function listReports(query: ReportQuery): Promise<Paged<ReportRow>> {
  return request<Paged<ReportRow>>(`/reports${toQuery({ ...query })}`);
}

export function reportExportUrl(query: Omit<ReportQuery, "page" | "page_size">): string {
  return apiFileUrl(`/reports/export${toQuery({ ...query })}`);
}

export function getReport(reportNo: string): Promise<ReportDetail> {
  return request<ReportDetail>(`/reports/${encodeURIComponent(reportNo)}`);
}

export function updateReportRemark(reportNo: string, remark: string | null): Promise<ReportDetail> {
  return request<ReportDetail>(`/reports/${encodeURIComponent(reportNo)}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ remark }),
  });
}

export function setReportArchived(reportNo: string, archived: boolean): Promise<ReportDetail> {
  return request<ReportDetail>(
    `/reports/${encodeURIComponent(reportNo)}/${archived ? "archive" : "unarchive"}`,
    { method: "POST" },
  );
}

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
   * 两套等级标签（高/中/低/待确认 与 最高/一级/二级/三级）并存，
   * 硬编码一份会让另一套一条都筛不出来，而页面看起来一切正常。
   */
  risk_levels?: string[];
}

export function listRiskResults(query: RiskQuery): Promise<RiskPage> {
  return request<RiskPage>(`/risk-results${toQuery({ ...query })}`);
}

export function riskExportUrl(query: Omit<RiskQuery, "page" | "page_size">): string {
  return apiFileUrl(`/risk-results/export${toQuery({ ...query })}`);
}
