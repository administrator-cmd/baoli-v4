import type {
  DraftFieldItem,
  DraftMaterialItem,
  DraftRuleItem,
  DraftSectionItem,
  JsonValue,
  Paged,
  ScenarioDetail,
  ScenarioDraftView,
  ScenarioListItem,
  ScenarioPublishResult,
} from "./contracts";
import { apiFileUrl, request } from "./http";
import { toQuery } from "./query";

/** 场景列表查询：参数与后端筛选一一对应；导出使用同一套查询参数。 */
export interface ScenarioQuery {
  name?: string;
  category?: string;
  enabled?: boolean;
  published_from?: string;
  published_to?: string;
  deleted?: boolean;
  page: number;
  page_size: number;
}

export interface ScenarioOption {
  scenario_code: string;
  scenario_name: string;
  category: string | null;
  enabled: boolean;
  latest_version_no: number;
}

export function listScenes(query: ScenarioQuery): Promise<Paged<ScenarioListItem>> {
  return request<Paged<ScenarioListItem>>(`/scenarios${toQuery({ ...query })}`);
}

export function sceneExportUrl(query: Omit<ScenarioQuery, "page" | "page_size">): string {
  return apiFileUrl(`/scenarios/export${toQuery({ ...query })}`);
}

export function listSceneOptions(): Promise<{ request_id: string; items: ScenarioOption[] }> {
  return request<{ request_id: string; items: ScenarioOption[] }>("/scenarios/options");
}

export interface ScenarioPayload {
  scenario_name?: string;
  category?: string | null;
  description?: string | null;
  enabled?: boolean;
}

export function getScene(scenarioCode: string): Promise<ScenarioDetail> {
  return request<ScenarioDetail>(`/scenarios/${encodeURIComponent(scenarioCode)}`);
}

export function updateScene(scenarioCode: string, payload: ScenarioPayload): Promise<ScenarioDetail> {
  return request<ScenarioDetail>(`/scenarios/${encodeURIComponent(scenarioCode)}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}

export function setSceneEnabled(scenarioCode: string, enabled: boolean): Promise<ScenarioDetail> {
  return request<ScenarioDetail>(`/scenarios/${encodeURIComponent(scenarioCode)}/enabled`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ enabled }),
  });
}

export function deleteScene(scenarioCode: string): Promise<ScenarioDetail> {
  return request<ScenarioDetail>(`/scenarios/${encodeURIComponent(scenarioCode)}`, { method: "DELETE" });
}

export function restoreScene(scenarioCode: string): Promise<ScenarioDetail> {
  return request<ScenarioDetail>(`/scenarios/${encodeURIComponent(scenarioCode)}/restore`, { method: "POST" });
}

export function copyScene(scenarioCode: string, scenarioName: string | null): Promise<ScenarioDetail> {
  return request<ScenarioDetail>(`/scenarios/${encodeURIComponent(scenarioCode)}/copy`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ scenario_name: scenarioName }),
  });
}

/* ---- 配置草稿 ---- */

export function getSceneDraft(scenarioCode: string): Promise<ScenarioDraftView> {
  return request<ScenarioDraftView>(`/scenarios/${encodeURIComponent(scenarioCode)}/draft`);
}

/** 草稿内的每次写入都返回最新的完整草稿视图，页面据服务端结果刷新。 */
export interface DraftMaterialPayload {
  name: string;
  material_type: string;
  required: boolean;
  description: string;
}

export function createDraftMaterial(scenarioCode: string, payload: DraftMaterialPayload): Promise<ScenarioDraftView> {
  return request<ScenarioDraftView>(`/scenarios/${encodeURIComponent(scenarioCode)}/draft/materials`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}

export function updateDraftMaterial(scenarioCode: string, materialKey: string, payload: Partial<DraftMaterialPayload>): Promise<ScenarioDraftView> {
  return request<ScenarioDraftView>(
    `/scenarios/${encodeURIComponent(scenarioCode)}/draft/materials/${encodeURIComponent(materialKey)}`,
    { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) },
  );
}

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
  unit: string | null;
  /** 币种：留空表示未声明，不表示人民币。 */
  currency?: string | null;
  /** 产物来源：当前仅 "SEAL"（印章识别）；留空表示常规字段抽取。 */
  artifact_source?: string | null;
  required: boolean;
  instruction: string;
}

export function createDraftField(scenarioCode: string, payload: DraftFieldPayload): Promise<ScenarioDraftView> {
  return request<ScenarioDraftView>(`/scenarios/${encodeURIComponent(scenarioCode)}/draft/fields`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}

export function updateDraftField(scenarioCode: string, fieldKey: string, payload: Partial<DraftFieldPayload>): Promise<ScenarioDraftView> {
  return request<ScenarioDraftView>(
    `/scenarios/${encodeURIComponent(scenarioCode)}/draft/fields/${encodeURIComponent(fieldKey)}`,
    { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) },
  );
}

export function deleteDraftField(scenarioCode: string, fieldKey: string): Promise<ScenarioDraftView> {
  return request<ScenarioDraftView>(
    `/scenarios/${encodeURIComponent(scenarioCode)}/draft/fields/${encodeURIComponent(fieldKey)}`,
    { method: "DELETE" },
  );
}

/** 规则的业务定义（与后端 DraftRuleRequest 对应）。 */
export interface DraftRulePayload {
  name: string;
  node: string;
  operator: string;
  enabled: boolean;
  inputs: Array<{ field_key: string; material_key: string }>;
  materials: string[];
  parameters: Record<string, JsonValue>;
  requirement: string;
  applicability: { kind: string };
  source_node: string | null;
  source_rule_key: string | null;
}

export function createDraftRule(scenarioCode: string, payload: DraftRulePayload): Promise<ScenarioDraftView> {
  return request<ScenarioDraftView>(`/scenarios/${encodeURIComponent(scenarioCode)}/draft/rules`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}

export function updateDraftRule(scenarioCode: string, ruleKey: string, payload: DraftRulePayload): Promise<ScenarioDraftView> {
  return request<ScenarioDraftView>(
    `/scenarios/${encodeURIComponent(scenarioCode)}/draft/rules/${encodeURIComponent(ruleKey)}`,
    { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) },
  );
}

export function setDraftRuleEnabled(scenarioCode: string, ruleKey: string, enabled: boolean): Promise<ScenarioDraftView> {
  return request<ScenarioDraftView>(
    `/scenarios/${encodeURIComponent(scenarioCode)}/draft/rules/${encodeURIComponent(ruleKey)}/enabled`,
    { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ enabled }) },
  );
}

export function deleteDraftRule(scenarioCode: string, ruleKey: string): Promise<ScenarioDraftView> {
  return request<ScenarioDraftView>(
    `/scenarios/${encodeURIComponent(scenarioCode)}/draft/rules/${encodeURIComponent(ruleKey)}`,
    { method: "DELETE" },
  );
}

export interface DraftSectionPayload {
  title: string;
  content_instruction: string;
  field_keys: string[];
  /**
   * 节点版（contract_review_v3）的报告分组属性。旧格式章节留空即可：
   * 留空表示该章节没有报告编号，按旧的「附加展示章节」语义渲染。
   */
  report_code?: string | null;
  report_name?: string | null;
  material_keys?: string[];
  formats?: string[];
  section_order?: number | null;
  content_type?: string | null;
  result_sort?: string | null;
  config_status?: string | null;
}

export function createDraftSection(scenarioCode: string, payload: DraftSectionPayload): Promise<ScenarioDraftView> {
  return request<ScenarioDraftView>(`/scenarios/${encodeURIComponent(scenarioCode)}/draft/sections`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}

export function updateDraftSection(scenarioCode: string, sectionKey: string, payload: Partial<DraftSectionPayload>): Promise<ScenarioDraftView> {
  return request<ScenarioDraftView>(
    `/scenarios/${encodeURIComponent(scenarioCode)}/draft/sections/${encodeURIComponent(sectionKey)}`,
    { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) },
  );
}

export function deleteDraftSection(scenarioCode: string, sectionKey: string): Promise<ScenarioDraftView> {
  return request<ScenarioDraftView>(
    `/scenarios/${encodeURIComponent(scenarioCode)}/draft/sections/${encodeURIComponent(sectionKey)}`,
    { method: "DELETE" },
  );
}

/** 发布草稿：必须携带页面看到的草稿修订号；内容与最新版本一致时返回原版本。 */
export function publishSceneDraft(
  scenarioCode: string,
  payload: { expected_revision: number; change_summary?: string | null },
): Promise<ScenarioPublishResult> {
  return request<ScenarioPublishResult>(`/scenarios/${encodeURIComponent(scenarioCode)}/draft/publish`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}

export function sceneDraftExportUrl(scenarioCode: string): string {
  return apiFileUrl(`/scenarios/${encodeURIComponent(scenarioCode)}/draft/export`);
}

/* ---- 版本 ---- */

/**
 * 场景配置只有一份：页面不再有“场景版本”概念（编辑即生效，无版本列表、
 * 无版本对比、无基于版本创建修订）。因此这里不提供任何版本查询入口。
 */

export type { DraftFieldItem, DraftMaterialItem, DraftRuleItem, DraftSectionItem };
