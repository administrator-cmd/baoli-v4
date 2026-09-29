/**
 * 中文业务标签与状态色。
 *
 * 页面只显示这里的中文名称；英文状态码最多出现在「查看技术信息」区域。
 * 状态色由 statusTone() 统一决定，任何页面都不再自己判断颜色。
 */

export type StatusTone = "neutral" | "info" | "success" | "warning" | "danger";

const statusLabels: Record<string, string> = {
  PARSED: "已读取",
  OPEN: "待处理",
  CONNECTED: "已连接",
  DEGRADED: "服务降级",
  RUNNING: "服务运行中",
  UNCONFIGURED: "未配置",
  CONFIGURATION_INCOMPLETE: "配置不完整",
  CONFIGURED_UNVERIFIED: "已配置，未验证",
  EMPTY: "无数据，已跳过",
  NO_BUSINESS_DATA: "无业务数据，已跳过",
  HIDDEN: "隐藏工作表",
  EXCLUDED: "已排除",
  BOUND: "已绑定",
  PROPOSED: "已转换",
  PENDING_DECISION: "等待业务确认",
  CONFIRMED: "已确认",
  // 历史状态：导入页已不再产生 PRECHECKED（发布前检查已移除，编译与发布状态机
  // 只在场景页）。保留这条映射只为让历史作业列表能显示中文，不是可选状态。
  PRECHECKED: "检查完成（历史）",
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
  WAITING_REVIEW: "等待人工复核",
  COMPLETED: "已全部完成",
  SKIPPED: "已跳过",
  INVALIDATED: "已失效",
  WAITING_UPLOAD: "待上传",
  UPLOADED: "已上传",
  PREPARED: "已处理",
  PROCESSED: "已处理",
  RESOLVED: "已解决",
  TYPE_REVIEW: "材料类型待复核",
  TYPE_REJECTED: "材料类型不符",
  TYPE_MISMATCH: "材料类型待复核",
  SUPERSEDED: "已替换",
  PRESENT: "已抽取",
  NOT_PRESENT: "未抽取到",
  EXTRACTION_FAILED: "抽取失败",
  EVIDENCE_UNCLEAR: "证据不足",
  NORMALIZATION_FAILED: "标准化失败",
  NOT_EXECUTED: "未执行",
  REVIEWED: "已复核",
  PASSED: "通过",
  PASS: "通过",
  ABNORMAL: "异常",
  EVIDENCE_INSUFFICIENT: "证据不足",
  PENDING_REVIEW: "待复核",
  NOT_APPLICABLE: "不适用",
  COMPLETE: "已生成",
  INCOMPLETE: "未完整执行",
  NOT_GENERATED: "未生成",
  AUTO: "自动识别",
  MANUAL: "已人工处理",

  // 外部作业阶段、证据定位、依据类型、逐行处置
  PROCESSING: "处理中",
  VERIFIED: "已核实",
  UNVERIFIED: "未核实",
  UNLOCATABLE: "无法定位",
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

  // 证据索引与检索。注意：READY 在任务状态里已经表示「待执行」，索引状态不能复用这个键，
  // 因此索引状态的中文名与颜色由 evidenceIndexStatusLabel / evidenceIndexStatusTone 单独提供。
  BUILDING: "索引构建中",
  NO_INDEX: "范围内没有可用索引",
  PARTIAL_COVERAGE: "检索范围未完整覆盖",
};

/**
 * 状态色。语义固定为：
 * 绿=已完成/已通过，蓝=进行中/可执行，橙=需要人处理，红=失败或阻断，灰=未开始/已跳过/已排除。
 * 「已跳过」「未执行」「不存在风险结果」都不是绿色，避免被读成通过。
 */
const statusTones: Record<string, StatusTone> = {
  // 连接与系统
  CONNECTED: "success",
  RUNNING: "info",
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
  PENDING_DECISION: "warning",
  CONFIRMED: "success",
  PRECHECKED: "success",
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
  COMPLETED: "success",

  // 材料
  WAITING_UPLOAD: "warning",
  UPLOADED: "info",
  PREPARED: "success",
  PROCESSED: "success",
  SUPERSEDED: "neutral",
  TYPE_REVIEW: "warning",
  TYPE_REJECTED: "danger",
  TYPE_MISMATCH: "warning",

  // 字段与规则结果
  PRESENT: "success",
  NOT_PRESENT: "warning",
  EXTRACTION_FAILED: "danger",
  EVIDENCE_UNCLEAR: "warning",
  NORMALIZATION_FAILED: "danger",
  NOT_EXECUTED: "neutral",
  REVIEWED: "success",
  PASSED: "success",
  PASS: "success",
  ABNORMAL: "danger",
  EVIDENCE_INSUFFICIENT: "warning",
  PENDING_REVIEW: "warning",
  NOT_APPLICABLE: "neutral",

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

  // 证据定位与依据类型
  VERIFIED: "success",
  UNVERIFIED: "warning",
  UNLOCATABLE: "warning",
  HUMAN_STATEMENT: "info",
  DOCUMENT_EVIDENCE: "info",
  UPLOAD_RECORD: "info",

  // 管理模块：报告归档、文件移除、服务配置
  GENERATED: "success",
  ARCHIVED: "neutral",
  REMOVED: "neutral",
  ACCEPTED: "info",
  CONFIGURED: "success",
  CONFIGURED_UNVERIFIED: "warning",
  UNCONFIGURED: "neutral",
  CONFIGURATION_INCOMPLETE: "warning",
  NOT_CONFIGURED: "neutral",
  PARTIAL_COVERAGE: "warning",
  // 索引状态色由 evidenceIndexStatusTone 提供（READY 在任务状态里另有所指）。
  BUILDING: "info",
};

/** 逐行处置的中文口径。 */
const dispositionLabels: Record<string, string> = {
  CANDIDATE: "已形成候选",
  DISABLED: "已停用并保留",
  EXCLUDED: "已明确排除",
  QUESTION: "待处理问题",
};

export function dispositionLabel(disposition: string | null | undefined): string {
  return disposition ? dispositionLabels[disposition] ?? "处置未知" : "处置未知";
}

export function dispositionTone(disposition: string | null | undefined): StatusTone {
  if (disposition === "QUESTION") {
    return "warning";
  }
  if (disposition === "CANDIDATE") {
    return "info";
  }
  return "neutral";
}

/**
 * 证据定位核对状态的中文口径与颜色。
 *
 * 这是**位置**结论，不是内容结论：位置对不上不等于这段原文不存在，
 * 因此文案统一带「位置」二字，避免被读成内容有问题。
 */
const locatorStatusLabels: Record<string, string> = {
  VERIFIED: "位置已核对",
  UNVERIFIED: "位置未核实",
  UNLOCATABLE: "位置无法定位",
};

export function locatorStatusLabel(status: string | null | undefined): string {
  return status ? locatorStatusLabels[status] ?? "位置状态未知" : "位置状态未知";
}

export function locatorStatusTone(status: string | null | undefined): StatusTone {
  if (status === "VERIFIED") {
    return "success";
  }
  if (status === "UNVERIFIED" || status === "UNLOCATABLE") {
    return "warning";
  }
  return "neutral";
}

/**
 * 证据**内容依据**的核对结论：引文或字段值能否在所述材料里找到原文。
 *
 * 与位置分开：内容找不到才是真的缺依据，位置对不上只是高亮不了。
 */
const quoteCheckLabels: Record<string, string> = {
  FOUND: "已在所属材料中找到该原文",
  VALUE_FOUND: "已在处理产物中找到字段值，但引文原文无法直接定位",
  NOT_FOUND: "在所属材料中找不到该原文，缺少原文依据",
  NOT_CHECKABLE: "该产物没有可核对的原文文本，未能核对内容依据",
  PENDING: "写入时尚未按当前口径核对内容依据",
};

export function quoteCheckLabel(status: string | null | undefined): string {
  return status ? quoteCheckLabels[status] ?? "内容依据状态未知" : "内容依据状态未知";
}

export function quoteCheckTone(status: string | null | undefined): StatusTone {
  if (status === "FOUND") {
    return "success";
  }
  if (status === "NOT_FOUND") {
    return "danger";
  }
  if (status === "VALUE_FOUND" || status === "NOT_CHECKABLE" || status === "PENDING") {
    return "warning";
  }
  return "neutral";
}

/** 人工结论依据类型的中文口径。 */
const basisTypeLabels: Record<string, string> = {
  HUMAN_STATEMENT: "人工业务判断",
  DOCUMENT_EVIDENCE: "文档证据",
  UPLOAD_RECORD: "上传记录",
};

export function basisTypeLabel(basisType: string | null | undefined): string {
  return basisType ? basisTypeLabels[basisType] ?? "依据未知" : "依据未知";
}

export const basisTypeOptions = [
  { value: "HUMAN_STATEMENT", label: "人工业务判断" },
  { value: "DOCUMENT_EVIDENCE", label: "文档证据" },
  { value: "UPLOAD_RECORD", label: "上传记录" },
];

/**
 * 字段事实状态的语义色。
 *
 * 与规则结论分开：字段的 PRESENT 只表示「从材料里抽到了这段原文」，
 * 是事实存在性，不是审核通过，因此不能用绿色（绿色在本系统里固定表示通过/完成）。
 * 只有人工已经确认过的 REVIEWED 才是经过人确认的有效值。
 */
export function fieldStatusTone(status: string | null | undefined): StatusTone {
  if (status === "REVIEWED") {
    return "success";
  }
  if (status === "PRESENT") {
    return "info";
  }
  if (status === "NOT_PRESENT" || status === "EVIDENCE_UNCLEAR") {
    return "warning";
  }
  if (status === "EXTRACTION_FAILED" || status === "NORMALIZATION_FAILED") {
    return "danger";
  }
  return "neutral";
}

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
  INTERRUPT_WAITING: "等待人工复核",
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

/**
 * 候选的业务状态。与工作流状态不同名（PENDING 在节点上是「待处理」，在候选上是「待补充」），
 * 所以单独一张表，不能靠 statusLabel 猜。
 */
const candidateStateLabels: Record<string, string> = {
  AUTO: "自动识别",
  PENDING: "待补充",
  MANUAL: "已人工处理",
  EXCLUDED: "已排除",
};

export function candidateStateLabel(state: string | null | undefined): string {
  return state ? candidateStateLabels[state] ?? "状态未知" : "状态未知";
}

/** 候选处理方式对应的颜色：待补充是橙，已排除是灰，不能显示成通过。 */
export function candidateStateTone(state: string | null | undefined): StatusTone {
  if (state === "PENDING") {
    return "warning";
  }
  if (state === "MANUAL") {
    return "success";
  }
  if (state === "AUTO") {
    return "info";
  }
  return "neutral";
}

/** 后台操作类型，用于「当前步骤」而不是展示内部编码。 */
const operationTypeLabels: Record<string, string> = {
  IMPORT_CONVERT: "规则解析与转换",
  NODE_EXECUTE: "业务节点执行",
  MANUAL_REVIEW_RESUME: "人工复核恢复",
  // 管理模块：业务写操作与命令
  CREATE: "新增",
  UPDATE: "编辑",
  ENABLE: "启用",
  DISABLE: "停用",
  DELETE: "删除",
  RESTORE: "恢复",
  COPY: "复制",
  PUBLISH: "发布",
  DRAFT_SAVE: "保存方案草稿",
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
  DRAFT_SECTION_CREATE: "新增报告章节",
  DRAFT_SECTION_UPDATE: "编辑报告章节",
  DRAFT_SECTION_DELETE: "删除报告章节",
  REVISE_FROM_VERSION: "基于版本创建修订",
  APPLY_DRAFT: "导入写入场景草稿",
  IMPORT_APPLY: "规则库写入场景草稿",
  TASK_CREATE: "创建审核任务",
  SAVE_FROM_TASK: "历史任务另存为方案",
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

/** 素材/配置表单一类页面的作用域文案。 */
export function draftSourceLabel(source: string | null | undefined): string {
  const labels: Record<string, string> = {
    FORM: "页面编辑",
    EXCEL: "规则库导入",
    COPY: "复制场景",
    REVISION: "版本修订",
    IMPORT: "规则库导入",
  };
  return source ? labels[source] ?? source : "未知来源";
}

/** 版本号是技术信息：页面只在次级区域显示，不进主标题。 */
export function versionLabel(versionNo: number | null | undefined): string {
  return typeof versionNo === "number" && versionNo > 0 ? `第 ${versionNo} 版` : "版本未知";
}

/** 业务节点中文名，用于节点编码兜底展示。 */
const nodeLabels: Record<string, string> = {
  MATERIAL_CHECK: "材料检查",
  DOCUMENT_PREPARE: "文件处理",
  FIELD_EXTRACTION: "字段抽取",
  INTERNAL_VALIDATE: "文件内部校验",
  CROSS_VALIDATE: "文件交叉校验",
  MANUAL_REVIEW: "人工复核",
  RISK_RATING: "风险评定",
  REPORT_OUTPUT: "报告输出",
};

export function nodeLabel(nodeCode: string | null | undefined): string {
  return nodeCode ? nodeLabels[nodeCode] ?? "未知节点" : "未知节点";
}

/** 节点在业务上的用途，执行前让用户知道这一步在做什么。 */
const nodePurposes: Record<string, string> = {
  MATERIAL_CHECK: "核对必传材料是否齐全，缺件会在这里被指出。",
  DOCUMENT_PREPARE: "对已上传材料做文件处理和文字识别，产出可供审核的处理结果。",
  FIELD_EXTRACTION: "从材料中抽取模板定义的业务字段，并保留原文证据。",
  INTERNAL_VALIDATE: "按模板配置对单份材料内部的信息做一致性检查。",
  CROSS_VALIDATE: "按模板配置在多份材料之间做交叉核对。",
  MANUAL_REVIEW: "对系统无法自动判定的项给出人工结论。",
  RISK_RATING: "按模板配置的风险规则评定风险等级。",
  REPORT_OUTPUT: "汇总本次执行的字段、规则和风险结果，生成审核报告。",
};

export function nodePurpose(nodeCode: string | null | undefined): string {
  return nodeCode ? nodePurposes[nodeCode] ?? "按模板配置执行本节点。" : "按模板配置执行本节点。";
}

/**
 * 节点状态 → 中文口径。
 *
 * 「已跳过」「未执行」与「已完成」必须分开：跳过表示本模板没有配置这类检查，
 * 它是一次真实执行给出的结论，但绝不是通过。
 */
const nodeStatusLabels: Record<string, string> = {
  CURRENT: "当前节点",
  PENDING: "未开始",
  READY: "待执行",
  RUNNING: "执行中",
  SUCCEEDED: "已完成",
  SKIPPED: "已跳过（本模板未配置此类检查）",
  FAILED: "执行失败",
  BLOCKED: "已阻断",
  INVALIDATED: "已失效（需重新执行）",
};

export function nodeStatusLabel(status: string | null | undefined): string {
  if (!status) {
    return "未开始";
  }
  return nodeStatusLabels[status] ?? statusLabel(status);
}

/** 节点状态 → 语义色。跳过与失效是灰，不能与绿色的「已完成」同色。 */
export function nodeStatusTone(status: string | null | undefined): StatusTone {
  if (status === "SUCCEEDED") {
    return "success";
  }
  if (status === "FAILED" || status === "BLOCKED") {
    return "danger";
  }
  if (status === "SKIPPED" || status === "INVALIDATED") {
    return "neutral";
  }
  if (status === "RUNNING" || status === "CURRENT" || status === "READY") {
    return "info";
  }
  return "neutral";
}

/** 规则适用条件：空白代表「始终适用」，不是「未配置」。 */
const applicabilityLabels: Record<string, string> = {
  ALWAYS: "始终适用",
  MATERIALS_UPLOADED: "所引用材料均已上传时",
};

export function applicabilityLabel(kind: string | null | undefined): string {
  return kind ? applicabilityLabels[kind] ?? "未支持的适用条件" : "始终适用";
}

/**
 * 只有存在真实可数总量时才返回百分比；否则返回 null，页面不显示百分比。
 */
export function progressPercent(done: number | null | undefined, total: number | null | undefined): number | null {
  if (typeof done !== "number" || typeof total !== "number" || !Number.isFinite(done) || !Number.isFinite(total)) {
    return null;
  }
  if (total <= 0 || done < 0) {
    return null;
  }
  return Math.min(100, Math.round((Math.min(done, total) / total) * 100));
}

/** 规则节点分批：创建任务时按业务分组勾选规则，不直接暴露内部编码。 */
export const ruleNodeOrder = [
  "INTERNAL_VALIDATE",
  "CROSS_VALIDATE",
  "RISK_RATING",
];

/**
 * base Excel 的 10 个业务 Sheet 用途。
 *
 * 中文名与模板里的工作表名称一致，用户可以直接对照 Excel 里的标签页填写；
 * 模板自带的说明页等元数据不是业务用途，绝不出现在这里。
 *
 * **服务端协议才是权威来源**：绑定步骤优先用 `/rulebook-templates/sheets` 返回的
 * `display_name`（见 profileSheetLabels），这份清单只作为协议不可达时的兜底。
 */
export const sheetRoleOptions = [
  { value: "MATERIAL_LIST", label: "01_材料位" },
  { value: "MATERIAL_QUALITY", label: "02_文件处理规则" },
  { value: "FIELD_EXTRACTION", label: "03_字段" },
  { value: "INTERNAL_VALIDATE", label: "04_文件内部校验规则" },
  { value: "CROSS_VALIDATE", label: "05_文件交叉校验规则" },
  { value: "CALC_DEFINITION", label: "06_计算定义" },
  { value: "CALC_PARAMETER", label: "07_计算参数" },
  { value: "CALC_JUDGEMENT", label: "08_计算判定" },
  { value: "RISK", label: "09_风险规则" },
  { value: "REPORT_TEMPLATE", label: "10_报告模板" },
];

/**
 * 非业务用途的中文名。
 *
 * 旧格式模板可能返回 INSTRUCTION（填写说明页）。它属于模板元数据，不能作为业务用途发布，
 * 但必须给出中文说明，避免页面上出现原始编码。
 */
const nonBusinessSheetRoleLabels: Record<string, string> = {
  INSTRUCTION: "填写说明（模板元数据，不参与本次导入）",
};

/** 工作表用途的业务说明，帮助用户判断这张表该绑成什么。 */
const sheetRoleDescriptions: Record<string, string> = {
  MATERIAL_LIST: "定义本次审核需要登记的材料位，以及哪些是必传。",
  MATERIAL_QUALITY: "定义对材料文件本身的检查项目、合格要求与判定口径。",
  FIELD_EXTRACTION: "定义需要从材料中抽取的业务字段、类型、单位、币种、取数期间与报表口径。",
  INTERNAL_VALIDATE: "定义单份材料内部信息的一致性检查规则及允许差值。",
  CROSS_VALIDATE: "定义两种材料之间的交叉核对规则及允许差值。",
  CALC_DEFINITION: "定义计算规则：指标名称、类别、计算公式、结果期间与报表口径。",
  CALC_PARAMETER: "定义公式里每个标识符的取值来源（字段 / 上游计算结果 / 常量）、单位与币种。",
  CALC_JUDGEMENT: "定义计算结果的判定区间、业务结论及对应的通过、异常或待复核状态。",
  RISK: "定义风险及其等级、成立条件（任一/全部关联检查不合格）；来源只在检查行的「关联风险」维护。",
  REPORT_TEMPLATE: "定义报告编号、材料范围、输出格式与章节；同一报告编号的属性必须整组一致。",
  INSTRUCTION: "模板自带的填写说明页，属于模板元数据，不参与本次导入。",
};

export function sheetRoleDescription(role: string | null | undefined): string {
  return role ? sheetRoleDescriptions[role] ?? "该用途暂无业务说明。" : "尚未指定用途。请选择用途，或把本表设为不参与本次导入。";
}

/**
 * 风险疑点（06_风险疑点）的业务列名。
 *
 * 来源节点与来源审核规则的中文文案由服务端返回（source_node_label / source_rule_name），
 * 前端只负责列名，不自行推断来源节点的中文名。
 */
export const riskSourceNodeColumnLabel = "来源节点";
export const riskSourceRuleColumnLabel = "来源审核规则";

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

export function sheetRoleLabel(role: string | null | undefined): string {
  if (!role) {
    return "未指定用途";
  }
  return (
    sheetRoleOptions.find((option) => option.value === role)?.label ??
    nonBusinessSheetRoleLabels[role] ??
    "未识别的用途"
  );
}

const valueTypeLabels: Record<string, string> = {
  STRING: "文本",
  DECIMAL: "金额",
  DECIMAL_CN: "大写金额",
  DATE: "日期",
  BOOLEAN: "是否",
  CLAUSE: "条款",
};

export function valueTypeLabel(valueType: string | null | undefined): string {
  return valueType ? valueTypeLabels[valueType] ?? "未指定类型" : "未指定类型";
}

/** 人工复核的选项：全部使用业务语言，不出现内部编码。 */
const reviewOptionLabels: Record<string, string> = {
  CONFIRM_FIELD: "确认系统抽取的字段值",
  MODIFY_FIELD: "修改为人工确认的字段值",
  MARK_NOT_PRESENT: "确认字段在文档中不存在",
  CONFIRM_ABNORMAL: "确认异常",
  MARK_NORMAL: "判定为正常",
  EVIDENCE_INSUFFICIENT: "证据不足，无法判定",
};

export function reviewOptions(reviewType: string): Array<{ value: string; label: string }> {
  const values =
    reviewType === "FIELD_CONFIRMATION"
      ? ["CONFIRM_FIELD", "MODIFY_FIELD", "MARK_NOT_PRESENT", "EVIDENCE_INSUFFICIENT"]
      : ["CONFIRM_ABNORMAL", "MARK_NORMAL", "EVIDENCE_INSUFFICIENT"];
  return values.map((value) => ({ value, label: reviewOptionLabels[value] }));
}

/** 人工复核的业务类型说明。 */
export function reviewTypeLabel(reviewType: string | null | undefined): string {
  if (reviewType === "FIELD_CONFIRMATION") {
    return "字段确认";
  }
  if (reviewType === "RULE_RESULT") {
    return "规则结论复核";
  }
  if (reviewType === "MATERIAL_TYPE_MISMATCH") {
    return "材料类型复核";
  }
  return "人工复核";
}

/**
 * 人工结论的中文口径。多处页面共用一份，避免各写一份后出现同码不同词。
 * 未裁决返回「未裁决」，不返回空串——空串会被读成「没有这一项」。
 */
const reviewDecisionLabels: Record<string, string> = {
  CONFIRM_ABNORMAL: "确认异常",
  MARK_NORMAL: "判定正常",
  EVIDENCE_INSUFFICIENT: "证据不足",
  CONFIRM_FIELD: "确认字段",
  MODIFY_FIELD: "修改字段",
  MARK_NOT_PRESENT: "确认字段不存在",
};

export function reviewDecisionLabel(decision: string | null | undefined): string {
  if (!decision) {
    return "未裁决";
  }
  return reviewDecisionLabels[decision] ?? `未识别结论（${decision}）`;
}

/**
 * 复核对象的业务类型。服务端的 object_kind 是稳定契约值，这里只做中文映射；
 * 未知取值必须显出原始码，不能笼统写成「材料」——那会把新增对象类型说成材料。
 */
export function reviewObjectKindLabel(kind: string | null | undefined): string {
  if (kind === "FIELD") {
    return "字段事实";
  }
  if (kind === "RULE") {
    return "规则判断";
  }
  if (kind === "MATERIAL") {
    return "材料";
  }
  return kind ? `未识别类型（${kind}）` : "未识别类型";
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

export function formatDate(value: string): string {
  const date = new Date(value);
  return Number.isNaN(date.getTime())
    ? value
    : new Intl.DateTimeFormat("zh-CN", { dateStyle: "medium", timeStyle: "short" }).format(date);
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

/** 系统状态卡片：外部服务与配置项的中文名称。 */
const externalServiceDescriptions: Record<string, string> = {
  大模型: "用于规则转换、字段抽取和智能校验。",
  材料识别: "用于核对上传材料是否符合模板要求。",
  文件解析: "用于解析材料文件并产出可审核的处理结果。",
  文字识别: "用于从扫描件中识别文字内容。",
  印章识别: "用于识别合同印章。",
};

export function externalServiceDescription(name: string): string {
  return externalServiceDescriptions[name] ?? "外部服务。";
}

const configurationItemLabels: Record<string, string> = {
  BAOLI_LLM_BASEURL: "大模型接口地址",
  BAOLI_LLM_APIKEY: "大模型访问密钥",
  BAOLI_LLM_MODEL_NAME: "大模型名称",
  BAOLI_SERVICE_8018_URL: "材料识别服务地址",
  BAOLI_SERVICE_8010_URL: "文件解析服务地址",
  BAOLI_OCR_SERVICE_URL: "文字识别服务地址",
  BAOLI_OCR_TOKEN: "文字识别服务令牌",
  BAOLI_OCR_MODEL: "文字识别模型名称",
  BAOLI_SEAL_SERVICE_URL: "印章识别服务地址",
};

export function configurationItemLabel(item: string): string {
  return configurationItemLabels[item] ?? item;
}

/** 业务节点的可选值，用于候选原地修正里选择「所属节点」。 */
export const nodeOptions = Object.keys(nodeLabels).map((value) => ({ value, label: nodeLabels[value] }));

/**
 * 规则参数的可选值。与后端 manifest.validate_rule_parameters 支持的取值保持一致：
 * 不在这些集合里的值会被后端拒绝，页面不提供自由输入以免产生无效参数。
 */
export const valueTypeOptions = [
  { value: "STRING", label: "文本" },
  { value: "DECIMAL", label: "金额" },
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

export function extractionStrategySupports(strategy: string, valueType: string): boolean {
  if (strategy === "STRUCTURED") return ["STRING", "CLAUSE"].includes(valueType);
  if (strategy === "RAW_TEXT") return ["STRING", "CLAUSE", "DECIMAL_CN"].includes(valueType);
  return strategy === "DETERMINISTIC" && ["DECIMAL", "DATE", "BOOLEAN"].includes(valueType);
}

export const artifactTypeOptions = [
  { value: "MATERIAL_CLASSIFY", label: "材料识别产物" },
  { value: "DOCUMENT_PARSE", label: "文件解析产物" },
  { value: "OCR", label: "文字识别产物" },
  { value: "SEAL", label: "印章识别产物" },
];

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
 * 计算参数按这两项定位字段事实。取值与统一模板协议里的 `value_labels` 同一口径，
 * 因此页面与 Excel 下拉看到的是同一套中文名。
 */
export const periodScopeOptions = [
  // 「不适用」是**显式取值**，不是留空的占位：合同场景没有期间概念，
  // 该字段声明为不适用后，计算参数不能拿它定位事实（发布期会拦）。
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

/**
 * 风险等级选项：旧格式用「高/中/低/待确认」，节点版用「最高/一级/二级/三级」。
 * 两套标签都是已确认的业务取值，页面必须都能选；PM 原「高/低」与四级标签之间的
 * 对应关系仍未确认，因此不在这里提供自动映射。
 */
export const riskLevelOptions = [
  { value: "最高", label: "最高" },
  { value: "一级", label: "一级" },
  { value: "二级", label: "二级" },
  { value: "三级", label: "三级" },
  { value: "高", label: "高（旧格式）" },
  { value: "中", label: "中（旧格式）" },
  { value: "低", label: "低（旧格式）" },
  { value: "待确认", label: "待确认" },
];

export const decimalCompareOperatorOptions = [
  { value: "EQ", label: "等于" },
  { value: "GTE", label: "大于或等于" },
  { value: "LTE", label: "小于或等于" },
  { value: "ABS_DIFF_LTE", label: "差额不超过阈值" },
];

/** 报告输出格式。生成 PDF/Word/Excel 文件本轮未实现，这里维护的是配置目标格式。 */
export const reportFormatOptions = [
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

/** 加总比较的方式：加法只用于数值的等于、不大于、不小于。 */
export const decimalSumCompareOperatorOptions = [
  { value: "EQ", label: "等于" },
  { value: "GTE", label: "不小于" },
  { value: "LTE", label: "不大于" },
];

export const dateCompareOperatorOptions = [
  { value: "EQ", label: "等于" },
  { value: "GTE", label: "不早于" },
  { value: "LTE", label: "不晚于" },
  { value: "GT", label: "晚于" },
  { value: "LT", label: "早于" },
];

export const riskSourceNodeOptions = [
  { value: "MATERIAL_QUALITY", label: "材料质量" },
  { value: "INTERNAL_VALIDATE", label: "文件内部校验" },
  { value: "CROSS_VALIDATE", label: "文件交叉校验" },
];

export const riskConditionOptions = [
  { value: "ANY_ABNORMAL", label: "任一来源检查不合格" },
  { value: "ALL_ABNORMAL", label: "全部来源检查不合格" },
];

export const checkLevelOptions = [
  { value: "最高", label: "最高" },
  { value: "一级", label: "一级" },
  { value: "二级", label: "二级" },
  { value: "三级", label: "三级" },
  { value: "高", label: "高（旧格式）" },
  { value: "低", label: "低（旧格式）" },
];

/**
 * 规则算子的**兜底**中文名。
 *
 * 页面显示检查方式时优先用后端能力目录返回的标签（`/system/capabilities` 的 `label`），
 * 这里只在目录标签取不到时兜底，或用于纯展示的历史文案映射。
 * 它**不用于**下拉选项：选项一律来自能力目录，本地名单与后端漂移过一次
 * （后端 14 个算子、页面只有 7 个），页面上打开一条已导入的规则会找不到它的当前取值。
 */
export const ruleOperatorOptions = [
  { value: "FILE_QUALITY_CHECK", label: "文件质量检查" },
  { value: "LLM_EXTRACT", label: "大模型抽取" },
  { value: "FIELD_EQUAL", label: "字段相等" },
  { value: "FIELD_NOT_EQUAL", label: "字段不等" },
  { value: "DATE_COMPARE", label: "日期比较" },
  { value: "DECIMAL_COMPARE", label: "金额比较" },
  { value: "DECIMAL_SUM_COMPARE", label: "金额加总比较" },
  { value: "DECIMAL_CN_EQUAL", label: "大小写金额一致性" },
  { value: "FIELD_NOT_EMPTY", label: "字段非空校验" },
  { value: "CLAUSE_ABSENT", label: "条款不应存在" },
  { value: "LLM_VALIDATE", label: "大模型语义校验" },
  { value: "RISK_TRIGGER", label: "风险触发" },
  { value: "REPORT_RENDER", label: "报告渲染" },
];

/** 业务错误码 → 用户能执行的下一步。未收录的错误码只显示原始原因和错误码。 */
const failureNextSteps: Record<string, string> = {
  NETWORK_ERROR: "检查后端服务是否已启动，以及本机网络是否可达。",
  REQUEST_TIMEOUT: "已提交的后台操作不会丢失，可稍后重新查询状态；不要重复提交。",
  OPERATION_ALREADY_ACTIVE: "该对象已有正在执行的后台操作，请等待其结束或用「再次查询」查看真实状态。",
  INVALID_RESPONSE: "服务返回的内容无法识别，请查看后端日志确认接口版本是否一致。",
  MANUAL_REVIEW_NOT_WAITING: "任务当前不在人工复核等待状态，请先执行到人工复核节点。",
  TASK_DOCUMENT_REPLACE_CONFIRMATION_REQUIRED: "该材料位已有文件，替换上传需要先明确确认替换。",
  TASK_DOCUMENT_LOCKED: "任务已结束，材料不可再替换；如需继续请新建审核任务。",
  AUDIT_TASK_RUNNING: "任务正在执行当前节点，等待执行结束后再替换材料。",
  BLOCKED_BY_PRECHECK: "先处理发布检查中列出的阻断项，再重新执行发布检查。",

  // 工作表绑定：未知工作表、用途缺失或表头行无效
  SHEET_ROLE_REQUIRED: "在「Sheet 绑定」步骤为这张工作表选择用途，或把它设为不参与本次导入。",
  SHEET_ROLE_INVALID: "该用途不在当前模板支持的范围内，请重新选择用途后再次提交解析。",
  SHEET_BINDING_INCOMPLETE: "本次作业的每一张工作表都要提交绑定结果，请刷新页面后重新确认用途。",
  SHEET_BINDING_DUPLICATE: "同一张工作表被提交了两次，请刷新页面后重新确认用途。",
  SHEET_NOT_FOUND: "该工作表已不在当前导入作业中，请刷新页面后重新确认用途。",
  SHEET_HEADER_REQUIRED: "为这张业务工作表填写实际存在的表头行（列名所在行）。",
  SHEET_HEADER_INVALID: "所选表头行不在可转换范围内，请按文件实际情况重新填写表头行。",
  TEMPLATE_METADATA_IMMUTABLE: "填写说明等模板元数据工作表不能绑定为业务工作表，请把它设为不参与本次导入。",
  NO_BUSINESS_DATA_SHEET_IMMUTABLE: "这张工作表没有业务数据，系统已跳过，不能绑定为业务用途。",

  // 规则填写超出系统支持范围
  RULE_CAPABILITY_UNSUPPORTED: "把它改成两个明确字段之间的一对一比较，或拆成多条规则：系统不会取第一份文件代替，也不会让大模型估算汇总。",
  RETRIEVAL_SCOPE_EMPTY: "本次检索没有明确材料范围。请在任务的字段结果里确认材料是否已上传并完成文件处理，再重新检索。",
  EMBEDDING_MODEL_UNCONFIGURED: "本机没有配置本地嵌入模型路径。需要检索取证的规则无法执行；请配置 BAOLI_EMBEDDING_MODEL_PATH 后重新执行文件处理节点。",
  EMBEDDING_MODEL_MISSING: "本地嵌入模型目录不存在。请在部署机上确认模型已下载到配置的路径，然后重新执行文件处理节点。",
  EMBEDDING_MODEL_INCOMPLETE: "本地嵌入模型目录缺少必要文件（权重或 tokenizer）。请补齐模型后重新执行文件处理节点。",
  EMBEDDING_DEPENDENCY_MISSING: "本机缺少运行本地 Embedding 所需的依赖。请按交付说明安装后重新执行文件处理节点。",
  EMBEDDING_INFERENCE_FAILED: "本地嵌入模型推理失败，索引或检索未完成。请检查部署机资源与模型文件后重试。",
  EVIDENCE_INDEX_MISSING: "本次范围内还没有可检索的索引。请先执行文件处理节点建立索引；索引为空不等于合同不存在该条款。",
  RULE_CELL_FORMULA_UNSUPPORTED: "把公式单元格改成业务描述文字后重新上传规则库。",
  RULE_REQUIRED_CELL_EMPTY: "补齐该单元格内容后重新上传规则库。",

  // 发布检查与发布
  IMPORT_STATUS_NOT_READY: "当前作业还没有完成规则转换，请先确认工作表用途并提交解析。",
  PROPOSALS_MISSING: "规则尚未转换，请先提交解析，再执行发布检查。",
  ISSUES_UNRESOLVED: "先为每一项业务问题提交业务结论，再重新执行发布检查。",
  MANIFEST_NO_MATERIAL: "在 01_材料清单里至少配置一个材料位，再重新执行发布检查。",
  MANIFEST_NO_RULE: "至少配置一条检查规则（或确认本模板只需要字段抽取），再重新执行发布检查。",
  MANIFEST_NODE_RULE_MISSING: "为缺失的必需节点补充规则，再重新执行发布检查。",
  MANIFEST_MATERIAL_REF_MISSING: "把 02～06 中引用的材料名称改成 01_材料清单里已有的材料，再重新解析。",
  MANIFEST_RULE_INPUT_MISSING: "补齐规则引用的材料和字段，使其与 01/03 中的定义一致，再重新解析。",
  MANIFEST_RULE_INPUT_COUNT_INVALID: "一条规则只比较左右各一个字段，请把多字段的关系拆成多条规则后重新解析。",
  MANIFEST_RULE_MATERIAL_MISSING: "补齐规则引用的材料范围，或修改规则的适用材料，再重新解析。",
  MANIFEST_RULE_MATERIAL_REQUIRED: "为这条材料规则写明适用的材料后重新解析。",
  MANIFEST_RISK_SOURCE_MISSING: "在 06_风险疑点里填写来源节点与来源审核规则，并确保它们指向同一条 02/04/05 规则。",
  MANIFEST_RISK_SOURCE_REQUIRED: "在 06_风险疑点的「来源审核规则」列填写被引用的审核规则名称，再重新解析。",
  MANIFEST_RISK_REVIEW_NODE_ORDER_INVALID: "风险评定排在人工复核之后，不能再要求新增人工复核；请去掉该风险规则的人工复核要求后重新解析。",
  MANIFEST_APPLICABILITY_MATERIAL_MISSING: "为「所引用材料均已上传时」的规则补充所引用的材料，或把适用条件改为始终适用。",
  PUBLISH_PRECHECK_FAILED: "这是历史导入发布接口留下的阻断。新导入请写入场景草稿，再在场景页按发布检查处理。",
  PUBLISH_PRECHECK_REQUIRED: "这是历史导入发布接口的结果。新导入不能从这里发布，请写入场景草稿后再到场景页发布。",
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
  RULE_INPUT_COUNT_INVALID: "一条规则只比较左右各一个字段；请把多字段关系拆成多条规则。",
  RULE_INPUT_FIELD_MISSING: "规则引用的字段不在本场景字段列表中；请先补上字段，或在规则里改选已有的字段。",
  RULE_INTERNAL_MATERIAL_MISMATCH: "规则引用的字段与所选材料对不上；请让字段的材料与规则适用的材料保持一致。",
  RULE_MATERIAL_MISSING: "规则引用了不存在的材料；请在 01_材料清单里补上该材料，或改掉规则的适用材料。",
  RULE_MATERIAL_REQUIRED: "这条规则需要指定适用材料；请在规则里选择它检查哪些材料。",
  RULE_NODE_ORDER_INVALID: "规则所在节点排在人工复核之后，不能再要求人工复核；请去掉该规则的人工复核要求。",
  RULE_TYPE_INVALID: "检查方式不在支持范围内，请从页面提供的算子中重新选择。",
  RULE_APPLICABILITY_INVALID: "适用条件不在支持范围内，请选择「始终适用」或「所引用材料均已上传时」。",
  RULE_RISK_SOURCE_MISSING: "风险规则必须写明来源节点与来源审核规则，且两者要指向同一条校验规则。",
  RULE_RISK_SOURCE_REQUIRED: "风险规则缺少来源审核规则；请选择它要判定的那条校验规则。",
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
  TASK_CREATE_REQUIRES_PLAN: "新审核任务必须从已启用的方案创建；请先基于已发布场景建立方案，再从方案创建任务。",
  TASK_DOCUMENT_NO_CURRENT_FILE: "该材料位还没有当前文件；请先上传文件再执行需要它的节点。",
};

/**
 * 方案未选到任何业务规则时的提示。
 *
 * 与后端 PLAN_RULE_EMPTY 的文案同口径（显式选择 / 业务规则），
 * 页面不得另造“显式规则”之类的说法。
 */
export const planRuleEmptyHint = failureNextSteps.PLAN_RULE_EMPTY;

export function failureNextStep(code: string | null | undefined): string {
  if (!code) {
    return "如问题持续，请携带错误码和请求编号联系技术支持。";
  }
  return failureNextSteps[code] ?? "如问题持续，请携带错误码和请求编号联系技术支持。";
}


/**
 * 证据索引状态的中文口径。
 *
 * 「可检索」只表示索引已构建成功，不表示证据充分或检查通过；
 * 「构建失败」与「索引为空」是两件事，页面不能混为一谈。
 */
const evidenceIndexStatusLabels: Record<string, string> = {
  BUILDING: "索引构建中",
  READY: "可检索",
  FAILED: "索引构建失败",
  INVALIDATED: "已失效（不再适用于当前上传版本）",
};

export function evidenceIndexStatusLabel(status: string | null | undefined): string {
  return status ? evidenceIndexStatusLabels[status] ?? "索引状态未知" : "索引状态未知";
}

export function evidenceIndexStatusTone(status: string | null | undefined): StatusTone {
  if (status === "READY") {
    return "success";
  }
  if (status === "FAILED") {
    return "danger";
  }
  if (status === "BUILDING") {
    return "info";
  }
  return "neutral";
}

/**
 * 页号来源的中文口径。
 *
 * 产物没有真实页码时只能显示产物内位置；合成序号与图片序号都不等于原 PDF 页码，
 * 页面必须如实说明，不能让用户以为那是真实页码。
 */
export function pageNoSourceLabel(source: string | null | undefined): string {
  if (source === "PROVIDER") {
    return "产物给出的真实页号";
  }
  if (source === "SYNTHETIC_SEQUENCE") {
    return "产物内序号（原 PDF 页码未提供）";
  }
  return "没有页码信息";
}

/**
 * 主体核对结论的候选取值。
 *
 * 只有这三种：相似度不能代替审核结论，本系统不提供“采纳前几条候选”的入口。
 */
export const retrievalDecisionOptions = [
  { value: "CONSISTENT", label: "一致" },
  { value: "INCONSISTENT", label: "不一致" },
  { value: "EVIDENCE_INSUFFICIENT", label: "证据不足" },
] as const;

/** 取证方式与判定方式的中文口径：让用户一眼看懂这条检查是怎么判的。 */
const evidenceRouteLabels: Record<string, string> = {
  DIRECT_FACTS: "直接读取结构化字段",
  // 只对印章识别产物的主体名称建向量：不是合同全文检索，也不是条款检索。
  SEAL_SUBJECT_RETRIEVAL: "用待核对主体检索印章主体",
  TASK_RETRIEVAL: "在材料中检索相关证据",
};

const judgeRouteLabels: Record<string, string> = {
  PROGRAM: "由程序判定",
  LLM: "由大模型按规则分析",
  HUMAN_REVIEW: "需要人工裁决",
};

export function evidenceRouteLabel(evidence: string | null | undefined): string {
  return evidence ? evidenceRouteLabels[evidence] ?? "取证方式未知" : "取证方式未记录";
}

export function judgeRouteLabel(judge: string | null | undefined): string {
  return judge ? judgeRouteLabels[judge] ?? "判定方式未知" : "判定方式未记录";
}

export function reportContentTypeLabel(contentType: string | null | undefined): string {
  const labels: Record<string, string> = {
    FIELD_DETAIL: "字段明细",
    AUDIT_RESULT: "审核结果",
    FIELD_AND_RESULT: "字段及审核结果",
    RISK_LIST: "风险清单",
    CONCLUSION: "审核结论",
  };
  return contentType ? labels[contentType] ?? "内容类型未知" : "内容类型未知";
}
