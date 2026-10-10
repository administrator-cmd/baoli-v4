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
  /** 新建规则可选节点；历史规则读取和执行使用 nodes。 */
  new_rule_nodes: string[];
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

/**
 * 工具目录项：模型可以调用什么、参数长什么样、当前能不能真跑。
 *
 * 与 `CapabilityItem` 的分工：能力项回答「这条规则由哪项能力判定」，
 * 工具项回答「模型可以调用什么」。
 */
export interface ToolCatalogItem {
  tool_id: string;
  version: string;
  label: string;
  description: string;
  input_schema: Record<string, JsonValue>;
  output_schema: Record<string, JsonValue>;
  handler_ref: Record<string, string> | null;
  allowed_nodes: string[];
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
  /** 已实现但没有模型调用入口（由确定性节点执行）。 */
  node_level_only: boolean;
  /** agent_selectable=false 的真实原因。 */
  agent_unselectable_reason: string | null;
  error_codes: string[];
  unimplemented_reason: string | null;
  input_count: string | null;
  runtime_keys: string[];
}

export interface CapabilityCatalog extends ApiResponse {
  capability_version: string;
  rule_statuses: string[];
  items: CapabilityItem[];
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
