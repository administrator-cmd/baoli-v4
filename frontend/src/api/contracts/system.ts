/** 系统与能力目录：服务状态、上传策略、材料类型目录、能力目录与规则状态口径。 */

import type { ApiResponse, JsonValue } from "./common";

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

/** 模型可调用工具及其参数和实际实现信息。 */
export interface ToolCatalogItem {
  tool_id: string;
  version: string;
  label: string;
  description: string;
  input_schema: Record<string, JsonValue>;
  output_schema: Record<string, JsonValue>;
  handler_ref: Record<string, string> | null;
  allowed_scenes: string[] | null;
  required_evidence: string[];
  side_effect: boolean;
  idempotent: boolean;
  supports_query: boolean;
  /** 已注册在工具目录里。 */
  registered: boolean;
  /** 当前能被真实执行（节点级实现或 Agent 工具都可以为 true）。 */
  implemented: boolean;
  /** 模型在规则评估循环里能选它。与 implemented 是两件事。 */
  agent_selectable: boolean;
  /** agent_selectable=false 的真实原因。 */
  agent_unselectable_reason: string | null;
  error_codes: string[];
  unimplemented_reason: string | null;
  input_count: string | null;
  runtime_keys: string[];
}

export interface ToolCatalog extends ApiResponse {
  tool_catalog_version: string;
  /** 工具目录内容指纹：能发现「版本号没改但内容变了」。 */
  tool_catalog_fingerprint: string;
  tools: ToolCatalogItem[];
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
