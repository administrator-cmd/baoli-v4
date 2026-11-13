/** 面向业务人员的规则库编制文档、作业与文件记录。 */
import type { OperationAccepted } from "./common";

export type GenerationStatus =
  | "NOT_GENERATED" | "PENDING" | "RUNNING" | "EDITABLE" | "NO_DEFINITIONS"
  | "FAILED" | "INTERRUPTED" | "UNKNOWN";
export type GenerationSourceKind = "ORIGINAL_EXCEL" | "USER_INPUT" | "DERIVED";
export type GenerationIssueStatus = "OPEN" | "RESOLVED";

export interface GenerationOrigin {
  sheet_name: string;
  row_no: number;
  related_rows: Array<{ sheet_name: string; row_no: number }>;
  cell_range: string | null;
}
export interface GenerationSource {
  kind: GenerationSourceKind;
  origin: GenerationOrigin | null;
  requirement_ids: string[];
  note: string;
}
export interface GenerationRequirement {
  requirement_id: string;
  text: string;
  category: string;
  source: GenerationSource;
}
export interface GenerationColumn {
  column_id: string;
  name: string;
  description: string;
  is_business_code?: boolean;
  code_role?: "IDENTITY" | "REFERENCE" | "GROUP" | null;
}
export interface GenerationEntry {
  row_id: string;
  cells: Record<string, string>;
  source: GenerationSource;
  business_code?: string;
  entry_kind?: "RULE" | "MATERIAL" | "REFERENCE" | "OTHER";
  related_row_ids?: string[];
}
export interface GenerationSheet {
  sheet_id: string;
  name: string;
  description: string;
  columns: GenerationColumn[];
  rows: GenerationEntry[];
  source: GenerationSource;
  business_code_scope?: "DOCUMENT" | "SHEET";
}
export interface GenerationIssue {
  issue_id: string;
  title: string;
  detail: string;
  affected_row_ids: string[];
  status: GenerationIssueStatus;
  resolution: string;
  source: GenerationSource;
}
export interface GenerationDocument {
  schema_version: "rulebook_generation_v2";
  title: string;
  summary: string;
  requirements: GenerationRequirement[];
  sheets: GenerationSheet[];
  issues: GenerationIssue[];
  code_counters?: Record<string, number>;
}

export interface GenerationJob {
  job_no: string;
  source_filename: string;
  source_size: number;
  business_background: string;
  generation_requirements: string;
  document_schema_version: string;
  prompt_version: string;
  revision: number;
  status: GenerationStatus;
  status_message: string;
  document: GenerationDocument | null;
  latest_operation: OperationAccepted | null;
  document_supported: boolean;
  entry_count: number;
  open_issue_count: number;
  can_generate: boolean;
  can_retry: boolean;
  retry_requires_new_job: boolean;
  can_edit: boolean;
  created_at: string;
  updated_at: string;
  reused: boolean;
}

export interface GenerationExportSummary {
  export_no: string;
  revision: number;
  filename: string;
}

export interface GenerationJobRow {
  job_no: string;
  source_filename: string;
  source_size: number;
  status: GenerationStatus;
  status_message: string;
  revision: number;
  latest_operation: OperationAccepted | null;
  latest_export: GenerationExportSummary | null;
  created_at: string;
  updated_at: string;
}

export interface GenerationCreated {
  job: GenerationJob;
  reused: boolean;
}

export interface GenerationRetryResult {
  job: GenerationJob;
  operation: OperationAccepted;
  reused: boolean;
}

export interface GenerationMessage {
  id: number;
  role: "USER" | "ASSISTANT";
  content: string;
  issue_id?: string | null;
  answer?: string | null;
  revision: number;
  created_at: string;
  operation: OperationAccepted;
}

export interface GenerationMessages {
  items: GenerationMessage[];
  next_before_id: number | null;
}

export interface GenerationRevision {
  revision: number;
  summary: string;
  origin: "INITIAL" | "MANUAL" | "AI" | "RESTORE" | "BASELINE";
  created_at: string;
}

export interface GenerationImportLink {
  import_job_no: string | null;
  scenario_code: string | null;
  import_status: string | null;
  deleted: boolean;
  created_at: string;
}

export interface GenerationExport {
  export_no: string;
  revision: number;
  filename: string;
  size: number;
  sha256: string;
  export_format_version: string;
  created_at: string;
  imports: GenerationImportLink[];
}

export interface GenerationImportResult {
  export_no: string;
  revision: number;
  import_job_no: string;
  scenario_code: string;
  import_status: string;
  reused: boolean;
}

export interface GenerationQuery {
  filename?: string;
  status?: GenerationStatus;
  created_from?: string;
  created_to?: string;
  page: number;
  page_size: number;
}
