import type { MaterialQualityStatus } from "@/api/contracts";
import type { StatusTone } from "@/constants/labels";

const statuses: Record<MaterialQualityStatus, { label: string; tone: StatusTone }> = {
  NO_FILE: { label: "待上传", tone: "neutral" },
  NOT_CHECKED: { label: "待检查", tone: "neutral" },
  RUNNING: { label: "检查中", tone: "info" },
  PASSED: { label: "前置检查通过", tone: "success" },
  ABNORMAL: { label: "不合格", tone: "danger" },
  EVIDENCE_INSUFFICIENT: { label: "无法判定", tone: "warning" },
  NOT_APPLICABLE: { label: "不适用", tone: "neutral" },
  NOT_CONFIGURED: { label: "无前置检查", tone: "neutral" },
};

export function materialQualityLabel(status?: MaterialQualityStatus | null): string {
  return statuses[status ?? "NOT_CHECKED"].label;
}

export function materialQualityTone(status?: MaterialQualityStatus | null): StatusTone {
  return statuses[status ?? "NOT_CHECKED"].tone;
}
