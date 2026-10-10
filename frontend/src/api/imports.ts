import type {
  ImportJob,
  ImportJobSummary,
  JsonValue,
  OperationAccepted,
  Paged,
} from "./contracts";
import { request, type OperationView } from "./http";
import { toQuery } from "./query";

/** 一条草稿裁决：问题作答、候选原地修正、候选排除/恢复共用同一接口。 */
export interface ImportDecision {
  target_type: "ISSUE" | "MATERIAL" | "FIELD" | "RULE" | "REPORT_SECTION" | "CALC_RULE";
  target_ref: string;
  action: "RESOLVE" | "EXCLUDE" | "ACCEPT";
  /** RESOLVE 时为属性字典（问题作答为 {selected: ...}）；EXCLUDE/ACCEPT 用空对象。 */
  value: Record<string, JsonValue>;
}

/** 工作台作业选择列表（完整摘要数组）。 */
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

/** 「更新已有模板」下拉用：未删除的场景及最新版本号（不含已发布状态字段）。 */
export interface TemplateOption {
  scenario_code: string;
  scenario_name: string;
  category: string | null;
  enabled: boolean;
  latest_version_no: number;
}

/**
 * 管理员用的场景下拉：未删除的全部场景与最新版本号。
 *
 * 导入页在场景发布之前就要选中它（先建场景、再上传规则库），
 * 因此走 `/scenarios/admin-options`；业务用户用的 `/scenarios/options`
 * 只返回已发布且启用的场景，选不到刚建好的场景。
 */
export function listTemplates(): Promise<TemplateOption[]> {
  return request<{ request_id: string; items: TemplateOption[] }>("/scenarios/admin-options").then((payload) => payload.items);
}

/**
 * 在指定场景下上传规则库 Excel，读取为工作簿 IR；此时还没有规则候选。
 *
 * `scenarioCode` 必填：上传流程以场景为起点（用户先建场景、再在该场景下上传），
 * 场景不由 Excel 表头推断。管理员选择参与解析的工作表，模型生成的字段、规则
 * 和计算各自引用场景实际节点，不按工作表绑定一个节点。
 */
export function createImportJob(file: File, scenarioCode: string): Promise<ImportJob> {
  const formData = new FormData();
  formData.append("file", file);
  formData.append("scenario_code", scenarioCode);
  return request<ImportJob>("/import-jobs", { method: "POST", body: formData });
}

/** 导入作业详情：工作表、问题、候选与草稿修订号。 */
export function getImportJob(jobNo: string): Promise<ImportJob> {
  return request<ImportJob>(`/import-jobs/${encodeURIComponent(jobNo)}`);
}

/**
 * 保存参与解析的工作表。
 * 表内业务分别引用实际节点，已排除的工作表不进入转换范围。
 */
export function updateSheetBindings(
  jobNo: string,
  bindings: Array<{
    sheet_key: string;
    excluded: boolean;
  }>,
): Promise<ImportJob> {
  return request<ImportJob>(`/import-jobs/${encodeURIComponent(jobNo)}/sheet-bindings`, {
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
): Promise<ImportJob> {
  return request<ImportJob>(`/import-jobs/${encodeURIComponent(jobNo)}/decisions`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ decisions, expected_revision: expectedRevision }),
  });
}

/**
 * 暂存问题答案（不改变候选与问题状态）。用户刷新后从 job.issues[].decision 恢复。
 */
export function saveDecisionDraft(jobNo: string, answers: Record<string, JsonValue>): Promise<ImportJob> {
  return request<ImportJob>(`/import-jobs/${encodeURIComponent(jobNo)}/decision-draft`, {
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
): Promise<ImportJob> {
  return request<ImportJob>(`/import-jobs/${encodeURIComponent(jobNo)}/apply-draft`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      expected_job_revision: expectedJobRevision,
      expected_scene_revision: expectedSceneRevision,
    }),
  });
}
