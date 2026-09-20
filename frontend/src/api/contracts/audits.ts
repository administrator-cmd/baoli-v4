/**
 * 审核任务：任务详情、节点、字段与规则结果、复核待办、证据、纠错与检索。
 *
 * 契约按业务域拆分：本文件只声明本域的载荷形状，跨域引用一律显式 import，
 * 不再让所有页面共同依赖一个不断扩大的中心文件。
 */

import type { ApiResponse, JsonValue, OperationAccepted } from "./common";
import type { TaskScope } from "./scenes";
import type { TaskPlanInfo } from "./plans";

export interface AuditNode {
  node_code: string;
  name: string;
  sequence_no: number;
  enabled: boolean;
  status: string;
  failure_message: string | null;
  skip_reason: string | null;
}

export interface AuditDocument {
  document_no: string;
  /** 材料键：检索取证的定位依据，不用中文名匹配材料。 */
  material_key?: string | null;
  material_name: string;
  required: boolean;
  status: string;
  filename: string | null;
  /** 当前文件的上传编号：下载/预览与历史版本定位用；没有当前文件时为 null。 */
  upload_no?: string | null;
  /**
   * 该材料位的 8018 识别结论；材料处理节点尚未执行时为 null。
   * 置信度与判定类型只作展示，不参与任何判定，也不影响材料状态。
   */
  material_classify?: {
    confidence?: number | null;
    declared_material_type?: string | null;
    detected_material_type?: string | null;
    matched_keywords?: JsonValue[] | null;
    matches_declared_material?: boolean | null;
  } | null;
}

export interface AuditField {
  name: string;
  material_name: string;
  status: string;
  raw_value: string | null;
  normalized_value: JsonValue;
  evidence_no: string | null;
}

export interface RuleInputSnapshot {
  /**
   * 输入种类。缺省为字段取值；MATERIAL 表示这条检查是按材料逐份判定的
   * （文件质量检查、材料检查），此时 material_key 是材料位而非字段。
   */
  kind?: "FIELD" | "MATERIAL" | null;
  field_key?: string | null;
  material_key?: string | null;
  field_name?: string | null;
  material_name?: string | null;
  /** 材料级输入：该份材料的文件名与它在本次检查里的判定。 */
  filename?: string | null;
  message?: string | null;
  value_type?: string | null;
  unit?: string | null;
  currency?: string | null;
  status?: string | null;
  /** 抽取到的原文（事实本身）。 */
  raw_value?: string | null;
  /** 由原文确定性算出的标准化值；标准化不改变原文，也不代表审核通过。 */
  normalized_value?: JsonValue;
  value?: JsonValue;
  has_evidence?: boolean | null;
}

export interface RuleCheckComparison {
  kind?: string | null;
  left_label?: string | null;
  left_value?: string | null;
  left_raw_value?: string | null;
  right_label?: string | null;
  right_value?: string | null;
  right_raw_value?: string | null;
  /** 加总比较：各加数的标准化值，与 addend_labels 按下标一一对应。 */
  addends?: string[] | null;
  addend_labels?: string[] | null;
  expression?: string | null;
  mode_label?: string | null;
  threshold?: string | null;
  actual_diff?: string | null;
  basis?: string | null;
}

export interface RuleStandard {
  operator_label?: string | null;
  requirement?: string | null;
  check_level?: string | null;
  risk_level?: string | null;
  comparison_mode?: string | null;
  comparison_label?: string | null;
  threshold?: string | number | null;
  threshold_unit?: string | null;
  /** 文件质量检查的检查项目与合格要求（业务配置事实，与审核要求原文分开表达）。 */
  check_item?: string | null;
  check_item_label?: string | null;
  check_requirement?: string | null;
  check_requirement_label?: string | null;
  /** 标准是否明确；为 false 时 missing 说明具体缺什么。 */
  clear?: boolean | null;
  missing?: string | null;
}

export interface AuditRule {
  rule_key?: string;
  name: string;
  status: string;
  message: string;
  result: Record<string, JsonValue> | null;
  configured_requirement?: string | null;
  evidence?: Array<{
    evidence_no: string;
    material_name: string | null;
    filename: string | null;
    quote_text: string;
  }>;
}

export interface RetrievalCandidateSnapshot {
  index_no?: string | null;
  chunk_no?: number | null;
  chunk_id?: number | null;
  source_entry_no?: number | null;
  subject_name?: string | null;
  material_key?: string | null;
  material_name?: string | null;
  filename?: string | null;
  similarity?: string | null;
}

export interface RetrievalRecordSnapshot {
  retrieval_no?: string | null;
  status?: string | null;
  message?: string | null;
  candidate_count?: number;
  candidates?: RetrievalCandidateSnapshot[];
  adopted?: RetrievalCandidateSnapshot[];
  uncovered_materials?: Array<{ material_key?: string; reason?: string }>;
  excluded_indexes?: Array<{ index_no?: string; reason?: string }>;
}

export interface SealRecord {
  material_key?: string | null;
  material_name?: string | null;
  filename?: string | null;
  artifact_no?: string | null;
  page_no?: number | null;
  entry_no?: number | null;
  subject_name?: string | null;
  seal_type?: string | null;
  confidence?: number | null;
}

export interface SubjectQuerySnapshot {
  field_key?: string | null;
  field_name?: string | null;
  material_key?: string | null;
  material_name?: string | null;
  value?: string | null;
  status?: string | null;
}

export interface PendingReview {
  review_no: string;
  review_type: string;
  business_note: string | null;
  proposed_value: string | null;
  material_name?: string | null;
  field_name?: string | null;
  rule_name?: string | null;
  /** 系统原判断的不可变快照：状态、消息、原值、依据引用。 */
  system_judgment?: {
    status: string | null;
    message: string | null;
    raw_value: string | null;
    normalized_value?: JsonValue;
    evidence?: {
      evidence_no?: string | null;
      page_no?: number | null;
      quote_text?: string | null;
      bbox?: JsonValue;
      /** 位置结论；只是辅助定位信息。 */
      locator_status?: string | null;
      locator_note?: string | null;
      /** 内容依据结论：引文能否在所属材料里找到原文。 */
      quote_check?: string | null;
      quote_check_label?: string | null;
    } | null;
    /**
     * 主体/签章一致性核对的判据：
     *   retrieval 是本次检索的真实范围、候选身份与双方原文；
     *   seal_records 是规则材料范围内的**全部**印章识别条目；
     *   subject_query 是本次实际使用的待核对主体原文。
     * 人工要同时看到这三项才能判断是否一致，不能在响应投影里丢掉。
     */
    retrieval?: RetrievalRecordSnapshot | null;
    seal_records?: SealRecord[] | null;
    subject_query?: SubjectQuerySnapshot | null;
    /**
     * 材料类型待复核的原始识别结论：
     *   service_code 是给出结论的外部服务（当前为 MATERIAL_CLASSIFY）；
     *   service_result 是它的原样返回（declared/detected_material_type、confidence、
     *   matched_keywords 等）。
     * 置信度不参与任何判定，只是给人工判断"文件是否放错材料位"的依据，必须展示。
     */
    service_code?: string | null;
    service_result?: Record<string, JsonValue> | null;
  } | null;
  /** 已完成裁决时的业务结论与复核后有效状态。 */
  manual_conclusion?: string | null;
  effective_status?: string | null;
  basis_type?: string | null;
  before_value?: string | null;
  after_value?: string | null;
  evidence_quote?: string | null;
  evidence_no?: string | null;
}

export interface TaskReviewJudgment {
  status: string | null;
  message: string | null;
  raw_value: string | null;
  retrieval?: RetrievalRecordSnapshot | null;
  seal_records?: SealRecord[] | null;
  subject_query?: SubjectQuerySnapshot | null;
}

export interface AuditTask extends ApiResponse {
  task_no: string;
  task_name: string;
  scenario_name: string;
  version_no: number;
  status: string;
  /** 任务基本资料的时间；执行快照不随其变化。 */
  created_at?: string | null;
  updated_at?: string | null;
  current_node: AuditNode;
  nodes: AuditNode[];
  documents: AuditDocument[];
  fields: AuditField[];
  rules: AuditRule[];
  pending_reviews: PendingReview[];
  unconfigured_check_nodes: string[];
  /** 任务输入修订号：首次补传、替换材料、人工有效值变化都会递增。 */
  input_revision: number;
  /** 任务实际执行范围：未配置 / 未启用 / 自动补入的区分依据（含规则中文名）。 */
  scope?: TaskScope | null;
  latest_operation: OperationAccepted | null;
  /** 任务备注（可编辑基本资料）。 */
  remark?: string | null;
  scenario_code?: string;
  deleted?: boolean;
  /** 方案绑定：为 null 表示旧任务没有方案关联（“历史任务配置”）。 */
  plan?: TaskPlanInfo | null;
  is_legacy_config?: boolean;
}

export interface ExecuteNodeResponse extends ApiResponse {
  task_no: string;
  node_name: string;
  execution_status: string;
  task_status: string;
  next_node_name: string | null;
  result_summary: Record<string, JsonValue>;
}

export interface ManualReviewResponse extends ApiResponse {
  task_no: string;
  resolved_count: number;
  status: string;
  next_node_name: string | null;
  field_changes: Array<{
    field_key: string;
    field_name?: string | null;
    material_name?: string | null;
    before_value: string | null;
    after_value: string | null;
    fact_revision_no?: number | null;
  }>;
  invalidated_reviews: number;
}

export interface RiskList extends ApiResponse {
  task_no: string;
  risk_configured: boolean;
  risk_executed: boolean;
  risk_conclusion_available: boolean;
  /** 只统计 ABNORMAL；不能用结果总数冒充命中数。 */
  hit_count: number;
  status_counts: Record<string, number>;
  items: Array<{
    name: string;
    status: string;
    message: string;
    risk_level?: string | null;
    details: Record<string, JsonValue> | null;
  }>;
}

export interface AuditTaskSummary extends ApiResponse {
  task_no: string;
  task_name: string;
  scenario_name: string;
  status: string;
  current_node_name: string;
  updated_at: string;
}

export interface Evidence extends ApiResponse {
  evidence_no: string;
  document_name: string | null;
  page_no: number | null;
  quote_text: string;
  bbox: number[] | null;
  /** 位置（页码/坐标）的核对结论；位置只是辅助信息，对不上不影响内容可用。 */
  locator_status: "VERIFIED" | "UNVERIFIED" | "UNLOCATABLE";
  locator_note: string | null;
  /** 内容依据的核对结论：引文能否在所属材料里找到原文。 */
  quote_check?: "FOUND" | "NOT_FOUND" | "NOT_CHECKABLE" | "PENDING" | null;
  quote_check_label?: string | null;
  /** 该证据所属的上传版本与产物；旧上传的证据按历史身份查看。 */
  document_upload_no: string | null;
  artifact_type: string | null;
  is_current_upload: boolean;
  historical: boolean;
}

export interface FieldCorrectionRequest {
  field_key: string;
  material_key: string;
  decision: "MODIFY_FIELD" | "MARK_NOT_PRESENT";
  field_value?: string;
  business_note: string;
  basis_type?: "HUMAN_STATEMENT" | "DOCUMENT_EVIDENCE" | "UPLOAD_RECORD";
  idempotency_key?: string;
  input_revision?: number;
}

export interface FieldCorrectionResponse extends ApiResponse {
  task_no: string;
  field_key: string;
  material_key: string;
  review_no: string;
  fact_revision_no: number;
  previous_revision_no: number | null;
  before_value: string | null;
  after_value: string | null;
  normalized_value: JsonValue;
  status: string;
  invalidated_reviews: number;
  invalidated_rule_results: number;
  recomputed_rules: string[];
}

export interface OcrReconcileResponse extends ApiResponse {
  document_no: string;
  artifact_no: string;
  status: string;
  job_id: string | null;
  deadline_at: string | null;
  message: string;
}

export interface ReviewDraftRecord {
  review_no: string;
  decision: string | null;
  business_note: string | null;
  field_value: string | null;
  basis_type: string | null;
  updated_at?: string;
}

export interface ReviewDraftPayloadItem {
  review_no: string;
  decision: string | null;
  business_note: string | null;
  field_value: string | null;
  basis_type: string | null;
}

export interface ReviewDraftResult extends ApiResponse {
  task_no: string;
  pending_hash: string | null;
  saved_review_nos: string[];
  drafts: Record<string, ReviewDraftRecord>;
}

export interface DocumentRemoveResult extends ApiResponse {
  document_no: string;
  material_name: string;
  status: string;
  input_revision: number;
  impact: {
    affected_artifacts: number;
    stale_field_values: number;
    invalidated_rule_results: number;
    invalidated_node_executions: number;
    invalidated_reviews: number;
    reset_to_node: string | null;
    description: string;
  } | null;
}

export interface RetrievalCandidate {
  chunk_id: number;
  chunk_no: number;
  index_no: string | null;
  /** 印章条目在原产物中的条目序号：候选身份的一部分，可以回到原始条目。 */
  source_entry_no: number | null;
  /** 印章识别出的主体名称。向量只由它生成，页码等定位信息不参与相似度。 */
  subject_name: string;
  material_key: string | null;
  material_name: string | null;
  filename: string | null;
  upload_no: string | null;
  /** 相似度只用于候选排序，不构成主体一致的结论。 */
  similarity: string;
}

export interface RetrievalResult extends ApiResponse {
  task_no: string;
  retrieval_no: string;
  status: string;
  message: string;
  model_id?: string | null;
  model_revision?: string | null;
  index_version?: string | null;
  index_versions?: string[];
  scope?: Record<string, JsonValue>;
  candidates: RetrievalCandidate[];
  /** 本次实际送入后续步骤的条目；它是上下文预算，不是人工结论。 */
  adopted: RetrievalCandidate[];
  /** 有上传但没有可用主体索引的材料及原因（未识别主体 / 未建索引 / 索引失效）。 */
  uncovered_materials?: Array<{ material_key?: string; reason?: string }>;
  excluded_indexes?: Array<{ index_no?: string; reason?: string }>;
}

export type RetrievalDecision = "CONSISTENT" | "INCONSISTENT" | "EVIDENCE_INSUFFICIENT";

export interface RetrievalDecisionResult extends ApiResponse {
  task_no: string;
  retrieval_no: string;
  decision: string;
  note: string | null;
  selected: Record<string, JsonValue> | null;
  decided_at: string | null;
  decision_input_revision: number | null;
}

export interface EvidenceIndexItem {
  index_no: string;
  artifact_type: string;
  status: string;
  /** 索引是否仍适用于当前产物内容：产物重新识别后旧索引只是历史记录。 */
  current_for_artifact: boolean;
  chunk_count: number;
  model_id: string;
  model_revision: string;
  embedding_dim?: number | null;
  chunk_version?: string | null;
  index_version: string;
  document_upload_no: string | null;
  filename: string | null;
  material_name: string | null;
  built_at: string | null;
  failure_code: string | null;
  failure_message: string | null;
}

export interface TaskEvidenceIndex extends ApiResponse {
  task_no: string;
  index_version: string | null;
  model_id: string | null;
  model_revision: string | null;
  chunk_version?: string | null;
  indexed: boolean;
  items: EvidenceIndexItem[];
}

export interface RetrievalHistoryItem {
  retrieval_no: string;
  rule_key: string | null;
  node_code: string | null;
  query_text: string;
  scope: Record<string, JsonValue>;
  model_id: string;
  model_revision: string;
  index_versions: string[];
  status: string;
  message: string | null;
  top_k: number;
  candidate_count: number;
  candidates: RetrievalCandidate[];
  /** 本次送入后续步骤的条目；不代表工作人员已确认过其中任何一条。 */
  adopted: RetrievalCandidate[];
  /** 人工结论与候选分开保存。 */
  human_decision: string | null;
  human_decision_note: string | null;
  human_selected: Record<string, JsonValue> | null;
  human_decided_at: string | null;
  human_decided_by: string | null;
  decision_input_revision: number | null;
  index_version: string | null;
  embedding_dim: number | null;
  input_revision: number;
  created_at: string;
}
