import type {
  ImportJob,
  ImportJobSummary,
  ImportProposalChanges,
  ImportProposalGroup,
  OperationAccepted,
  Paged,
} from "./contracts";
import { apiFileUrl, request, type OperationView } from "./http";
import { toQuery } from "./query";

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
  /**
   * 该作业未结束（待执行 / 执行中）的后台操作数。
   * 转换期间作业状态不变，所以「是否执行中」只能看这个字段，不能看 status。
   */
  active_operation_count: number;
  failure_code: string | null;
  failure_message: string | null;
  /** 上传原件的字节数；历史作业如实为 null。 */
  source_size: number | null;
  /** 这份规则库发布出的场景版本号；未发布的草稿作业为 null。 */
  published_version_no: number | null;
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

/** 规则库原件下载地址：直接交给浏览器下载，不经前端读取内容。 */
export function importJobFileUrl(jobNo: string): string {
  return apiFileUrl(`/import-jobs/${encodeURIComponent(jobNo)}/file`);
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
 * 场景不由 Excel 表头推断。系统解析全部非空工作表，生成业务配置。
 */
export function createImportJob(file: File, scenarioCode: string): Promise<ImportJob> {
  const formData = new FormData();
  formData.append("file", file);
  formData.append("scenario_code", scenarioCode);
  return request<ImportJob>("/import-jobs", { method: "POST", body: formData });
}

/** 导入作业详情：解析配置与修订号。 */
export function getImportJob(jobNo: string): Promise<ImportJob> {
  return request<ImportJob>(`/import-jobs/${encodeURIComponent(jobNo)}`);
}

/** 保存一项解析配置；返回最新结果和修订号。 */
export function updateImportProposal(
  jobNo: string, group: ImportProposalGroup, candidateRef: string,
  expectedJobRevision: number, changes: ImportProposalChanges,
): Promise<ImportJob> {
  return request<ImportJob>(`/import-jobs/${encodeURIComponent(jobNo)}/proposals/${group}/${encodeURIComponent(candidateRef)}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ expected_job_revision: expectedJobRevision, changes }),
  });
}

/**
 * 触发规则转换。长处理只受理不等待：返回 202 语义的受理状态，页面再用 operation_no 查询进度。
 * idempotency_key 与作业内容绑定，重复提交同一动作不会重复执行。
 */
export function startImportConversion(
  jobNo: string,
  idempotencyKey: string,
  mode: "FULL" | "RESUME_REFINEMENT" = "FULL",
): Promise<OperationAccepted> {
  return request<OperationAccepted>(`/import-jobs/${encodeURIComponent(jobNo)}/retry`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ idempotency_key: idempotencyKey, mode }),
  });
}

/**
 * 把已确认的导入结果写入作业所属场景的草稿。
 * 不发布场景版本。管理员之后在场景页核对并显式发布。
 */
export function applyImportToDraft(
  jobNo: string, expectedJobRevision: number, expectedSceneRevision: number | null,
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
