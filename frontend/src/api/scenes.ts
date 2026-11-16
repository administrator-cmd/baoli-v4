import type {
  DraftFieldItem,
  DraftMaterialItem,
  DraftRuleItem,
  DraftSectionItem,
  JsonValue,
  Paged,
  ScenarioDetail,
  ScenarioDraftView,
  ScenarioConfigurationView,
  ScenarioListItem,
  ScenarioPublishResult,
} from "./contracts";
import { request } from "./http";
import { toQuery } from "./query";

/** 场景列表查询：参数与后端筛选一一对应；导出使用同一套查询参数。 */
export interface ScenarioQuery {
  name?: string;
  category?: string;
  enabled?: boolean;
  published_from?: string;
  published_to?: string;
  page: number;
  page_size: number;
}

export interface ScenarioOption {
  scenario_code: string;
  scenario_name: string;
  category: string | null;
  enabled: boolean;
  latest_version_no: number;
  /** 该场景当前能否被业务用户用于创建方案（已启用且已有已发布版本）。 */
  available?: boolean;
}

/** 场景分页列表（后端筛选 / 排序 / 分页）。 */
export function listScenes(query: ScenarioQuery): Promise<Paged<ScenarioListItem>> {
  return request<Paged<ScenarioListItem>>(`/scenarios${toQuery({ ...query })}`);
}

/**
 * 业务用户可用的场景下拉：只含已启用且已有已发布版本的场景。
 *
 * 方案与任务的场景选择器用它：草稿、未发布、停用或已删除的场景不出现。
 */
export function listSceneOptions(): Promise<{ items: ScenarioOption[] }> {
  return request<{ items: ScenarioOption[] }>("/scenarios/options");
}

/**
 * 管理员用的场景下拉：未删除的全部场景及其实时状态。
 *
 * 场景管理页与导入页在发布前就要选中场景，因此不能使用只返回已发布场景的业务接口。
 */
export function listAdminSceneOptions(): Promise<{ items: ScenarioOption[] }> {
  return request<{ items: ScenarioOption[] }>("/scenarios/admin-options");
}

export interface ScenarioPayload {
  scenario_name?: string;
  category?: string | null;
  description?: string | null;
  enabled?: boolean;
}

/** 场景详情：当前发布版本与配置概览。 */
export function getScene(scenarioCode: string): Promise<ScenarioDetail> {
  return request<ScenarioDetail>(`/scenarios/${encodeURIComponent(scenarioCode)}`);
}

/** 编辑场景基本资料（名称、业务类型、说明）。 */
export function updateScene(scenarioCode: string, payload: ScenarioPayload): Promise<ScenarioDetail> {
  return request<ScenarioDetail>(`/scenarios/${encodeURIComponent(scenarioCode)}`, {
    method: "PUT",
    data: payload,
  });
}

/** 启用 / 停用场景；已停用的场景不能用于创建任务。 */
export function setSceneEnabled(scenarioCode: string, enabled: boolean): Promise<ScenarioDetail> {
  return request<ScenarioDetail>(`/scenarios/${encodeURIComponent(scenarioCode)}/enabled`, {
    method: "PUT",
    data: { enabled },
  });
}

/** 实际删除场景及草稿，不能恢复。 */
export function deleteScene(scenarioCode: string): Promise<void> {
  return request<void>(`/scenarios/${encodeURIComponent(scenarioCode)}`, { method: "DELETE" });
}

/** 新建场景。此时还没有规则库，也没有已发布版本。 */
export function createScene(payload: {
  scenario_name: string;
  category?: string | null;
  description?: string | null;
  enabled?: boolean;
}): Promise<ScenarioDetail> {
  return request<ScenarioDetail>("/scenarios", {
    method: "POST",
    data: payload,
  });
}

/* 配置修改只保存草稿并返回发布就绪状态；场景版本必须由管理员显式发布。 */

/** 查看当前配置不创建草稿；有草稿时读取草稿，否则读取发布内容。 */
export function getSceneConfiguration(scenarioCode: string, view?: "DRAFT" | "PUBLISHED"): Promise<ScenarioConfigurationView> {
  return request<ScenarioConfigurationView>(`/scenarios/${encodeURIComponent(scenarioCode)}/configuration${toQuery({ view })}`);
}

/** 材料、字段、审核规则与报告写入返回完整草稿视图；计算写入返回已提交的草稿修订号。 */
export interface DraftMaterialPayload {
  name: string;
  required: boolean;
  description: string;
  /** 业务编号（Excel 的「材料编号」列）：导出规则库时写回该列。 */
  source_ref?: string | null;
}

/** 在草稿中新增材料位。 */
export function createDraftMaterial(scenarioCode: string, payload: DraftMaterialPayload): Promise<ScenarioDraftView> {
  return request<ScenarioDraftView>(`/scenarios/${encodeURIComponent(scenarioCode)}/draft/materials`, {
    method: "POST",
    data: payload,
  });
}

/** 修改草稿中的材料位（只覆盖提交的字段）。 */
export function updateDraftMaterial(scenarioCode: string, materialKey: string, payload: Partial<DraftMaterialPayload>): Promise<ScenarioDraftView> {
  return request<ScenarioDraftView>(
    `/scenarios/${encodeURIComponent(scenarioCode)}/draft/materials/${encodeURIComponent(materialKey)}`,
    { method: "PUT", data: payload },
  );
}

/** 删除草稿中的材料位；仍被字段或规则引用时后端返回 409。 */
export function deleteDraftMaterial(scenarioCode: string, materialKey: string): Promise<ScenarioDraftView> {
  return request<ScenarioDraftView>(
    `/scenarios/${encodeURIComponent(scenarioCode)}/draft/materials/${encodeURIComponent(materialKey)}`,
    { method: "DELETE" },
  );
}

export interface DraftFieldPayload {
  material_key: string;
  source_ref?: string | null;
  name: string;
  value_type: string;
  extraction_strategy?: "RAW_TEXT" | "DETERMINISTIC" | "STRUCTURED";
  structure_schema?: Record<string, unknown> | null;
  unit: string | null;
  /** 币种：留空表示未声明，不表示人民币。 */
  currency?: string | null;
  /** 产物来源：当前仅 "SEAL"（印章识别）；留空表示常规字段抽取。 */
  artifact_source?: string | null;
  /**
   * 取数期间与报表口径，不涉及相应口径时声明 NOT_APPLICABLE（不适用）。
   * 计算参数按字段实际声明定位事实。
   */
  period_scope?: string;
  statement_scope?: string;
  entity_scope?: string;
  required: boolean;
  instruction: string;
}

/** 在草稿中新增字段。 */
export function createDraftField(scenarioCode: string, payload: DraftFieldPayload): Promise<ScenarioDraftView> {
  return request<ScenarioDraftView>(`/scenarios/${encodeURIComponent(scenarioCode)}/draft/fields`, {
    method: "POST",
    data: payload,
  });
}

/** 修改草稿中的字段（只覆盖提交的字段）。 */
export function updateDraftField(scenarioCode: string, fieldKey: string, payload: Partial<DraftFieldPayload>): Promise<ScenarioDraftView> {
  return request<ScenarioDraftView>(
    `/scenarios/${encodeURIComponent(scenarioCode)}/draft/fields/${encodeURIComponent(fieldKey)}`,
    { method: "PUT", data: payload },
  );
}

/** 删除草稿中的字段；仍被规则或报告章节引用时后端返回 409。 */
export function deleteDraftField(scenarioCode: string, fieldKey: string): Promise<ScenarioDraftView> {
  return request<ScenarioDraftView>(
    `/scenarios/${encodeURIComponent(scenarioCode)}/draft/fields/${encodeURIComponent(fieldKey)}`,
    { method: "DELETE" },
  );
}

/** 规则的业务定义（与后端 DraftRuleRequest 对应）。 */
export interface DraftRulePayload {
  name: string;
  source_ref: string | null;
  node: string;
  operator: string;
  execution_mode: string;
  rule_role: "CHECK" | "RISK";
  enabled: boolean;
  inputs: Array<{ field_key: string; material_key: string }>;
  outputs: string[];
  materials: string[];
  parameters: Record<string, JsonValue>;
  requirement: string;
  applicability: { kind: string };
  source_rule_key: string | null;
  source_rule_keys: string[];
  source_rule_codes: string[];
  check_level: string | null;
  risk_condition: string | null;
}

/** 在草稿中新增规则。 */
export function createDraftRule(scenarioCode: string, payload: DraftRulePayload): Promise<ScenarioDraftView> {
  return request<ScenarioDraftView>(`/scenarios/${encodeURIComponent(scenarioCode)}/draft/rules`, {
    method: "POST",
    data: payload,
  });
}

/** 整体替换草稿中这条规则的定义。 */
export function updateDraftRule(scenarioCode: string, ruleKey: string, payload: DraftRulePayload): Promise<ScenarioDraftView> {
  return request<ScenarioDraftView>(
    `/scenarios/${encodeURIComponent(scenarioCode)}/draft/rules/${encodeURIComponent(ruleKey)}`,
    { method: "PUT", data: payload },
  );
}

/** 删除草稿中的规则。 */
export function deleteDraftRule(scenarioCode: string, ruleKey: string): Promise<ScenarioDraftView> {
  return request<ScenarioDraftView>(
    `/scenarios/${encodeURIComponent(scenarioCode)}/draft/rules/${encodeURIComponent(ruleKey)}`,
    { method: "DELETE" },
  );
}

export interface DraftCalculationPayload {
  rule_code: string;
  rule_name: string;
  result_code: string;
  source_id: string | null;
  formula: string;
  requirement: string;
  result_type: string;
  unit: string | null;
  currency: string | null;
  period_policy: string | null;
  statement_scope: string | null;
  status: "CLEAR" | "PENDING" | "DISABLED";
  parameters: Array<{
    parameter_code: string;
    source_type: string;
    source_code: string | null;
    constant_value?: JsonValue;
    [key: string]: unknown;
  }>;
  depends_on_result_codes: string[];
  business_context: Record<string, JsonValue>;
  remark: string;
}

/** 新增或编辑计算只更新场景草稿，发布后供新方案使用。 */
export interface DraftCalculationMutation {
  scenario_code: string;
  revision_no: number;
}

export function createDraftCalculation(scenarioCode: string, payload: DraftCalculationPayload): Promise<DraftCalculationMutation> {
  return request<DraftCalculationMutation>(`/scenarios/${encodeURIComponent(scenarioCode)}/draft/calculations`, {
    method: "POST", data: payload,
  });
}

export function updateDraftCalculation(scenarioCode: string, ruleCode: string, payload: DraftCalculationPayload): Promise<DraftCalculationMutation> {
  return request<DraftCalculationMutation>(`/scenarios/${encodeURIComponent(scenarioCode)}/draft/calculations/${encodeURIComponent(ruleCode)}`, {
    method: "PUT", data: payload,
  });
}

export function deleteDraftCalculation(scenarioCode: string, ruleCode: string): Promise<DraftCalculationMutation> {
  return request<DraftCalculationMutation>(`/scenarios/${encodeURIComponent(scenarioCode)}/draft/calculations/${encodeURIComponent(ruleCode)}`, {
    method: "DELETE",
  });
}

export interface DraftSectionPayload {
  title: string;
  content_instruction: string;
  data_refs: string[];
  /**
   * 场景报告分组属性。未声明报告编号时可留空：
   * 留空表示该章节没有报告编号，归为一份报告，按章节要求生成正文。
   */
  report_code?: string | null;
  report_name?: string | null;
  material_keys?: string[];
  formats?: string[];
  section_order?: number | null;
  content_type?: string | null;
  result_sort?: string | null;
  risk_level_order?: string[];
  config_status?: string | null;
}

/** 在草稿中新增报告章节。 */
export function createDraftSection(scenarioCode: string, payload: DraftSectionPayload): Promise<ScenarioDraftView> {
  return request<ScenarioDraftView>(`/scenarios/${encodeURIComponent(scenarioCode)}/draft/sections`, {
    method: "POST",
    data: payload,
  });
}

/** 修改草稿中的报告章节。 */
export function updateDraftSection(scenarioCode: string, sectionKey: string, payload: Partial<DraftSectionPayload>): Promise<ScenarioDraftView> {
  return request<ScenarioDraftView>(
    `/scenarios/${encodeURIComponent(scenarioCode)}/draft/sections/${encodeURIComponent(sectionKey)}`,
    { method: "PUT", data: payload },
  );
}

/** 删除草稿中的报告章节。 */
export function deleteDraftSection(scenarioCode: string, sectionKey: string): Promise<ScenarioDraftView> {
  return request<ScenarioDraftView>(
    `/scenarios/${encodeURIComponent(scenarioCode)}/draft/sections/${encodeURIComponent(sectionKey)}`,
    { method: "DELETE" },
  );
}

/** 发布草稿：必须携带页面看到的草稿修订号；内容与最新版本一致时返回原版本。 */
export function publishSceneDraft(
  scenarioCode: string,
  payload: {
    expected_revision: number;
    change_summary?: string | null;
  },
): Promise<ScenarioPublishResult> {
  return request<ScenarioPublishResult>(`/scenarios/${encodeURIComponent(scenarioCode)}/draft/publish`, {
    method: "POST",
    data: payload,
  });
}

/**
 * 页面只维护当前草稿，不提供版本列表、版本对比或指定版本回填入口；
 * 草稿发布后生成不可变的执行快照。
 */

export type { DraftFieldItem, DraftMaterialItem, DraftRuleItem, DraftSectionItem };
