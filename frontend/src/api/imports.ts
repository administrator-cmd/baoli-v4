import type {
  ImportJob,
  ImportJobSummary,
  JsonValue,
  OperationAccepted,
  Paged,
  ProposalCalculation,
  ProposalRule,
  ProposalSheet,
  RuleComparison,
} from "./contracts";
import { apiFileUrl, request, type OperationView } from "./http";
import { toQuery } from "./query";

/** 一条草稿裁决：问题作答、候选原地修正、候选排除/恢复共用同一接口。 */
export interface ImportDecision {
  target_type: "ISSUE" | "MATERIAL" | "FIELD" | "RULE" | "REPORT_SECTION";
  target_ref: string;
  action: "RESOLVE" | "EXCLUDE" | "ACCEPT";
  /** RESOLVE 时为属性字典（问题作答为 {selected: ...}）；EXCLUDE/ACCEPT 用空对象。 */
  value: Record<string, JsonValue>;
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
 * - profile_display_name 由 ImportJob 声明，页面优先使用服务端给出的中文格式名；
 * - profile：作业所属场景 + 统一模板协议摘要（Sheet 数量、名称与可绑定的节点类型）。
 *   页面据此显示真实的表数量与用途说明，不再硬编码「7 类工作表」。
 */
export type ImportJobView = Omit<ImportJob, "proposals"> & {
  proposals: { sheets: ProposalSheetView[]; calculations: ProposalCalculation[] } | null;
  latest_operation?: OperationView | null;
  profile?: ProfileSummary | null;
};

/** 导入作业摘要列表（完整数组，兼容旧调用）。 */
export function listImportJobs(): Promise<ImportJobSummary[]> {
  return request<ImportJobSummary[]>("/import-jobs");
}

/** 导入作业的管理分页列表（文件名、状态、时间筛选；total 来自服务端）。 */
export interface ImportJobRow {
  job_no: string;
  source_filename: string;
  status: string;
  profile_version: string;
  /** 作业所属场景（上传时指定）；历史作业为 null，如实显示「未记录场景」。 */
  scenario_code: string | null;
  scenario_name: string | null;
  draft_revision: number;
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
  scenario_code?: string;
  filename?: string;
  status?: string;
  created_from?: string;
  created_to?: string;
  page: number;
  page_size: number;
}

/** 导入作业分页列表（后端筛选与 total）。 */
export function listImportJobsPage(query: ImportJobQuery): Promise<Paged<ImportJobRow>> {
  return request<Paged<ImportJobRow>>(`/import-jobs/page${toQuery({ ...query })}`);
}

/** 导入作业列表导出地址；与当前筛选同一作用域。 */
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

/**
 * 统一模板协议摘要：系统支持的全部 Sheet 与可绑定的节点类型。
 *
 * 统一协议是内部执行格式；场景 Schema 决定其 Excel 的 Sheet 展示名与列别名。
 */
export interface ProfileSheet {
  sheet_role: string;
  /** 编号前缀（例如 RSK-）：跨表引用靠它判断指向。 */
  code_prefix?: string;
  display_name: string;
  /** 这张 Sheet 可以绑定的节点类型（绑定由用户确认，系统只给建议）。 */
  node_types: string[];
  column_count: number;
}

export interface ProfileSummary {
  profile_version: string | null;
  display_name: string | null;
  /** 业务场景冻结的 Schema 身份；与统一执行协议版本分开。 */
  scenario_schema_key?: string | null;
  scenario_schema_version?: string | null;
  /** 内部统一协议版本；导入作业顶层 schema_version 是业务场景版本。 */
  schema_version: string | null;
  /** 模板说明页名称：模板生成、导出与导入读同一声明。 */
  instruction_sheet?: string;
  sheet_count: number;
  node_types: Array<{ node_type: string; display_name: string; depends_on_types: string[] }>;
  sheets: ProfileSheet[];
  /** 作业所属场景（上传时指定）；历史作业为 null。 */
  scenario_code?: string | null;
  scenario_name?: string | null;
}

/**
 * 规则库模板下载地址。
 *
 * 不传 selection 时只带场景编码：已有确认绑定的场景由服务端按绑定导出。
 * 还没有确认绑定时，必须由用户勾选本次要下载的工作表；该选择不写场景。
 * 下载按该场景冻结的 Schema 生成，勾选哪些表就下载哪些表。
 */
export function unifiedTemplateUrl(
  scenarioCode: string,
  selection?: { sheetRoles: string[] },
): string {
  const params = new URLSearchParams();
  params.set("scenario_code", scenarioCode);
  if (selection) {
    for (const role of selection.sheetRoles) {
      params.append("sheet_roles", role);
    }
  }
  return apiFileUrl(`/rulebook-templates/download?${params.toString()}`);
}

/** 统一模板协议声明的 Sheet 与列协议（页面据此渲染 Sheet 绑定步骤）。 */
export function getProfileSheets(scenarioCode?: string): Promise<{
  request_id: string;
  profile_version: string | null;
  display_name: string | null;
  scenario_schema_key?: string | null;
  scenario_schema_version?: string | null;
  schema_version: string | null;
  instruction_sheet: string;
  node_types: ProfileSummary["node_types"];
  sheets: Array<ProfileSheet & { aliases: string[]; header_signature: string[]; columns: Array<Record<string, unknown>> }>;
}> {
  const query = scenarioCode ? `?scenario_code=${encodeURIComponent(scenarioCode)}` : "";
  return request(`/rulebook-templates/sheets${query}`);
}



/** 「更新已有模板」下拉用：未删除的场景及最新版本号（不含已发布状态字段）。 */
export interface TemplateOption {
  scenario_code: string;
  scenario_name: string;
  category: string | null;
  enabled: boolean;
  latest_version_no: number;
}

/**
 * **管理员**用的场景下拉：未删除的全部场景与最新版本号。
 *
 * 导入页在场景**发布之前**就要选中它（先建场景、再上传规则库），
 * 因此走 `/scenarios/admin-options`；业务用户用的 `/scenarios/options`
 * 只返回已发布且启用的场景，选不到刚建好的场景。
 */
export function listTemplates(): Promise<TemplateOption[]> {
  return request<{ request_id: string; items: TemplateOption[] }>("/scenarios/admin-options").then((payload) => payload.items);
}

/** 上传规则库 Excel 并读取为工作簿 IR；此时还没有规则候选。 */
/**
 * 在**指定场景**下上传规则库 Excel，读取为工作簿 IR；此时还没有规则候选。
 *
 * `scenarioCode` 必填：上传流程以场景为起点（用户先建场景、再在该场景下上传），
 * 场景不由 Excel 表头推断。此时也没有绑定关系——Sheet 与业务节点的对应关系
 * 由用户在绑定步骤确认。
 */
export function createImportJob(file: File, scenarioCode: string): Promise<ImportJobView> {
  const formData = new FormData();
  formData.append("file", file);
  formData.append("scenario_code", scenarioCode);
  return request<ImportJobView>("/import-jobs", { method: "POST", body: formData });
}

/** 导入作业详情：工作表、问题、候选与草稿修订号。 */
export function getImportJob(jobNo: string): Promise<ImportJobView> {
  return request<ImportJobView>(`/import-jobs/${encodeURIComponent(jobNo)}`);
}

/** 保存工作表用途与表头行绑定。 */
/**
 * 保存 Sheet → 节点实例的绑定。
 *
 * `node_code` 是用户在当前场景里建立的**节点实例**：一张 Sheet 只能绑定一个节点，
 * 一个节点可以绑定多张 Sheet。未绑定或已排除的 Sheet 不进入转换与执行范围。
 */
export function updateSheetBindings(
  jobNo: string,
  bindings: Array<{
    sheet_key: string;
    role: string | null;
    node_code: string | null;
    header_row: number | null;
    excluded: boolean;
  }>,
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

/**
 * 把已确认的导入结果写入作业所属场景的草稿。
 * 不发布场景版本。管理员之后在场景页核对并显式发布。
 */
export function applyImportToDraft(
  jobNo: string, expectedJobRevision: number, expectedSceneRevision: number,
): Promise<ImportJobView> {
  return request<ImportJobView>(`/import-jobs/${encodeURIComponent(jobNo)}/apply-draft`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      expected_job_revision: expectedJobRevision,
      expected_scene_revision: expectedSceneRevision,
    }),
  });
}
