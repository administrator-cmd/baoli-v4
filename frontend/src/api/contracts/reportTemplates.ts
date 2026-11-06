export type ReportTemplateStatus = "PROCESSING" | "READY" | "FAILED";

import type { OperationAccepted } from "./common";

export interface ReportStyleSection {
  name: string;
  purpose: string;
}

export interface ReportStyleProfile {
  version: "1.0";
  document_overview: string;
  structure: Record<string, unknown>;
  sections: ReportStyleSection[];
  expression: Record<string, unknown>;
  formatting_conventions: string[];
}

export interface ReportTemplate {
  template_no: string;
  name: string;
  source_filename: string;
  source_size: number;
  page_count: number;
  status: ReportTemplateStatus;
  style_profile: ReportStyleProfile | null;
  analysis_error: string | null;
  latest_operation: OperationAccepted | null;
  created_at: string;
  updated_at: string;
}

export interface ReportTemplateUploadAccepted {
  template: ReportTemplate;
  operation: OperationAccepted;
}

export interface ReportTemplateOption {
  template_no: string;
  name: string;
}
