import type { ImportJobView } from "@/api/imports";
import { isOperationActive, isOperationFinished, type OperationView } from "@/api/http";

/**
 * 导入流程的五个业务步骤。
 *
 * 每一步的可进入状态完全由服务端数据推导：
 * 作业没有 proposals 就不能进第二、三步；还有未处理问题就不能进第四步；
 * 发布检查没通过就不能点发布。这里不产生任何「前端假装完成」的状态。
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

export function buildImportSteps(options: {
  job: ImportJobView | null;
  operation: OperationView | null;
  hasProposals: boolean;
  blockingIssues: number;
  precheckReady: boolean;
  published: boolean;
}): ImportStep[] {
  const { job, operation, hasProposals, blockingIssues, precheckReady, published } = options;
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
      description: "为每张工作表指定用途与表头行，或设为不参与发布",
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
      label: "发布前预览",
      description: "核对将要发布的材料、字段、规则与报告章节；本步只读，不改配置",
      enabled: true,
      state: "not-started",
      blockedReason: null,
    },
    {
      label: "发布模板",
      description: "确认发布意图、执行发布检查、处理能力缺口并发布新版本",
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
    steps[1].description = "正在进行规则解析与转换，可在此查看后台真实状态";
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

  // 第 4 步
  if (!hasProposals) {
    steps[3].enabled = false;
    steps[3].blockedReason = "解析完成后才能预览将要发布的内容。";
  } else if (blockingIssues > 0) {
    steps[3].enabled = false;
    steps[3].state = "blocked";
    steps[3].blockedReason = `先处理完 ${blockingIssues} 项业务问题，再进入发布前预览。`;
  } else if (published || precheckReady) {
    // 发布检查已经通过（或作业已发布，其 precheck 记录同样为 ready）：
    // 这一步的真实业务含义已经完成。少了这个分支，已发布的作业回到这一页时
    // 第 4 步会因为没有 done 状态而显示成灰色未开始，与它实际走过的流程不符。
    steps[3].state = "done";
    steps[3].description = "发布检查已通过";
  } else {
    steps[3].state = "current";
  }

  // 第 5 步
  if (published) {
    steps[4].state = "done";
    steps[4].description = "模板版本已发布";
  } else if (!hasProposals) {
    steps[4].enabled = false;
    steps[4].blockedReason = "还没有可发布的转换结果。";
  } else if (blockingIssues > 0) {
    steps[4].enabled = false;
    steps[4].state = "blocked";
    steps[4].blockedReason = "存在未处理的业务问题，不能发布。";
  } else if (precheckReady) {
    steps[4].state = "current";
    steps[4].description = "发布检查已通过，可以发布";
  } else {
    steps[4].state = "not-started";
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
