import type { ImportJob } from "@/api/contracts";
import { isOperationActive, type OperationView } from "@/api/http";

/**
 * 导入流程的三个业务步骤。
 *
 * 每一步的可进入状态完全由服务端数据推导：
 * 作业没有 proposals 就不能进入转换结果；解析得到的候选可以直接写入草稿。
 * 导入只生成草稿，不在此处发布场景。
 */
export type StepState = "not-started" | "current" | "done" | "blocked" | "running" | "failed";

export interface ImportStep {
  index: number;
  label: string;
  description: string;
  state: StepState;
  /** 被阻断时给出的中文原因，显示在步骤下方。 */
  blockedReason: string | null;
  /** 是否允许点击进入。 */
  enabled: boolean;
}

/** 按当前服务端数据算出三个导入步骤的可进入性与阻断原因。 */
export function buildImportSteps(options: {
  job: ImportJob | null;
  operation: OperationView | null;
  hasProposals: boolean;
  published: boolean;
}): ImportStep[] {
  const { job, operation, hasProposals, published } = options;
  const operationRunning = isOperationActive(operation?.status);
  const operationFailed = operation?.status === "FAILED" || operation?.status === "INTERRUPTED";
  const hasFile = Boolean(job);

  const steps: Array<Omit<ImportStep, "index">> = [
    {
      label: "上传规则 Excel",
      description: "上传当前场景的 Excel 规则库",
      enabled: true,
      state: "current",
      blockedReason: null,
    },
    {
      label: "解析结果",
      description: "核对解析配置，编辑规则并调整启停",
      enabled: true,
      state: "not-started",
      blockedReason: null,
    },
    {
      label: "写入场景草稿",
      description: "把解析配置写入所属场景草稿，不发布场景",
      enabled: true,
      state: "not-started",
      blockedReason: null,
    },
  ];

  // 第 1 步
  if (hasFile) {
    steps[0].state = "done";
    steps[0].description = `已选择文件：${job!.source_filename}`;
  }

  // 第 2 步（解析结果）
  if (operationRunning) {
    steps[1].state = "running";
    steps[1].description = "正在解析规则库，请稍候";
  } else if (operationFailed) {
    steps[1].state = "failed";
    steps[1].description = "上次解析未完成，可在本步重新提交解析";
  } else if (hasProposals) {
    steps[1].state = "done";
    steps[1].description = "解析已完成，可调整业务配置与启停";
  } else if (hasFile) {
    steps[1].state = "current";
  } else {
    steps[1].enabled = false;
    steps[1].blockedReason = "请先上传规则 Excel 文件。";
  }

  // 第 3 步（写入场景草稿）
  if (!hasProposals) {
    steps[2].enabled = false;
    steps[2].blockedReason = operationRunning ? "解析仍在进行，完成后才能确认候选。" : "请先完成解析。";
  } else if (published) {
    steps[2].state = "done";
    steps[2].description = "已写入场景草稿；发布请到场景配置页执行";
  } else {
    steps[2].state = "current";
    steps[2].description = "确认后替换场景业务配置草稿，不会发布场景版本";
  }

  return steps.map((step, index) => ({ ...step, index }));
}
