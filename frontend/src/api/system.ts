import type {
  MaterialTypeItem,
  OperationLogRow,
  Paged,
  StatusVocabulary,
  SystemServices,
  UploadPolicy,
} from "./contracts";
import { request } from "./http";
import { toQuery } from "./query";

/**
 * 8018 支持的材料类型目录。材料上传页从这里取值，
 * 不内置默认清单、不把 8018 的取值与业务材料名称做映射。
 */
export async function getMaterialTypes(): Promise<MaterialTypeItem[]> {
  const response = await request<{ items: MaterialTypeItem[] }>("/system/material-types");
  return response.items;
}

/**
 * 上传策略：规则库与业务材料各自的大小上限与允许扩展名。
 * 上传界面必须从这里读取并展示，不得在组件里另抄一份常量——复制的那份会在
 * 配置变更后与后端实际校验不一致。
 */
export function getUploadPolicy(): Promise<UploadPolicy> {
  return request<UploadPolicy>("/system/upload-policy");
}

/**
 * 服务状态：配置状态与连接状态分开；只有真实探测过的连接才显示已连接。
 * `refresh` 忽略服务端的短时探测缓存，立即重新探测。
 */
export function getSystemServices(refresh = false): Promise<SystemServices> {
  return request<SystemServices>(`/system/services${refresh ? "?refresh=true" : ""}`);
}

/**
 * 全部语境的审核状态词表：规则、风险、字段各自的唯一文案、分档与说明。
 * 页面按语境取文案，不再各自维护状态表。
 */
export function getStatusVocabulary(): Promise<StatusVocabulary> {
  return request<StatusVocabulary>("/system/status-vocabulary");
}

export interface OperationQuery {
  object_type?: string;
  operation_type?: string;
  result?: string;
  keyword?: string;
  created_from?: string;
  created_to?: string;
  page: number;
  page_size: number;
}

/** 操作日志：业务写操作与后台命令并列，各自的编号独立可查。 */
export function listOperations(query: OperationQuery): Promise<Paged<OperationLogRow>> {
  return request<Paged<OperationLogRow>>(`/system/operations${toQuery({ ...query })}`);
}
