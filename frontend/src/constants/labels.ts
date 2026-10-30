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
  TASK_CREATE: "创建审核任务",
  FIELD_CORRECTION: "字段纠错",
  REPORT_REMARK: "编辑报告备注",
  REPORT_ARCHIVE: "归档报告",
  REPORT_UNARCHIVE: "取消归档",
};

export function operationTypeLabel(operationType: string | null | undefined): string {
  return operationType ? operationTypeLabels[operationType] ?? "后台操作" : "后台操作";
}

/** 操作日志的对象类型中文名。 */
const objectTypeLabels: Record<string, string> = {
  SCENARIO: "审核场景",
  AUDIT_PLAN: "审核方案",
  AUDIT_TASK: "审核任务",
  IMPORT_JOB: "规则库导入",
  AUDIT_REPORT: "审核报告",
};

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

/** 业务错误码 → 用户能执行的下一步。未收录的错误码只显示原始原因和错误码。 */
const failureNextSteps: Record<string, string> = {
  NETWORK_ERROR: "检查后端服务是否已启动，以及本机网络是否可达。",
  UNEXPECTED_ERROR: "这是前端页面自身抛出的错误，不是服务端返回；请把错误码和技术信息交给开发排查。",
  REQUEST_TIMEOUT: "已提交的后台操作不会丢失，可稍后重新查询状态；不要重复提交。",
  OPERATION_ALREADY_ACTIVE: "该对象已有正在执行的后台操作，请等待其结束或用「再次查询」查看真实状态。",
  INVALID_RESPONSE: "服务返回的内容无法识别，请查看后端日志确认接口版本是否一致。",
  TASK_DOCUMENT_REPLACE_CONFIRMATION_REQUIRED: "该材料位已有文件，替换上传需要先明确确认替换。",
  TASK_DOCUMENT_LOCKED: "任务已结束，材料不可再替换；如需继续请新建审核任务。",
  AUDIT_TASK_RUNNING: "任务正在执行当前节点，等待执行结束后再替换材料。",
  AUDIT_TASK_WAITING_FIELD_REVIEW: "任务正在等待字段核对；请在字段结果页核对后点「确认并继续审核」，不能另建批次绕过核对。",
  AUDIT_FIELD_REVIEW_NOT_WAITING: "任务已不在字段核对等待点，请刷新任务后查看当前状态。",
  AUDIT_FIELD_REVIEW_IN_PROGRESS: "任务正在等待字段核对，请直接在字段结果页修改，纠错入口会失效整个执行批次。",
  FIELD_REVISION_CONFLICT: "字段取值已被其他操作更新，请刷新任务后重新核对。",
  FIELD_REVIEW_VALUE_INVALID: "核对值不符合该字段声明的类型，请按字段类型填写。",
  FIELD_REVIEW_MATERIAL_MISSING: "该字段所属材料位还没有上传当前文件，无法补充取值。",
  AUDIT_FIELD_REVIEW_MISSING_UNACKNOWLEDGED: "字段仍有缺项；确认知悉这些缺项后才能继续，系统不会自动补齐。",
  AUDIT_FIELD_REVIEW_DEPENDENCY_CONFLICT: "字段抽取依赖了抽取以外的业务规则，无法在一次字段核对里完成；需要先调整规则依赖。",

  // 工作表选择错误与历史标准格式错误
  SHEET_ROLE_REQUIRED: "历史标准格式缺少工作表角色；可重新上传规则库，按当前 LLM 流程解析。",
  SHEET_ROLE_INVALID: "历史工作表角色无法识别；可重新上传规则库，按当前 LLM 流程解析。",
  SHEET_BINDING_INCOMPLETE: "请提交本次作业的全部工作表选择，且每张表只提交一次；刷新页面后重新确认解析范围。",
  SHEET_BINDING_DUPLICATE: "同一张工作表被提交了两次，请刷新页面后重新确认解析范围。",
  SHEET_NOT_FOUND: "该工作表已不在当前导入作业中，请刷新页面后重新确认解析范围。",
  SHEET_HEADER_REQUIRED: "历史标准格式缺少表头行；可重新上传规则库，按当前 LLM 流程解析。",
  SHEET_HEADER_INVALID: "历史标准格式的表头行无效；可重新上传规则库，按当前 LLM 流程解析。",
  TEMPLATE_METADATA_IMMUTABLE: "历史标准格式不将说明页作为业务表；新导入按所选工作表真实内容解析。",
  NO_BUSINESS_DATA_SHEET_IMMUTABLE: "这张工作表没有业务数据，请核对文件内容和本次解析范围。",

  // 规则填写超出系统支持范围
  RULE_CAPABILITY_UNSUPPORTED: "该规则存在执行能力缺口，请按返回的具体原因核对规则内容和场景节点配置。",
  LLM_STREAM_INCOMPLETE: "模型流式响应未完整结束，本次生成结果未保存；请查看后端流式请求日志。",
  LLM_OUTPUT_TRUNCATED: "模型因输出长度限制而截断了响应，本次生成结果未保存；请核对模型接口的输出限制。",
  LLM_RESULT_UNKNOWN: "先确认模型服务可用；确认后重新提交解析（第一轮已完成时按钮为「重试第二轮」）。",
  IMPORT_INITIAL_STAGE_UNAVAILABLE: "第一轮解析结果已不可用（缺失、损坏或解析契约已变化），请从头重新解析。",
  LLM_NETWORK_FAILED: "模型网络请求在自动重试后仍失败；确认模型服务和网络可达后再重新提交解析。",
  LLM_TIMEOUT: "模型请求超时；确认模型服务和网络可达后再重新提交解析。",
  RULE_CELL_FORMULA_UNSUPPORTED: "把公式单元格改成业务描述文字后重新上传规则库。",
  RULE_REQUIRED_CELL_EMPTY: "补齐该单元格内容后重新上传规则库。",

  // 配置编译、场景发布与历史导入发布
  IMPORT_STATUS_NOT_READY: "当前作业还没有完成规则转换，请先选择参与解析的工作表并提交解析。",
  PROPOSALS_MISSING: "规则尚未转换，请先选择参与解析的工作表并提交解析。",
  ISSUES_UNRESOLVED: "查看解析失败详情，再写入场景草稿。",
  MANIFEST_NO_MATERIAL: "在当前场景中至少配置一个材料位，再重新解析或发布。",
  MANIFEST_NO_RULE: "至少配置一条检查规则（或确认本场景只需要字段抽取），再重新解析或发布。",
  MANIFEST_NODE_RULE_MISSING: "为缺失的必需节点补充规则，再重新解析或发布。",
  MANIFEST_MATERIAL_REF_MISSING: "规则引用的材料必须在当前场景中定义，请补齐材料配置或修正引用。",
  MANIFEST_RULE_INPUT_MISSING: "补齐规则引用的材料和字段，使其与当前场景的定义一致。",
  MANIFEST_RULE_INPUT_COUNT_INVALID: "规则输入数量不符合所选算子要求，请按具体错误核对输入；多字段业务方法可使用智能校验。",
  MANIFEST_RULE_MATERIAL_MISSING: "补齐规则引用的材料范围，或修改规则的适用材料，再重新解析。",
  MANIFEST_RULE_MATERIAL_REQUIRED: "为这条材料规则写明适用的材料后重新解析。",
  MANIFEST_RISK_SOURCE_MISSING: "核对风险配置引用的来源规则或计算结果，确保它们在当前场景中存在且已启用。",
  MANIFEST_RISK_SOURCE_REQUIRED: "为风险配置声明实际依赖的审核规则或计算结果。",
  MANIFEST_RISK_REVIEW_NODE_ORDER_INVALID: "风险评定排在人工复核之后，不能再要求新增人工复核；请去掉该风险规则的人工复核要求后重新解析。",
  MANIFEST_APPLICABILITY_MATERIAL_MISSING: "为「所引用材料均已上传时」的规则补充所引用的材料，或把适用条件改为始终适用。",
  PUBLISH_PREVIEW_MISMATCH: "历史导入发布的内容与当时的检查结果不一致。新导入请写入场景草稿，再在场景页发布。",
  IMPORT_DOES_NOT_PUBLISH_SCENE: "规则库导入不能直接发布场景。请先写入该场景草稿，核对配置后在场景页执行「发布场景」。",
  IMPORT_ALREADY_APPLIED: "该导入作业已经写入场景草稿，不能再改。请到场景草稿里修改，或重新上传一份规则库。",
  IMPORT_SCENARIO_REQUIRED: "这份导入没有所属场景。请先创建场景，再在该场景下上传规则库。",
  PLAN_FROM_TASK_FORBIDDEN: "不能把审核任务另存为方案。请先选择已发布场景建立方案，再从方案创建任务。",
  MANIFEST_CALC_FROZEN_STALE: "已冻结的计算配置与当前草稿的字段编号或计算节点不一致，不能发布。请核对字段编号和计算节点后再发布。",
  IMPORT_DRAFT_REVISION_CONFLICT: "有人在本次编辑后改动了这个导入作业的草稿；请刷新页面重新加载最新草稿，再重新提交。",
  OPERATION_IDEMPOTENCY_CONFLICT: "同一提交标识对应了不同的输入内容；请刷新页面后按最新内容重新提交，不要复用旧请求。",
  INPUT_REVISION_CONFLICT: "任务输入已被其他操作更新（例如另一端替换了材料或提交了纠错）；请刷新任务详情加载最新状态后再提交。",

  // 场景草稿编辑与发布：每一条都指出具体要改哪个配置项。
  SCENARIO_DRAFT_REVISION_CONFLICT: "场景草稿在本次确认期间被改动；请刷新页面核对最新配置后再提交。",
  SCENARIO_DISABLED: "该场景已停用，不能发布或用于新建方案与任务；如需继续请先启用场景。",
  FIELD_REFERENCED: "该字段被规则引用，删除会破坏规则定义；请先改掉引用它的规则，再删除字段。",
  FIELD_TYPE_INVALID: "字段类型不在支持范围内，请从页面提供的类型里重新选择。",
  FIELD_EXTRACTION_STRATEGY_INVALID: "字段抽取策略不受支持，请重新选择。",
  FIELD_EXTRACTION_STRATEGY_TYPE_MISMATCH: "字段类型与抽取策略不匹配，请按字段类型重新选择抽取方式。",
  FIELD_STRUCTURE_SCHEMA_REQUIRED: "结构化抽取必须填写结构定义。",
  FIELD_STRUCTURE_SCHEMA_INVALID: "结构定义不合法，请检查 JSON Schema 后重新保存。",
  FIELD_STRUCTURE_SCHEMA_UNSUPPORTED: "结构定义使用了系统尚未支持的 JSON Schema 关键字。",
  FIELD_STRUCTURE_SCHEMA_ROOT_INVALID: "结构定义的根类型必须是 object。",
  FIELD_STRUCTURE_SCHEMA_PROPERTIES_REQUIRED: "结构定义必须声明至少一个属性。",
  FIELD_STRUCTURE_SCHEMA_UNEXPECTED: "普通字段不能携带结构定义，请清空后保存。",
  SECTION_FIELD_MISSING: "报告章节引用的字段不存在；请在章节里重新选择字段，或先补上该字段。",
  RULE_SYSTEM_READONLY: "字段抽取与报告生成规则由系统维护，不能在页面上改写；如需调整请改字段定义或报告章节。",
  RULE_INPUT_COUNT_INVALID: "规则输入数量不符合所选算子要求，请核对具体错误；智能校验支持多个输入字段。",
  RULE_INPUT_FIELD_MISSING: "规则引用的字段不在本场景字段列表中；请先补上字段，或在规则里改选已有的字段。",
  RULE_INTERNAL_MATERIAL_MISMATCH: "规则引用的字段与所选材料对不上；请让字段的材料与规则适用的材料保持一致。",
  RULE_MATERIAL_MISSING: "规则引用了不存在的材料；请补齐场景材料配置，或修正规则的材料引用。",
  RULE_MATERIAL_REQUIRED: "这条规则需要指定适用材料；请在规则里选择它检查哪些材料。",
  RULE_NODE_ORDER_INVALID: "规则所在节点排在人工复核之后，不能再要求人工复核；请去掉该规则的人工复核要求。",
  RULE_TYPE_INVALID: "检查方式不在支持范围内，请从页面提供的算子中重新选择。",
  RULE_APPLICABILITY_INVALID: "适用条件不在支持范围内，请选择「始终适用」或「所引用材料均已上传时」。",
  RULE_RISK_SOURCE_MISSING: "核对风险配置引用的来源规则或计算结果，确保它们存在且已启用。",
  RULE_RISK_SOURCE_REQUIRED: "风险规则缺少来源；请选择实际依赖的审核规则或计算结果。",
  RULE_RISK_SOURCE_INVALID: "风险规则的来源引用无效；请重新选择来源节点与来源审核规则。",
  RULE_RISK_SOURCE_NODE_REQUIRED: "风险规则缺少来源节点；请选择风险来自哪个检查阶段。",

  // 方案与建任务：范围与冻结相关的阻断原因。
  PLAN_RULE_EMPTY: "方案至少要显式选择一条业务规则才能生效。",
  PLAN_SCENARIO_VERSION_REQUIRED: "请先为方案选择绑定的场景版本。",
  PLAN_DISABLED: "该方案已停用，不能用于新建任务；如需继续请先启用方案。",
  SCENARIO_VERSION_NOT_FOUND: "方案绑定的场景版本已不存在；请回到方案编辑页重新保存。",
  PLAN_SCOPE_UNAVAILABLE: "该方案的范围尚未生效，不能用来创建任务；请回到方案编辑页保存成功后再试。",
  TASK_RULE_EMPTY: "任务至少要执行一条规则；请回到方案里选择规则并重新保存。",
  TASK_RULE_UNKNOWN: "任务引用了当前场景版本中不存在的规则；请回到方案编辑页重新保存以对齐场景配置。",
  TASK_RULE_DISABLED: "任务引用了场景里已停用的规则；请在场景配置中启用该规则或从方案中移除。",
  TASK_SCHEMA_FIELD_MISSING: "规则引用的字段不在场景字段集合中；请补齐字段定义后重新发布。",
  TASK_DOCUMENT_NO_CURRENT_FILE: "该材料位还没有当前文件；请先上传文件再执行需要它的节点。",
};

export function failureNextStep(code: string | null | undefined): string {
  if (!code) {
    return "如问题持续，请携带错误码和请求编号联系技术支持。";
  }
  return failureNextSteps[code] ?? "如问题持续，请携带错误码和请求编号联系技术支持。";
}
