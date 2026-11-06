import { statusFilterOptions } from "@/constants/labels";

/** 后端真实使用的导入作业状态，列表与规则库文件页共用同一套筛选值。 */
export const importJobStatusOptions = statusFilterOptions([
  "PARSED",
  "PROPOSED",
  "APPLIED",
  "EXTERNAL_FAILED",
  "CONTRACT_INVALID",
]);
