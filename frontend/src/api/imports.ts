import type {
  ImportJob,
  ImportJobSummary,
  JsonValue,
  OperationAccepted,
  Paged,
  PrecheckResult,
  ProposalRule,
  ProposalSheet,
  RuleComparison,
  ScenarioVersion,
} from "./contracts";
import { apiFileUrl, request, type OperationView } from "./http";
import { toQuery } from "./query";

/** 模板发布目标：新建模板，或在已有模板身份下生成新版本。 */
export type TemplateMode = "new" | "update";

/** 一条草稿裁决：问题作答、候选原地修正、候选排除/恢复共用同一接口。 */
export interface ImportDecision {
  target_type: "ISSUE" | "MATERIAL" | "FIELD" | "RULE" | "REPORT_SECTION";
  target_ref: string;
  action: "RESOLVE" | "EXCLUDE" | "ACCEPT";
  /** RESOLVE 时为属性字典（问题作答为 {selected: ...}）；EXCLUDE/ACCEPT 用空对象。 */
  value: Record<string, JsonValue>;
}

export interface PublishPayload {
  scenario_name: string;
  /** 更新已有模板时必填；新建模板时由后端生成，页面不要求业务用户输入编码。 */
  scenario_code?: string;
  template_mode: TemplateMode;
  /**
   * 已逐条确认「这条检查本次确实无法执行」的规则键。
   *
   * 能力缺口默认阻断发布；要把发布继续下去，必须把缺口规则的 rule_key 逐条列出来，
   * 表示使用者已经看到并接受「该检查运行时会明确报不可判定、不会给出结论」。
   * 它不是「忽略全部缺口」的开关，也不是默认值：未列出的缺口仍然阻断。
   */
  acknowledged_capability_gaps?: string[];
}

/**
 * 规则预览视图。contracts.ts 的 RuleComparison 需要补齐后端已返回的
 * requirement（审核要求原文）、applicability / applicability_label（适用条件）、
 * source_texts（规则所在行的原始单元格文本）、方向/阈值/单位与支持状态。
 * 在补齐之前先用这里的扩展类型，字段名与后端 rule_comparison_view 一致。
 */
export type RuleComparisonView = RuleComparison & {
  requirement?: string | null;
  applicability?: { kind: string; material_keys: string[] } | null;
  applicability_label?: string | null;
  /** 规则所在行的原始单元格文本（列名 → 原文），供预览逐项对照 Excel。 */
  source_texts?: Record<string, string> | null;
  direction_label?: string | null;
  threshold?: JsonValue;
  /** 支持状态：SUPPORTED / UNSUPPORTED / UNIT_MISMATCH；不能把不支持显示成可执行。 */
  support_status?: string | null;
  support_message?: string | null;
  input_units?: Array<string | null>;
};

export type ProposalRuleView = Omit<ProposalRule, "comparison"> & { comparison: RuleComparisonView };

export type ProposalSheetView = Omit<ProposalSheet, "rules"> & { rules: ProposalRuleView[] };

/**
 * 作业视图。契约文件不在这里改写，只补齐后端已返回、而 contracts.ts 尚未声明的字段：
 * - latest_operation：页面刷新后要凭它继续查询该作业正在进行/刚结束的后台操作；
 * - profile_display_name 由 ImportJob 声明，页面优先使用服务端给出的中文格式名。
 */
export type ImportJobView = Omit<ImportJob, "proposals"> & {
  proposals: { sheets: ProposalSheetView[] } | null;
  latest_operation?: OperationView | null;
};

export function listImportJobs(): Promise<ImportJobSummary[]> {
  return request<ImportJobSummary[]>("/import-jobs");
}

/** 导入作业的管理分页列表（文件名、状态、时间筛选；total 来自服务端）。 */
export interface ImportJobRow {
  job_no: string;
  source_filename: string;
  status: string;
  profile_version: string;
  draft_revision: number;
  target_mode: "new" | "update" | null;
  target_name: string | null;
  pending_issue_count: number;
  /**
   * 该作业未结束（待执行 / 执行中）的后台操作数。
   * 转换期间作业状态不变，所以「是否执行中」只能看这个字段，不能看 status。
   */
  active_operation_count: number;
  failure_code: string | null;
  failure_message: string | null;
  created_at: string;
  updated_at: string;
}

export interface ImportJobQuery {
  filename?: string;
  status?: string;
  created_from?: string;
  created_to?: string;
  page: number;
  page_size: number;
}

export function listImportJobsPage(query: ImportJobQuery): Promise<Paged<ImportJobRow>> {
  return request<Paged<ImportJobRow>>(`/import-jobs/page${toQuery({ ...query })}`);
}

export function importJobsExportUrl(query: Omit<ImportJobQuery, "page" | "page_size">): string {
  return apiFileUrl(`/import-jobs/export${toQuery({ ...query })}`);
}

/** 删除仅允许未发布的草稿作业；已发布作业由服务端拒绝并说明原因。 */
export function deleteImportJob(jobNo: string): Promise<void> {
  return request<void>(`/import-jobs/${encodeURIComponent(jobNo)}`, { method: "DELETE" });
}

/**
 * 中止该作业尚未结束的后台转换操作：只释放活跃占用，让作业回到可重试 / 可删除的状态。
 * 不是删除，也不改写作业状态与草稿数据；没有活跃操作时服务端返回 409 OPERATION_NOT_ACTIVE。
 */
export function abortImportJob(jobNo: string): Promise<OperationView> {
  return request<OperationView>(`/import-jobs/${encodeURIComponent(jobNo)}/abort`, { method: "POST" });
}

export type RulebookTemplateExample = "blank" | "single_contract" | "contract_and_invoice";

/**
 * 模板下载地址。示例 key 由后端 /rulebook-templates/contract-review/examples 给出，
 * 不限于当前已知的三种取值——新格式可以声明自己的示例，前端不应把它限制成写死的联合类型。
 */
export function rulebookTemplateUrl(example: string): string {
  return apiFileUrl(`/rulebook-templates/contract-review/download?example=${encodeURIComponent(example)}`);
}

/**
 * 当前格式版本真实可下载的模板示例。
 * 页面必须据此渲染下载入口：节点版没有真实示例内容，只有 blank；
 * 在前端写死示例 A/B 会让用户点到必然失败的链接。
 */
export interface RulebookTemplateExampleItem {
  key: string;
  label: string;
  available: boolean;
  description: string;
}

export async function listRulebookTemplateExamples(): Promise<RulebookTemplateExampleItem[]> {
  const payload = await request<{ items: RulebookTemplateExampleItem[] }>(
    "/rulebook-templates/contract-review/examples",
  );
  return payload.items;
}

/** 「更新已有模板」下拉用：未删除的场景及最新版本号（不含已发布状态字段）。 */
export interface TemplateOption {
  scenario_code: string;
  scenario_name: string;
  category: string | null;
  enabled: boolean;
  latest_version_no: number;
}

export function listTemplates(): Promise<TemplateOption[]> {
  return request<{ request_id: string; items: TemplateOption[] }>("/scenarios/options").then((payload) => payload.items);
}

export function createImportJob(file: File): Promise<ImportJobView> {
  const formData = new FormData();
  formData.append("file", file);
  return request<ImportJobView>("/import-jobs", { method: "POST", body: formData });
}

export function getImportJob(jobNo: string): Promise<ImportJobView> {
  return request<ImportJobView>(`/import-jobs/${encodeURIComponent(jobNo)}`);
}

export function updateSheetBindings(
  jobNo: string,
  bindings: Array<{ sheet_key: string; role: string | null; header_row: number | null; excluded: boolean }>,
): Promise<ImportJobView> {
  return request<ImportJobView>(`/import-jobs/${encodeURIComponent(jobNo)}/sheet-bindings`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ bindings }),
  });
}

/**
 * 触发规则转换。长处理只受理不等待：返回 202 语义的受理状态，页面再用 operation_no 查询进度。
 * idempotency_key 与作业内容绑定，重复提交同一动作不会重复执行。
 */
export function startImportConversion(jobNo: string, idempotencyKey: string): Promise<OperationAccepted> {
  return request<OperationAccepted>(`/import-jobs/${encodeURIComponent(jobNo)}/retry`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ idempotency_key: idempotencyKey }),
  });
}

/**
 * 提交完整裁决集合。expected_revision 必须带页面看到的 job.draft_revision：
 * 任一草稿改动（绑定、排除、裁决、候选修正）都会递增它，服务端据此拒绝过期提交。
 */
export function saveImportDecisions(
  jobNo: string,
  decisions: ImportDecision[],
  expectedRevision: number,
): Promise<ImportJobView> {
  return request<ImportJobView>(`/import-jobs/${encodeURIComponent(jobNo)}/decisions`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ decisions, expected_revision: expectedRevision }),
  });
}

/**
 * 暂存问题答案（不改变候选与问题状态）。用户刷新后从 job.issues[].decision 恢复。
 */
export function saveDecisionDraft(jobNo: string, answers: Record<string, JsonValue>): Promise<ImportJobView> {
  return request<ImportJobView>(`/import-jobs/${encodeURIComponent(jobNo)}/decision-draft`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ answers }),
  });
}

export function precheckImport(jobNo: string, payload: PublishPayload): Promise<PrecheckResult> {
  return request<PrecheckResult>(`/import-jobs/${encodeURIComponent(jobNo)}/precheck`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}

/** 发布保持同步：返回已发布（或同一作业已发布的同一个）模板版本。 */
export function publishImport(jobNo: string, payload: PublishPayload): Promise<ScenarioVersion> {
  return request<ScenarioVersion>(`/import-jobs/${encodeURIComponent(jobNo)}/publish`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}
