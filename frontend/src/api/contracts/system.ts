/** 系统与能力目录：服务状态、上传策略、材料类型目录、能力目录与规则状态口径。 */

import type { ApiResponse } from "./common";

export interface UploadKindPolicy {
  max_bytes: number;
  allowed_extensions: string[];
}

export interface UploadPolicy extends ApiResponse {
  rulebook: UploadKindPolicy;
  document: UploadKindPolicy;
  scope_note: string;
}

export interface MaterialTypeItem {
  id: number;
  name: string;
}

export interface RuleStatusItem {
  status: string | null;
  known: boolean;
  label: string | null;
  category: string;
  category_label: string | null;
  note: string | null;
}

export interface RuleStatusCatalog extends ApiResponse {
  statuses: RuleStatusItem[];
  categories: Array<{ category: string; label: string }>;
}

export interface SystemServiceItem {
  name: string;
  capability: string;
  configuration_status: string;
  connection_status: string;
  last_checked_at: string | null;
  missing: string[];
  note: string | null;
}

export interface SystemServices extends ApiResponse {
  database: "CONNECTED" | "FAILED";
  services: SystemServiceItem[];
}
