/** 材料产物状态与任务执行状态具有不同语义，按材料处理上下文统一展示。 */
import type { DocumentArtifactStatus, DocumentOcrStatus } from "@/api/contracts";
import type { StatusTone } from "./labels";

export interface ProcessingArtifact {
  artifact_type: DocumentArtifactStatus["artifact_type"] | "OCR";
  artifact_no: string | null;
  status: string;
  message: string;
  job_id?: string | null;
  deadline_at?: string | null;
  provider_state?: string | null;
  extract_progress?: DocumentOcrStatus["extract_progress"];
  progress_updated_at?: string | null;
}

const artifactStatuses: Record<string, { label: string; tone: StatusTone }> = {
  NOT_SUBMITTED: { label: "未提交", tone: "neutral" },
  SUBMITTING: { label: "提交结果未知", tone: "warning" },
  PROCESSING: { label: "处理结果未知", tone: "warning" },
  PENDING: { label: "等待完成", tone: "info" },
  READY: { label: "已就绪", tone: "success" },
  FAILED: { label: "处理失败", tone: "danger" },
  INVALIDATED: { label: "已失效", tone: "neutral" },
  SKIPPED: { label: "已停止处理", tone: "warning" },
};

export function processingStatusLabel(artifact: ProcessingArtifact, operationActive: boolean): string {
  if (operationActive && artifact.status === "SUBMITTING") return "提交中";
  if (operationActive && artifact.status === "PROCESSING") return "处理中";
  if (artifact.artifact_type === "OCR" && artifact.status === "PENDING") {
    if (artifact.provider_state === "running") return "识别中";
    if (artifact.provider_state === "pending") return "排队中";
    if (artifact.provider_state === "done") return "识别完成，正在保存";
    if (artifact.job_id) return "可继续查询";
  }
  return artifactStatuses[artifact.status]?.label ?? artifact.status;
}

export function processingStatusTone(artifact: ProcessingArtifact, operationActive: boolean): StatusTone {
  if (operationActive && ["SUBMITTING", "PROCESSING"].includes(artifact.status)) return "info";
  return artifactStatuses[artifact.status]?.tone ?? "neutral";
}

export function processingArtifactLabel(artifactType: ProcessingArtifact["artifact_type"]): string {
  return {
    MATERIAL_CLASSIFY: "材料类型识别",
    DOCUMENT_PARSE: "去除印章",
    OCR: "正文识别",
    SEAL: "印章识别",
  }[artifactType];
}
