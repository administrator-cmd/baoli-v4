import type { DocumentDetail, DocumentRow, JsonValue, Paged } from "./contracts";
import { request } from "./http";
import { toQuery } from "./query";

export interface DocumentQuery {
  task_no?: string;
  task_name?: string;
  keyword?: string;
  material_name?: string;
  has_file?: boolean;
  page: number;
  page_size: number;
}

/** 审核材料分页列表：以任务材料位为行，显示当前文件与处理状态。 */
export function listDocuments(query: DocumentQuery): Promise<Paged<DocumentRow>> {
  return request<Paged<DocumentRow>>(`/documents${toQuery({ ...query })}`);
}

/** 材料位详情：当前文件与历史上传版本。 */
export function getDocument(documentNo: string): Promise<DocumentDetail> {
  return request<DocumentDetail>(`/documents/${encodeURIComponent(documentNo)}`);
}

export type { JsonValue };
