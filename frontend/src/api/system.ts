import type {
  MaterialTypeItem,
  OperationLogRow,
  Paged,
  RuleStatusCatalog,
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

/** 服务状态：配置状态与连接状态分开；只有真实探测过的连接才显示已连接。 */
export function getSystemServices(): Promise<SystemServices> {
  return request<SystemServices>("/system/services");
}

/**
 * 全部审核结论状态的标准口径与分档。
 * 页面用它区分「已判定 / 尚未判定 / 不适用」，未识别的状态码必须显式显示原始码。
 */
export function getRuleStatusCatalog(): Promise<RuleStatusCatalog> {
  return request<RuleStatusCatalog>("/system/rule-statuses");
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
