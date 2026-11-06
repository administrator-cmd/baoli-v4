/**
 * 中文业务标签与状态色。
 *
 * 通用任务、执行与材料状态使用本文件；审核结论、风险结论与字段状态的文案
 * 统一来自后端状态词表，见 statusVocabulary.ts。
 */

export type StatusTone = "neutral" | "info" | "success" | "warning" | "danger";

const statusLabels: Record<string, string> = {
  PARSED: "已读取",
  OPEN: "待处理",
  CONNECTED: "已连接",
  DEGRADED: "服务降级",
  UNCONFIGURED: "未配置",
  CONFIGURATION_INCOMPLETE: "配置不完整",
  EMPTY: "无数据，已跳过",
  NO_BUSINESS_DATA: "无业务数据，已跳过",
  HIDDEN: "隐藏工作表",
  EXCLUDED: "已排除",
  BOUND: "已选择",
  PROPOSED: "已转换",
  CONFIRMED: "已确认",
  // 场景版本发布状态；导入作业写入草稿后为 APPLIED。
  PUBLISHED: "已发布",
  APPLIED: "已写入场景草稿",
  CONTRACT_INVALID: "转换结果无效",
  SOURCE_REF_INVALID: "来源引用无效",
  EXTERNAL_FAILED: "外部服务失败",
  CHECKPOINT_MISSING: "流程检查点丢失",
  READY: "待执行",
  CURRENT: "当前节点",
  PENDING: "待处理",
  WAITING: "等待处理",
  SUCCEEDED: "已完成",
  FAILED: "执行失败",
  INTERRUPTED: "已中断",
  BLOCKED: "已阻断",
  SKIPPED: "已跳过",
  WAITING_REVIEW: "等待人工复核",
  WAITING_FIELD_REVIEW: "等待字段核对",
  COMPLETED: "已全部完成",
  INVALIDATED: "已失效",
  WAITING_UPLOAD: "待上传",
  UPLOADED: "已上传",
  PREPARED: "已处理",
  PROCESSED: "已处理",
  RESOLVED: "已解决",
  TYPE_REVIEW: "材料类型待复核",
  TYPE_REJECTED: "材料类型不符",
  SUPERSEDED: "已替换",
  PASS: "通过",
  COMPLETE: "已生成",
  INCOMPLETE: "未完整执行",
  NOT_GENERATED: "未生成",
  AUTO: "自动识别",
  MANUAL: "已人工处理",

  // 外部作业阶段、依据类型、逐行处置
  PROCESSING: "处理中",
  HUMAN_STATEMENT: "人工业务判断",
  DOCUMENT_EVIDENCE: "文档证据",
  UPLOAD_RECORD: "上传记录",
  DOCUMENT_EXTRACTION: "文档抽取",
  HUMAN_CORRECTION: "人工纠错",
  CANDIDATE: "已形成候选",
  QUESTION: "待处理问题",
  EXTERNAL_SUBMITTED: "外部已受理",
  RESULT_UNKNOWN: "结果未知",
  BUSINESS_COMMITTED: "业务已提交",
  GRAPH_ADVANCED: "流程已推进",
  INTERRUPT_WAITING: "等待人工复核",

  // 管理模块：报告快照、归档、文件移除、后台命令受理、服务配置状态
  GENERATED: "已生成",
  ARCHIVED: "已归档",
  REMOVED: "已移除",
  ACCEPTED: "执行中",
  CONFIGURED: "已配置",
  NOT_CONFIGURED: "未配置",
  STALE: "已失效",

};

/**
 * 状态色。语义固定为：
 * 绿=已完成/已通过，蓝=进行中/可执行，橙=需要人处理，红=失败或阻断，灰=未开始/已跳过/已排除。
 * 「已跳过」「未执行」「不存在风险结果」都不是绿色，避免被读成通过。
 */
const statusTones: Record<string, StatusTone> = {
  // 连接与系统
  CONNECTED: "success",
  DEGRADED: "warning",

  // 导入作业与工作表
  PARSED: "info",
  OPEN: "warning",
  EMPTY: "neutral",
  NO_BUSINESS_DATA: "neutral",
  HIDDEN: "neutral",
  EXCLUDED: "neutral",
  BOUND: "info",
  PROPOSED: "info",
  CONFIRMED: "success",
  PUBLISHED: "success",
  CONTRACT_INVALID: "danger",
  SOURCE_REF_INVALID: "danger",
  EXTERNAL_FAILED: "danger",
  CHECKPOINT_MISSING: "danger",

  // 节点与任务
  READY: "info",
  CURRENT: "info",
  PENDING: "warning",
  WAITING: "warning",
  SUCCEEDED: "success",
  FAILED: "danger",
  INTERRUPTED: "danger",
  BLOCKED: "danger",
  SKIPPED: "neutral",
  INVALIDATED: "neutral",
  WAITING_REVIEW: "warning",
  WAITING_FIELD_REVIEW: "warning",
  COMPLETED: "success",

  // 材料
  WAITING_UPLOAD: "warning",
  UPLOADED: "info",
  PREPARED: "success",
  PROCESSED: "success",
  SUPERSEDED: "neutral",
  TYPE_REVIEW: "warning",
  TYPE_REJECTED: "danger",

  // 字段与规则结果
  PASS: "success",

  // 报告
  COMPLETE: "success",
  INCOMPLETE: "warning",
  NOT_GENERATED: "neutral",

  // 候选处理方式
  AUTO: "info",
  MANUAL: "success",
  RESOLVED: "success",

  // 外部作业阶段与结果未知：未结束一律蓝，结果未知是橙（需要人处理），不给绿。
  PROCESSING: "info",
  EXTERNAL_SUBMITTED: "info",
  BUSINESS_COMMITTED: "info",
  GRAPH_ADVANCED: "info",
  INTERRUPT_WAITING: "warning",
  RESULT_UNKNOWN: "warning",

  // 依据类型
  HUMAN_STATEMENT: "info",
  DOCUMENT_EVIDENCE: "info",
  UPLOAD_RECORD: "info",

  // 管理模块：报告归档、文件移除、服务配置
  GENERATED: "success",
  ARCHIVED: "neutral",
  REMOVED: "neutral",
  ACCEPTED: "info",
  CONFIGURED: "success",
  UNCONFIGURED: "neutral",
  CONFIGURATION_INCOMPLETE: "warning",
  NOT_CONFIGURED: "neutral",
};

/** 字段事实来源：人工纠错与文档抽取必须分开显示。 */
const factOriginLabels: Record<string, string> = {
  DOCUMENT_EXTRACTION: "文档抽取",
  HUMAN_CORRECTION: "人工纠错",
};

export function factOriginLabel(origin: string | null | undefined): string {
  return origin ? factOriginLabels[origin] ?? "来源未知" : "来源未知";
}

/** 后台操作阶段：区分已保存未执行 / 外部已受理 / 结果未知 / 业务已提交但图未推进。 */
const operationPhaseLabels: Record<string, string> = {
  COMMAND_ACCEPTED: "已受理",
  LLM_REQUEST: "请求模型",
  EXTERNAL_SUBMITTED: "外部服务已受理",
  RESULT_UNKNOWN: "外部结果未知",
  BUSINESS_COMMITTED: "业务结果已提交",
  GRAPH_ADVANCED: "流程已推进",
  INTERRUPT_WAITING: "等待继续执行",
  FIELD_REVIEW_WAITING: "等待字段核对",
  RESULT_READY: "结果已就绪",
};

export function operationPhaseLabel(phase: string | null | undefined): string | null {
  return phase ? operationPhaseLabels[phase] ?? null : null;
}

/** 后台操作状态：受理中/执行中是蓝色，结束态沿用业务色。 */
const operationTones: Record<string, StatusTone> = {
  PENDING: "info",
  RUNNING: "info",
  SUCCEEDED: "success",
  FAILED: "danger",
  INTERRUPTED: "danger",
};

export function statusTone(status: string | null | undefined): StatusTone {
  if (!status) {
    return "neutral";
  }
  return operationTones[status] ?? statusTones[status] ?? "neutral";
}

/** el-tag 的 type 取值。状态色只在这里映射一次，页面不再各自判断颜色。 */
export type TagType = "primary" | "success" | "info" | "warning" | "danger";

const TONE_TO_TAG_TYPE: Record<StatusTone, TagType> = {
  neutral: "info",
  info: "primary",
  success: "success",
  warning: "warning",
  danger: "danger",
};

export function toneTagType(tone: StatusTone): TagType {
  return TONE_TO_TAG_TYPE[tone];
}

/** 状态 → Element Plus 标签颜色。 */
export function statusTagType(status: string | null | undefined): TagType {
  return toneTagType(statusTone(status));
}

const operationTypeLabels: Record<string, string> = {
  IMPORT_CONVERT: "规则解析与转换",
  SOURCE_ACTIVATION_PREVIEW: "来源增量解析",
  NODE_EXECUTE: "规则执行",
  NODE_RESUME: "中断恢复",
  FIELD_REVIEW_CONFIRM: "确认字段核对并继续",
  DOCUMENT_CLASSIFY: "文件自动校验",
  DOCUMENT_ARTIFACT_RETRY: "材料单项重试",
  OCR_DOCUMENT_RETRY: "OCR 单份重试",
  // 管理模块：业务写操作与命令
  CREATE: "新增",
  UPDATE: "编辑",
  ENABLE: "启用",
  DISABLE: "停用",
  DELETE: "删除",
  RESTORE: "恢复",
  COPY: "复制",
  PUBLISH: "发布",
  SAVE: "保存方案配置",
  DRAFT_MATERIAL_CREATE: "新增材料配置",
  DRAFT_MATERIAL_UPDATE: "编辑材料配置",
  DRAFT_MATERIAL_DELETE: "删除材料配置",
  DRAFT_FIELD_CREATE: "新增字段配置",
  DRAFT_FIELD_UPDATE: "编辑字段配置",
  DRAFT_FIELD_DELETE: "删除字段配置",
  DRAFT_RULE_CREATE: "新增规则配置",
  DRAFT_RULE_UPDATE: "编辑规则配置",
  DRAFT_RULE_ENABLE: "启用规则",
  DRAFT_RULE_DISABLE: "停用规则",
  DRAFT_RULE_DELETE: "删除规则配置",
  DRAFT_CALCULATION_CREATE: "新增计算规则",
  DRAFT_CALCULATION_UPDATE: "编辑计算规则",
  DRAFT_CALCULATION_DELETE: "删除计算规则",
  DRAFT_SECTION_CREATE: "新增报告章节",
  DRAFT_SECTION_UPDATE: "编辑报告章节",
  DRAFT_SECTION_DELETE: "删除报告章节",
  REVISE_FROM_VERSION: "基于版本创建修订",
  APPLY_DRAFT: "导入写入场景草稿",
  IMPORT_APPLY: "规则库写入场景草稿",
  RULEBOOK_GENERATE: "生成规则库",
  REPORT_TEMPLATE_ANALYZE: "分析报告模板",
  RULEBOOK_REVISE: "AI 修改规则库",
  TASK_CREATE: "创建审核任务",
  FIELD_CORRECTION: "字段纠错",
  REPORT_REMARK: "编辑报告备注",
  REPORT_ARCHIVE: "归档报告",
  REPORT_UNARCHIVE: "取消归档",
  TASK_COPY: "复制审核任务",
  ABORT: "中止导入",
};

/**
 * 操作日志筛选只列当前后端实际写入的操作类型。
 *
 * 标签表还保留少量历史/展示兼容文案，不能把它们全部当成筛选值，否则
 * COPY、FIELD_CORRECTION 等没有生产者的选项会永远返回空结果。
 */
const producedOperationTypeValues = [
  "IMPORT_CONVERT",
  "NODE_EXECUTE",
  "NODE_RESUME",
  "FIELD_REVIEW_CONFIRM",
  "DOCUMENT_CLASSIFY",
  "DOCUMENT_ARTIFACT_RETRY",
  "OCR_DOCUMENT_RETRY",
  "CREATE",
  "UPDATE",
  "ENABLE",
  "DISABLE",
  "DELETE",
  "RESTORE",
  "PUBLISH",
  "SAVE",
  "DRAFT_MATERIAL_CREATE",
  "DRAFT_MATERIAL_UPDATE",
  "DRAFT_MATERIAL_DELETE",
  "DRAFT_FIELD_CREATE",
  "DRAFT_FIELD_UPDATE",
  "DRAFT_FIELD_DELETE",
  "DRAFT_RULE_CREATE",
  "DRAFT_RULE_UPDATE",
  "DRAFT_RULE_ENABLE",
  "DRAFT_RULE_DISABLE",
  "DRAFT_RULE_DELETE",
  "DRAFT_CALCULATION_CREATE",
  "DRAFT_CALCULATION_UPDATE",
  "DRAFT_CALCULATION_DELETE",
  "DRAFT_SECTION_CREATE",
  "DRAFT_SECTION_UPDATE",
  "DRAFT_SECTION_DELETE",
  "APPLY_DRAFT",
  "IMPORT_APPLY",
  "RULEBOOK_GENERATE",
  "REPORT_TEMPLATE_ANALYZE",
  "RULEBOOK_REVISE",
  "TASK_CREATE",
  "REPORT_REMARK",
  "REPORT_ARCHIVE",
  "REPORT_UNARCHIVE",
  "TASK_COPY",
  "ABORT",
] as const;

export const operationTypeFilterOptions = producedOperationTypeValues.map((value) => ({
  value,
  label: operationTypeLabels[value],
}));

export function operationTypeLabel(operationType: string | null | undefined): string {
  return operationType ? operationTypeLabels[operationType] ?? "后台操作" : "后台操作";
}

/** 操作日志的对象类型中文名。 */
const objectTypeLabels: Record<string, string> = {
  SCENARIO: "审核场景",
  AUDIT_PLAN: "审核方案",
  AUDIT_TASK: "审核任务",
  IMPORT_JOB: "规则库导入",
  RULEBOOK_GENERATION: "AI生成规则库",
  REPORT_TEMPLATE: "报告模板",
  AUDIT_REPORT: "审核报告",
};

/** 对象类型筛选与展示标签共用同一份来源。 */
export const objectTypeFilterOptions = Object.entries(objectTypeLabels).map(([value, label]) => ({ value, label }));

export function objectTypeLabel(objectType: string | null | undefined): string {
  return objectType ? objectTypeLabels[objectType] ?? objectType : "未知对象";
}

/** 规则执行技术容器的展示名称。 */
const nodeLabels: Record<string, string> = {
  RULE_EXECUTION: "执行生效规则",
};

export function nodeLabel(nodeCode: string | null | undefined): string {
  return nodeCode ? nodeLabels[nodeCode] ?? "未知节点" : "未知节点";
}

/** 规则结果状态的中文口径；未识别的状态码连同原始码一起显示。 */
export function statusLabel(status: string | null | undefined): string {
  if (!status) {
    return "未开始";
  }
  if (status === "RUNNING") {
    return "执行中";
  }
  // 未知状态码必须把原始码一并显示：只显示「状态未知」会让新增状态在页面上
  // 无法诊断，也无法与「确实没有状态」区分开。
  const label = statusLabels[status];
  return label ? label : `未识别状态（${status}）`;
}

/** 用统一状态文案生成筛选下拉；页面只声明状态码和必要的上下文特例。 */
export function statusFilterOptions(
  values: readonly string[],
  overrides: Record<string, string> = {},
): Array<{ value: string; label: string }> {
  return values.map((value) => ({ value, label: overrides[value] ?? statusLabel(value) }));
}

const valueTypeLabels: Record<string, string> = {
  STRING: "文本",
  DECIMAL: "数值",
  AMOUNT: "金额",
  DECIMAL_CN: "大写金额",
  DATE: "日期",
  BOOLEAN: "是否",
  CLAUSE: "条款",
};

export function valueTypeLabel(valueType: string | null | undefined): string {
  return valueType ? valueTypeLabels[valueType] ?? "未指定类型" : "未指定类型";
}

/** 业务取值 → 展示文本；null / undefined / 空串统一显示为「未提供」，不留空白。 */
export function formatBusinessValue(value: unknown): string {
  if (value === null || value === undefined || value === "") {
    return "未提供";
  }
  if (typeof value === "string" || typeof value === "number") {
    return String(value);
  }
  if (typeof value === "boolean") {
    return value ? "是" : "否";
  }
  if (Array.isArray(value)) {
    return value.map((item) => formatBusinessValue(item)).join("、") || "未提供";
  }
  if (typeof value === "object") {
    const values = Object.values(value as Record<string, unknown>);
    return values.map((item) => formatBusinessValue(item)).join("；") || "已形成结果";
  }
  return String(value);
}

/**
 * 后端时间统一按 UTC 写入 MySQL DATETIME，序列化后没有时区后缀。
 * 无后缀时明确按 UTC 解析；已有偏移量的 ISO 时间则按其自身时区解析。
 * 解析失败返回 null，由调用方决定如何展示。
 */
export function parseServerTime(value: string): Date | null {
  const normalized = /(?:Z|[+-]\d{2}:?\d{2})$/.test(value) ? value : `${value}Z`;
  const date = new Date(normalized);
  return Number.isNaN(date.getTime()) ? null : date;
}

export function formatDate(value: string): string {
  const date = parseServerTime(value);
  return date === null
    ? value
    : new Intl.DateTimeFormat("zh-CN", { dateStyle: "medium", timeStyle: "short" }).format(date);
}

/** 耗时：毫秒以下保留毫秒，其余按秒/分/时拆分。 */
export function formatDuration(ms: number | null): string {
  if (ms === null) return "—";
  if (ms < 1000) return `${ms} 毫秒`;
  if (ms < 60000) return `${(ms / 1000).toFixed(1)} 秒`;
  const seconds = Math.round(ms / 1000);
  const minutes = Math.floor(seconds / 60);
  return minutes < 60
    ? `${minutes} 分 ${seconds % 60} 秒`
    : `${Math.floor(minutes / 60)} 小时 ${minutes % 60} 分 ${seconds % 60} 秒`;
}

/** 文件大小：只用于业务展示，不显示本地路径。 */
export function formatFileSize(bytes: number | null | undefined): string {
  if (typeof bytes !== "number" || !Number.isFinite(bytes) || bytes < 0) {
    return "大小未知";
  }
  if (bytes < 1024) {
    return `${bytes} 字节`;
  }
  if (bytes < 1024 * 1024) {
    return `${(bytes / 1024).toFixed(1)} KB`;
  }
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

/** 业务节点的可选值，用于候选原地修正里选择「所属节点」。 */
export const nodeOptions = Object.keys(nodeLabels).map((value) => ({ value, label: nodeLabels[value] }));

/**
 * 规则参数的可选值。与后端 manifest.validate_rule_parameters 支持的取值保持一致：
 * 不在这些集合里的值会被后端拒绝，页面不提供自由输入以免产生无效参数。
 */
export const valueTypeOptions = [
  { value: "STRING", label: "文本" },
  { value: "DECIMAL", label: "数值" },
  { value: "AMOUNT", label: "金额" },
  { value: "DECIMAL_CN", label: "大写金额" },
  { value: "DATE", label: "日期" },
  { value: "BOOLEAN", label: "是否" },
  { value: "CLAUSE", label: "条款" },
];

export const extractionStrategyOptions = [
  { value: "RAW_TEXT", label: "保留原文" },
  { value: "DETERMINISTIC", label: "确定性转换" },
  { value: "STRUCTURED", label: "结构化抽取" },
] as const;

const extractionStrategyLabels: Record<string, string> = Object.fromEntries(
  extractionStrategyOptions.map((item) => [item.value, item.label]),
);

export function extractionStrategyLabel(value: string | null | undefined): string {
  return value ? extractionStrategyLabels[value] ?? value : "未配置";
}

export function defaultExtractionStrategy(valueType: string): "RAW_TEXT" | "DETERMINISTIC" {
  return ["STRING", "CLAUSE", "DECIMAL_CN"].includes(valueType) ? "RAW_TEXT" : "DETERMINISTIC";
}

export function extractionStrategySupports(strategy: string): boolean {
  // 普通策略只决定模型返回原文的方式，服务端再按 value_type 规范化；
  // 不把业务类型和策略硬编码成互斥组合。STRUCTURED 仅要求 Schema 完整。
  return strategy === "RAW_TEXT" || strategy === "DETERMINISTIC" || strategy === "STRUCTURED";
}

/**
 * 字段的「产物来源」：当前只有印章识别一种。
 * 留空表示该字段由常规字段抽取提供，不是“未指定所以随便挑一个产物”。
 */
export const artifactSourceOptions = [{ value: "SEAL", label: "印章识别" }];

const artifactSourceLabels: Record<string, string> = {
  SEAL: "印章识别",
};

export function artifactSourceLabel(value: string | null | undefined): string {
  return value ? artifactSourceLabels[value] ?? value : "常规抽取";
}

/**
 * 取数期间与报表口径：字段自己声明「这个数取的是哪一期、哪个口径」，
 * 计算参数按这两项定位字段事实。取值与参考格式里的 `value_labels` 同一口径，
 * 因此页面与 Excel 下拉看到的是同一套中文名。
 */
export const periodScopeOptions = [
  // 「不适用」是显式取值，不是留空占位；不涉及期间的字段与计算结果使用该口径。
  { value: "NOT_APPLICABLE", label: "不适用" },
  { value: "CURRENT", label: "本期" },
  { value: "PRIOR", label: "上期" },
  { value: "OPENING", label: "期初" },
  { value: "CLOSING", label: "期末" },
  { value: "AVERAGE_OPENING_CLOSING", label: "期初期末平均值" },
];

const periodScopeLabels: Record<string, string> = Object.fromEntries(
  periodScopeOptions.map((item) => [item.value, item.label]),
);

export function periodScopeLabel(value: string | null | undefined): string {
  return value ? periodScopeLabels[value] ?? value : "未声明";
}

export const statementScopeOptions = [
  { value: "NOT_APPLICABLE", label: "不适用" },
  { value: "CONSOLIDATED", label: "合并报表" },
  { value: "PARENT", label: "母公司报表" },
];

const statementScopeLabels: Record<string, string> = Object.fromEntries(
  statementScopeOptions.map((item) => [item.value, item.label]),
);

export function statementScopeLabel(value: string | null | undefined): string {
  return value ? statementScopeLabels[value] ?? value : "未声明";
}

/** 报告输出格式。当前尚未生成 PDF/Word/Excel 文件，这里维护的是配置目标格式。 */
export const reportFormatOptions = [
  { value: "Markdown", label: "Markdown" },
  { value: "PDF", label: "PDF" },
  { value: "Word", label: "Word" },
  { value: "Excel", label: "Excel" },
];

/** 报告章节的内容类型：决定章节取哪些已有结果，章节标题不参与推断。 */
export const reportContentTypeOptions = [
  { value: "FIELD_DETAIL", label: "字段明细" },
  { value: "AUDIT_RESULT", label: "审核结果" },
  { value: "FIELD_AND_RESULT", label: "字段及审核结果" },
  { value: "RISK_LIST", label: "风险清单" },
  { value: "CONCLUSION", label: "审核结论" },
];

export const reportResultSortOptions = [
  { value: "RISK_LEVEL_DESC", label: "风险等级降序" },
  { value: "MATERIAL_ORDER", label: "材料顺序" },
  { value: "MATERIAL_ORDER_RISK_LEVEL_DESC", label: "材料顺序；风险等级降序" },
];


/**
 * 规则动作的中文展示名。
 *
 * 仅用于展示规则原文中的动作名称；不构成可执行能力或工具选项清单。
 */
export const ruleOperatorOptions = [
  { value: "FILE_QUALITY_CHECK", label: "文件质量检查" },
  { value: "LLM_EXECUTE", label: "大模型审核" },
  { value: "LLM_EXTRACT", label: "大模型提取信息" },
  { value: "LLM_CALCULATE", label: "大模型计算" },
  { value: "FIELD_EQUAL", label: "字段相等" },
  { value: "FIELD_NOT_EQUAL", label: "字段不等" },
  { value: "DATE_COMPARE", label: "日期比较" },
  { value: "DECIMAL_COMPARE", label: "数值比较" },
  { value: "DECIMAL_SUM_COMPARE", label: "数值加总比较" },
  { value: "DECIMAL_CN_EQUAL", label: "大小写金额一致性" },
  { value: "FIELD_NOT_EMPTY", label: "字段非空校验" },
  { value: "CLAUSE_ABSENT", label: "条款不应存在" },
  { value: "LLM_VALIDATE", label: "大模型语义校验" },
  { value: "RISK_TRIGGER", label: "风险触发" },
  { value: "REPORT_RENDER", label: "生成报告" },
];
