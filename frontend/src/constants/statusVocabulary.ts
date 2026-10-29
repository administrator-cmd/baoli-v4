import { reactive } from "vue";

import type { StatusVocabularyTerm } from "@/api/contracts";
import { getStatusVocabulary } from "@/api/system";

import type { StatusTone } from "./labels";

/**
 * 审核状态词表的唯一前端入口。
 *
 * 文案、分档与说明全部来自后端 `/system/status-vocabulary`；本文件只做缓存与查询，
 * 不再维护第二份中文表。词表没有的码必须显示原始码，不能补一个看似正常的中文。
 */
export type StatusDomain = "rule" | "risk" | "field";

interface VocabularyState {
  loaded: boolean;
  error: string | null;
  terms: Record<string, Record<string, StatusVocabularyTerm>>;
  categories: Record<string, string>;
}

const state = reactive<VocabularyState>({
  loaded: false,
  error: null,
  terms: {},
  categories: {},
});

/** 词表读取失败后再次取词的重试间隔；避免每个状态标签都发一次请求。 */
const RETRY_INTERVAL_MS = 5000;

let inflight: Promise<void> | null = null;
let lastAttemptAt = 0;

/** 读取并缓存词表。失败时保留错误并抛出，由调用方显式记录，不静默降级。 */
export function loadStatusVocabulary(): Promise<void> {
  if (inflight) return inflight;
  lastAttemptAt = Date.now();
  inflight = fetchVocabulary().finally(() => {
    inflight = null;
  });
  return inflight;
}

async function fetchVocabulary(): Promise<void> {
  try {
    const response = await getStatusVocabulary();
    const terms: Record<string, Record<string, StatusVocabularyTerm>> = {};
    for (const domain of response.domains) {
      const byCode: Record<string, StatusVocabularyTerm> = {};
      for (const term of domain.statuses) {
        byCode[term.code] = term;
      }
      terms[domain.domain] = byCode;
    }
    state.terms = terms;
    state.categories = Object.fromEntries(
      response.categories.map((item) => [item.category, item.label]),
    );
    state.loaded = true;
    state.error = null;
  } catch (error) {
    state.error = error instanceof Error ? error.message : "未知原因";
    throw error;
  }
}

export function statusVocabularyError(): string | null {
  return state.error;
}

function statusTerm(
  domain: StatusDomain,
  code: string | null | undefined,
): StatusVocabularyTerm | null {
  if (!code) return null;
  const term = state.terms[domain]?.[code] ?? null;
  if (term) return term;
  // 启动时读取失败会让所有状态一直显示原始码；这里按间隔自动重试，
  // 成功后响应式缓存更新，页面无需刷新即可恢复中文文案。
  if (!state.loaded && Date.now() - lastAttemptAt >= RETRY_INTERVAL_MS) {
    void loadStatusVocabulary().catch(() => undefined);
  }
  return null;
}

/** 状态中文；没有状态时显示未开始，词表外的码连同原始码一起显示。 */
export function statusTextOf(domain: StatusDomain, code: string | null | undefined): string {
  if (!code) return "未开始";
  return statusTerm(domain, code)?.label ?? `未识别状态（${code}）`;
}

export function statusCategoryOf(
  domain: StatusDomain,
  code: string | null | undefined,
): string | null {
  if (!code) return null;
  return statusTerm(domain, code)?.category ?? null;
}

export function categoryLabel(category: string | null | undefined): string | null {
  if (!category) return null;
  return state.categories[category] ?? null;
}

export function statusNoteOf(
  domain: StatusDomain,
  code: string | null | undefined,
): string | null {
  if (!code) return null;
  return statusTerm(domain, code)?.note ?? null;
}

/**
 * 状态色。它是展示层约定，不属于后端词表：
 * 绿=通过/已确认，蓝=进行中/事实存在，橙=需要处理，红=异常或失败，灰=未开始/不适用。
 */
const TONES: Record<StatusDomain, Record<string, StatusTone>> = {
  rule: {
    PASSED: "success",
    ABNORMAL: "danger",
    COMPUTED: "info",
    NOT_APPLICABLE: "neutral",
    EVIDENCE_INSUFFICIENT: "warning",
    NOT_EXECUTED: "neutral",
    PENDING_REVIEW: "warning",
    REVIEWED: "success",
    RUNNING: "info",
    SKIPPED: "neutral",
  },
  risk: {
    PASSED: "success",
    ABNORMAL: "danger",
    COMPUTED: "info",
    NOT_APPLICABLE: "neutral",
    EVIDENCE_INSUFFICIENT: "warning",
    NOT_EXECUTED: "neutral",
    PENDING_REVIEW: "warning",
    REVIEWED: "success",
    RUNNING: "info",
    SKIPPED: "neutral",
  },
  field: {
    PRESENT: "info",
    NOT_PRESENT: "warning",
    EVIDENCE_INSUFFICIENT: "warning",
    EVIDENCE_UNCLEAR: "warning",
    REVIEWED: "success",
    EXTRACTION_FAILED: "danger",
    NORMALIZATION_FAILED: "danger",
    PENDING: "neutral",
    NOT_EXECUTED: "neutral",
  },
};

export function statusToneOf(
  domain: StatusDomain,
  code: string | null | undefined,
): StatusTone {
  if (!code) return "neutral";
  return TONES[domain][code] ?? "neutral";
}

/** 词表是否已加载；筛选项需要在未加载时保留原始码。 */
export function isStatusVocabularyLoaded(): boolean {
  return state.loaded;
}
