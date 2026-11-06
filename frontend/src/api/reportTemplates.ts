import type { OperationAccepted, ReportStyleProfile, ReportTemplate, ReportTemplateOption, ReportTemplateUploadAccepted } from "./contracts";
import { apiFileUrl, request, uploadWithProgress } from "./http";

export function reportTemplatePdfUrl(templateNo: string): string {
  return apiFileUrl(`/report-templates/${encodeURIComponent(templateNo)}/pdf`);
}

export function listReportTemplates(): Promise<{ items: ReportTemplate[] }> {
  return request<{ items: ReportTemplate[] }>("/report-templates");
}

export function listReportTemplateOptions(): Promise<{ items: ReportTemplateOption[] }> {
  return request<{ items: ReportTemplateOption[] }>("/report-templates/options");
}

export function uploadReportTemplate(
  file: File, name: string | undefined, idempotencyKey: string, onProgress?: (percent: number) => void,
): Promise<ReportTemplateUploadAccepted> {
  const form = new FormData();
  form.append("file", file);
  if (name?.trim()) form.append("name", name.trim());
  form.append("idempotency_key", idempotencyKey);
  return uploadWithProgress<ReportTemplateUploadAccepted>("/report-templates", form, onProgress);
}

export function reanalyzeReportTemplate(templateNo: string, idempotencyKey: string): Promise<OperationAccepted> {
  return request<OperationAccepted>(`/report-templates/${encodeURIComponent(templateNo)}/reanalyze`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ idempotency_key: idempotencyKey }),
  });
}

export function updateReportTemplate(templateNo: string, payload: { name: string; style_profile?: ReportStyleProfile | null }): Promise<ReportTemplate> {
  return request<ReportTemplate>(`/report-templates/${encodeURIComponent(templateNo)}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}
