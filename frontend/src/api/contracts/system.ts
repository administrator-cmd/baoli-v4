/** 系统与能力目录：服务状态、上传策略、材料类型目录、能力目录与规则状态口径。 */

export interface UploadKindPolicy {
  max_bytes: number;
  allowed_extensions: string[];
}

export interface UploadPolicy {
  rulebook: UploadKindPolicy;
  document: UploadKindPolicy;
  scope_note: string;
}

export interface MaterialTypeItem {
  id: number;
  name: string;
}

/** 一个状态码在某个语境下的标准文案。 */
export interface StatusVocabularyTerm {
  code: string;
  label: string;
  category: string | null;
  category_label: string | null;
  note: string | null;
}

/** 一个语境（规则、风险、字段）下的完整状态词表。 */
export interface StatusVocabularyDomain {
  domain: string;
  statuses: StatusVocabularyTerm[];
}

/** 全部语境的审核状态词表与分档。 */
export interface StatusVocabulary {
  domains: StatusVocabularyDomain[];
  categories: Array<{ category: string; label: string }>;
}

export interface SystemServiceItem {
  name: string;
  capability: string;
  configuration_status: string;
  connection_status: string;
  last_checked_at: string | null;
  latency_ms: number | null;
  missing: string[];
  note: string | null;
}

export interface SystemServices {
  database: "CONNECTED" | "FAILED";
  services: SystemServiceItem[];
}
