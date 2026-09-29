import type { ImportJobView } from "@/api/imports";
import { isOperationActive, isOperationFinished, type OperationView } from "@/api/http";

/**
 * 导入流程的四个业务步骤。
 *
 * 每一步的可进入状态完全由服务端数据推导：
 * 作业没有 proposals 就不能进入转换结果；还有未处理问题就不能写入场景草稿。
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

/** 人工复核项：候选里存在待处理问题的数量。 */
export function importBlockingIssues(job: ImportJobView | null): number {
  return job?.issues.filter((issue) => issue.status === "OPEN").length ?? 0;
}

/** 按当前服务端数据算出四个导入步骤的可进入性与阻断原因。 */
export function buildImportSteps(options: {
  job: ImportJobView | null;
  operation: OperationView | null;
  hasProposals: boolean;
  blockingIssues: number;
  published: boolean;
}): ImportStep[] {
  const { job, operation, hasProposals, blockingIssues, published } = options;
  const operationRunning = isOperationActive(operation?.status);
  const operationFailed = operation?.status === "FAILED" || operation?.status === "INTERRUPTED";
  const hasFile = Boolean(job);

  const steps: Array<Omit<ImportStep, "index">> = [
    {
      label: "上传规则 Excel",
      description: "选择规则库文件并确认模板格式",
      enabled: true,
      state: "current",
      blockedReason: null,
    },
    {
      label: "确认工作表用途和表头",
      description: "为每张工作表指定用途、绑定节点，或设为不参与本次导入",
      enabled: true,
      state: "not-started",
      blockedReason: null,
    },
    {
      label: "规则转换、候选和歧义处理",
      description: "提交解析后逐项处理需要业务判断的候选与歧义",
      enabled: true,
      state: "not-started",
      blockedReason: null,
    },
    {
      label: "写入场景草稿",
      description: "把已确认的配置写入所属场景草稿，不发布场景",
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

  // 第 2 步
  if (operationRunning) {
    steps[1].state = "running";
    steps[1].description = "正在解析规则库，请稍候";
  } else if (operationFailed) {
    steps[1].state = "failed";
    steps[1].description = "上次解析未完成，可在本步重新提交解析";
  } else if (hasProposals) {
    steps[1].state = "done";
    steps[1].description = "工作表识别与转换已完成";
  } else if (hasFile) {
    steps[1].state = "current";
  } else {
    steps[1].enabled = false;
    steps[1].blockedReason = "请先上传规则 Excel 文件。";
  }

  // 第 3 步
  if (!hasProposals) {
    steps[2].enabled = false;
    steps[2].blockedReason = operationRunning ? "解析仍在进行，完成后才能确认候选。" : "请先完成工作表绑定与解析。";
  } else if (blockingIssues > 0) {
    steps[2].state = "current";
    steps[2].description = `还有 ${blockingIssues} 项需要业务判断`;
  } else {
    steps[2].state = "done";
    steps[2].description = "没有需要业务判断的歧义项";
  }

  // 第 4 步（写入场景草稿）
  if (!hasProposals) {
    steps[3].enabled = false;
    steps[3].blockedReason = "解析完成后才能写入场景草稿。";
  } else if (blockingIssues > 0) {
    steps[3].enabled = false;
    steps[3].state = "blocked";
    steps[3].blockedReason = `先处理完 ${blockingIssues} 项业务问题，再写入场景草稿。`;
  } else if (published) {
    steps[3].state = "done";
    steps[3].description = "已写入场景草稿；发布请到场景配置页执行";
  } else {
    steps[3].state = "current";
    steps[3].description = "确认后替换场景业务配置草稿，不会发布场景版本";
  }

  return steps.map((step, index) => ({ ...step, index }));
}

/** 操作结束后重新读取作业时，判断主动作是否还可以再次提交。 */
export function operationFinishedState(operation: OperationView | null): "none" | "running" | "failed" | "done" {
  if (!operation) {
    return "none";
  }
  if (!isOperationFinished(operation.status)) {
    return "running";
  }
  return operation.status === "SUCCEEDED" ? "done" : "failed";
}
