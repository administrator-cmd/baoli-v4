/**
 * 系统与能力目录：服务状态、上传策略、材料类型目录、能力目录与规则状态口径。
 *
 * 契约按业务域拆分：本文件只声明本域的载荷形状，跨域引用一律显式 import，
 * 不再让所有页面共同依赖一个不断扩大的中心文件。
 */

import type { ApiResponse } from "./common";

export type ExternalServiceStatus = "UNCONFIGURED" | "CONFIGURATION_INCOMPLETE" | "CONFIGURED_UNVERIFIED";

export interface ExternalServiceConfiguration {
  status: ExternalServiceStatus;
  missing: string[];
}

export interface SystemStatus extends ApiResponse {
  service: string;
  database: "CONNECTED" | "FAILED";
  external_services: Record<string, ExternalServiceConfiguration>;
}

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

export interface CapabilityParameterSpec {
  required: string[];
  optional: string[];
  notes: string[];
}

export interface CapabilityItem {
  capability_id: string;
  label: string;
  /** OPERATOR=算子级能力；QUALITY_ITEM=文件检查子项。 */
  kind: "OPERATOR" | "QUALITY_ITEM" | string;
  parent: string | null;
  nodes: string[];
  input_kind: string;
  parameters: CapabilityParameterSpec;
  output_keys: string[];
  statuses: string[];
  supports_evidence: boolean;
  supports_comparison: boolean;
  /** 当前是否已实现。false 表示可以配置，但发布前必须逐条确认，运行时明确不可判定。 */
  implemented: boolean;
  unimplemented_reason: string | null;
  execution: string;
  evidence: string | null;
  judge: string | null;
  /** 能力专用扩展数据所在的命名空间；它不能混进所有规则共有的公共结构。 */
  extension_namespace: string | null;
  version: string;
  note: string | null;
}

export interface CapabilityCatalog extends ApiResponse {
  capability_version: string;
  rule_statuses: string[];
  items: CapabilityItem[];
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
