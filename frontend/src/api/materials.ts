import type { DocumentDetail, DocumentRow, EvidenceRow, JsonValue, Paged } from "./contracts";
import { apiFileUrl, request } from "./http";
import { toQuery } from "./query";

export interface DocumentQuery {
  task_no?: string;
  keyword?: string;
  material_name?: string;
  has_file?: boolean;
  page: number;
  page_size: number;
}

export function listDocuments(query: DocumentQuery): Promise<Paged<DocumentRow>> {
  return request<Paged<DocumentRow>>(`/documents${toQuery({ ...query })}`);
}

export function documentExportUrl(query: Omit<DocumentQuery, "page" | "page_size">): string {
  return apiFileUrl(`/documents/export${toQuery({ ...query })}`);
}

export function getDocument(documentNo: string): Promise<DocumentDetail> {
  return request<DocumentDetail>(`/documents/${encodeURIComponent(documentNo)}`);
}

/** 上传版本文件的下载/预览地址（按文件唯一编号定位，历史版本同样可下载）。 */
export function documentFileUrl(documentNo: string, uploadNo: string, preview = false): string {
  return apiFileUrl(
    `/documents/${encodeURIComponent(documentNo)}/files/${encodeURIComponent(uploadNo)}/download${preview ? "?preview=true" : ""}`,
  );
}

export interface EvidenceQuery {
  task_no?: string;
  reference_type?: string;
  locator_status?: string;
  keyword?: string;
  page: number;
  page_size: number;
}

export function listEvidence(query: EvidenceQuery): Promise<Paged<EvidenceRow>> {
  return request<Paged<EvidenceRow>>(`/evidence${toQuery({ ...query })}`);
}

export function evidenceExportUrl(query: Omit<EvidenceQuery, "page" | "page_size">): string {
  return apiFileUrl(`/evidence/export${toQuery({ ...query })}`);
}

export type { JsonValue };
