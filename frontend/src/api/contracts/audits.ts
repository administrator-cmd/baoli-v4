/** 审核任务：任务详情、节点、字段、规则结果、证据与字段纠错。 */

import type { JsonValue, OperationAccepted } from "./common";
import type { TaskScope } from "./scenes";
import type { TaskPlanInfo } from "./plans";

/** 继续执行的范围：四类互斥，数量由服务端按真实重跑计划给出。 */
export interface ResumeScope {
  /** 保留的已完成项。 */
  preserved: number;
  /** 补跑的未完成项。 */
  pending: number;
  /** 因上游重跑需要重算的已完成项。 */
  dependent: number;
  /** 本轮不执行、留待字段核对确认之后才执行的项。 */
  waiting_review?: number;
}

/** 字段核对缺项：真实没有可执行取值的字段与原因。 */
export interface FieldReviewMissing {
  field_key: string;
  material_key: string;
  field_name: string;
  material_name: string;
  status: string | null;
  /** 缺项原因码：材料未上传 / 未启用抽取 / 抽取未完成 / 材料中不存在 / 证据不足。 */
  reason: string;
  material_uploaded: boolean;
}

/** 字段核对状态：由服务端按当前执行批次、输入修订与字段版本算出，页面只展示。 */
export interface FieldReviewState {
  /** 本次计划是否存在字段核对等待点；没有抽取段时为 false。 */
  required: boolean;
  /** 当前字段版本是否已被确认。 */
  confirmed: boolean;
  confirmation_no: string | null;
  confirmed_at: string | null;
  confirmed_input_revision: number | null;
  input_revision: number;
  missing_field_count: number;
  missing_fields: FieldReviewMissing[];
  /** 确认后还要执行多少条规则。 */
  after_review_rule_count: number;
  /** 已有确认但输入或字段版本已变化时的原因。 */
  reconfirm_reason: string | null;
  /** 任务当前停在该等待点。 */
  waiting: boolean;
  /** 无法建立核对边界时的原因（例如抽取依赖了核对后的规则）。 */
  blocked_reason?: string | null;
}

/** 字段核对修改：动作、要写入的值，以及页面看到的字段版本。 */
export interface FieldReviewCorrection {
  field_key: string;
  material_key: string;
  decision: "MODIFY_FIELD" | "MARK_NOT_PRESENT";
  field_value?: string | null;
  business_note: string;
  basis_type?: "HUMAN_STATEMENT" | "DOCUMENT_EVIDENCE" | "UPLOAD_RECORD";
  /** 页面看到的字段事实版本；没有字段事实（漏抽）时为 null。 */
  expected_revision_no: number | null;
}

/** 只保存核对修改的结果；任务仍停在等待核对。 */
export interface FieldReviewSaveResult {
  task_no: string;
  input_revision: number;
  applied: Array<{
    field_key: string;
    material_key: string;
    review_no: string;
    fact_revision_no: number;
    previous_revision_no: number | null;
    before_value: string | null;
    after_value: string | null;
    status: string;
  }>;
  missing_field_count: number;
  missing_fields: FieldReviewMissing[];
}

/** 模型渠道故障：暂停原因，以及这次请求是否可能已经产生费用。 */
export interface ChannelFailure {
  code: string;
  message: string;
  result_unknown: boolean;
  /** 触发暂停的规则键；没有具体规则时为空。 */
  rule_key?: string | null;
}

/** 服务端对当前批次能否续跑、续跑多少的统一判断；页面只展示，不自行推导。 */
export interface ResumeState {
  execution_id: number | null;
  /** 不能续跑的原因；可续跑时为 null。 */
  blocked_reason: string | null;
  plan: ResumeScope | null;
  channel_failure: ChannelFailure | null;
}

export interface AuditNode {
  /** 节点实例码：用户配置的实例，同一能力类型可以有多个。 */
  node_code: string;
  /** 能力类型：决定这个节点由谁执行、属于哪个业务工作区。 */
  node_type?: string | null;
  name: string;
  sequence_no: number;
  enabled: boolean;
  status: string;
  failure_message: string | null;
  skip_reason: string | null;
  /** 可原地继续的执行批次；为 null 表示只能全部重新执行。 */
  resumable_execution_id?: number | null;
  /** 续跑判断与范围；任务还没有执行批次时为 null。 */
  resume?: ResumeState | null;
  /** 字段核对等待点状态；任务还没有执行批次时为 null。 */
  field_review?: FieldReviewState | null;
}
/**
 * 任务的展示结构：由后端按当前任务的场景配置返回。
 *
 * 页面按它通用渲染：只展示当前场景实际使用的节点与字段，
 * 展示范围来自任务冻结的配置，不按参考模板补充内容。
 */
export interface TaskDisplay {
  task_no: string;
  nodes: AuditNode[];
  fields: Array<{
    field_key: string;
    name: string;
    value_type: string;
    unit: string | null;
    currency: string | null;
    material_key: string;
  }>;
}

export interface AuditDocument {
  document_no: string;
  /** 材料键：材料位的稳定身份，不用中文名匹配材料。 */
  material_key?: string | null;
  material_name: string;
  required: boolean;
  status: string;
  filename: string | null;
  /** 当前上传时从 8018 目录选择的材料类型；没有当前上传时为空。 */
  declared_material_type?: string | null;
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
  /**
   * 当前上传的四项处理产物状态；尚未产生某项时该键不存在。
   * 由任务详情一次返回，用于显示识别进度与定位失败项。
   */
  artifacts?: Record<string, MaterialArtifactStatus>;
}

export interface MaterialArtifactStatus {
  status: string;
  artifact_no: string | null;
  message: string;
  deadline_at: string | null;
  job_id?: string | null;
  /** OCR 供应商侧的作业状态（running/pending/done），仅 OCR 返回。 */
  provider_state?: string | null;
  extract_progress?: { totalPages?: number; extractedPages?: number; [key: string]: unknown } | null;
  progress_updated_at?: string | null;
}

export interface AuditField {
  /** 规则库中的字段业务编号。 */
  source_ref?: string | null;
  name: string;
  material_name: string;
  status: string;
  raw_value: string | null;
  normalized_value: JsonValue;
  /** 随业务结果返回的引用原文，不单独查询证据记录。 */
  evidence: Array<{
    document_name: string | null;
    material_name?: string | null;
    document_upload_id: number;
    artifact_id: number | null;
    /** 目标值所在单元格的真实原文。 */
    quote_text: string;
    /** 目标值对应的科目/行标签、列/期间表头、期间原文；各自独立校验。 */
    quote_label?: string | null;
    quote_header?: string | null;
    quote_period?: string | null;
    quote_statement_scope?: string | null;
    quote_entity_scope?: string | null;
    quote_unit?: string | null;
    /** OCR 产物内页码；只表示产物位置，不冒充原 PDF 页码。 */
    artifact_page?: number | null;
    field_path?: string | null;
  }>;
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
  /** 配置声明的取数期间与报表口径；实际识别值另见 actual_*。 */
  period_scope?: string | null;
  statement_scope?: string | null;
  /** 正文实际识别到的期间与单位；用于解释取数口径。 */
  actual_period_scope?: string | null;
  actual_statement_scope?: string | null;
  actual_entity_scope?: string | null;
  actual_unit?: string | null;
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
  /** 规则业务编号；系统生成的抽取/报告规则为 SYS- 前缀。 */
  rule_code?: string | null;
  operator_label?: string | null;
  requirement?: string | null;
  check_level?: string | null;
  comparison_label?: string | null;
  threshold?: string | number | null;
  threshold_unit?: string | null;
}

export interface AuditRule {
  rule_key?: string;
  /** 冻结配置中的真实业务编号；抽取、报告规则继承各自来源编号。 */
  source_ref?: string | null;
  name: string;
  kind?: string | null;
  /** 规则执行方式：材料检查/字段比较/跨范围比较/汇总/风险推导等，由场景配置冻结。 */
  execution_mode?: string | null;
  /** 由后端按冻结配置中的风险角色或风险算子识别，与执行方式独立。 */
  is_risk_rule?: boolean;
  /** 规则来源工作表；页面按它分组，不同场景的 Sheet 名不同。 */
  source_sheet?: string | null;
  material_names?: string[];
  status: string;
  message: string;
  result: Record<string, JsonValue> | null;
  turn_count?: number;
  configured_requirement?: string | null;
  /** 解析阶段记录的未绑定输入；只解释配置完整性，不改变执行状态。 */
  input_gaps?: Array<{
    parameter_code?: string | null;
    missing_input?: string | null;
    reason?: string | null;
    original_requirement?: string | null;
  }>;
  evidence: AuditField["evidence"];
}

export interface AuditTask {
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
  /** 任务冻结的技术容器、来源工作表与字段。 */
  display?: TaskDisplay | null;
  documents: AuditDocument[];
  fields: AuditField[];
  rules: AuditRule[];
  /** 任务输入修订号：首次补传、替换材料、人工有效值变化都会递增。 */
  input_revision: number;
  /** 当前失败节点是否具备可核对的等待点恢复条件。 */
  /** 任务冻结的生效规则范围。 */
  scope?: TaskScope | null;
  latest_operation: OperationAccepted | null;
  /** 任务备注（可编辑基本资料）。 */
  remark?: string | null;
  scenario_code?: string;
  /** 任务创建时冻结的方案身份。 */
  plan?: TaskPlanInfo | null;
}

export interface ExecuteNodeResponse {
  task_no: string;
  node_name: string;
  execution_status: string;
  task_status: string;
  next_node_name: string | null;
  result_summary: Record<string, JsonValue>;
}


export interface RiskList {
  task_no: string;
  risk_configured: boolean;
  risk_executed: boolean;
  risk_conclusion_available: boolean;
  /** 只统计 ABNORMAL；不能用结果总数冒充命中数。 */
  hit_count: number;
  status_counts: Record<string, number>;
  items: Array<{
    /** 风险规则的业务编号，与规则结果中的编号一致。 */
    rule_code: string | null;
    name: string;
    status: string;
    message: string;
    risk_level?: string | null;
    source_sheet?: string | null;
    details: Record<string, JsonValue> | null;
    evidence: AuditField["evidence"];
  }>;
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

export interface FieldCorrectionResponse {
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

export interface DocumentRemoveResult {
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
