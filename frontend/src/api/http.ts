import axios, { type AxiosRequestConfig } from "axios";

import type { ApiEnvelope, ApiFailure, OperationAccepted, OperationStatus } from "./contracts";
import { newId } from "../utils/ids";

const apiBaseUrl = import.meta.env.VITE_API_BASE_URL ?? "/api";
const REQUEST_TIMEOUT_MS = 60_000;

const http = axios.create({
  baseURL: apiBaseUrl,
  timeout: REQUEST_TIMEOUT_MS,
  headers: { Accept: undefined },
  // 所有 HTTP 响应交由统一错误契约处理，JSON 在入口显式解析，避免非法 JSON 被静默保留为字符串。
  validateStatus: () => true,
  transformResponse: [],
  transitional: { clarifyTimeoutError: true },
});

http.interceptors.request.use((config) => {
  config.headers.set("X-Request-ID", config.headers.get("X-Request-ID") || newId());
  if (!config.headers.has("Accept")) {
    config.headers.set("Accept", config.responseType === "blob" ? "application/octet-stream" : "application/json");
  }
  return config;
});

/** 后台操作状态查询间隔。 */
export const OPERATION_POLL_INTERVAL_MS = 2_000;

export class ApiError extends Error {
  readonly code: string;
  readonly details: NonNullable<ApiFailure["data"]>;
  readonly requestId: string;
  /** 没有收到 HTTP 响应时为 null。 */
  readonly statusCode: number | null;
  readonly cause: unknown;

  constructor(failure: ApiFailure, statusCode: number | null = null, cause?: unknown) {
    super(failure.message);
    this.name = "ApiError";
    this.code = failure.code;
    this.details = failure.data ?? {};
    this.requestId = failure.request_id;
    this.statusCode = statusCode;
    this.cause = cause;
  }
}

function isApiEnvelope(value: unknown): value is ApiEnvelope<unknown> {
  if (typeof value !== "object" || value === null) return false;
  const envelope = value as Record<string, unknown>;
  return typeof envelope.code === "string"
    && typeof envelope.message === "string"
    && typeof envelope.request_id === "string"
    && typeof envelope.success === "boolean"
    && "data" in envelope;
}

function isApiFailure(value: unknown): value is ApiFailure {
  if (!isApiEnvelope(value)) return false;
  return value.success === false
    && (value.data === null || (typeof value.data === "object" && !Array.isArray(value.data)));
}

function invalidResponse(statusCode: number, requestId: string, cause: unknown): ApiError {
  return new ApiError({
    code: "INVALID_RESPONSE",
    message: "服务返回了无法识别的响应",
    data: {},
    request_id: requestId,
    success: false,
  }, statusCode, cause);
}

function httpFailure(payload: unknown, statusCode: number, requestId: string): ApiError {
  if (isApiFailure(payload)) return new ApiError(payload, statusCode);
  return new ApiError({
    code: "HTTP_ERROR",
    message: `服务返回 HTTP ${statusCode}，错误响应不符合接口约定`,
    data: { status_code: statusCode },
    request_id: requestId,
    success: false,
  }, statusCode);
}

/** 统一请求入口：沿用 60 秒超时；取消、网络、HTTP 和响应格式错误分别暴露。 */
export async function request<T>(path: string, config: AxiosRequestConfig<unknown> = {}): Promise<T> {
  return requestResponse<T>(path, config, "json");
}

/** 文件请求沿用相同的超时与错误解析，错误 JSON 不能被保存成 Excel。 */
export async function requestBlob(path: string, config: AxiosRequestConfig<unknown> = {}): Promise<Blob> {
  return requestResponse<Blob>(path, config, "blob");
}

async function requestResponse<T>(path: string, config: AxiosRequestConfig<unknown>, format: "json" | "blob" | "upload"): Promise<T> {
  let response;
  try {
    response = await http.request<string | Blob>({
      ...config,
      url: path,
      responseType: format === "blob" ? "blob" : "text",
    });
  } catch (error) {
    if (!axios.isAxiosError(error)) throw error;
    const timedOut = error.code === "ETIMEDOUT" || error.code === "ECONNABORTED";
    const cancelled = axios.isCancel(error);
    const upload = format === "upload";
    throw new ApiError({
      code: timedOut ? "REQUEST_TIMEOUT" : cancelled ? "REQUEST_CANCELLED" : "NETWORK_ERROR",
      message: timedOut
        ? upload
          ? "上传超过等待时间，尚未确认操作结果；请查询材料的真实状态后再决定是否重新提交"
          : "请求超过 60 秒仍未返回，尚未确认操作结果；请查询真实状态后再决定是否重新提交"
        : cancelled
          ? upload ? "上传已取消，尚未确认操作结果" : "请求已取消，尚未确认操作结果"
          : upload ? "上传通信失败，请检查网络或服务状态" : "请求通信失败，请检查网络或服务状态",
      data: {},
      request_id: String(error.config?.headers.get("X-Request-ID") ?? ""),
      success: false,
    }, error.response?.status ?? null, error);
  }

  const statusCode = response.status;
  const responseRequestId = String(response.headers["x-request-id"] || response.config.headers.get("X-Request-ID"));
  if (statusCode === 204) {
    if (format === "blob") throw invalidResponse(statusCode, responseRequestId, null);
    return undefined as T;
  }
  const successful = statusCode >= 200 && statusCode < 300;
  if (format === "blob" && successful
    && !String(response.headers["content-type"]).includes("application/json")) {
    return response.data as T;
  }

  const content = typeof response.data === "string" ? response.data : await response.data.text();
  let payload: unknown;
  try {
    payload = JSON.parse(content);
  } catch (error) {
    if (error instanceof SyntaxError) throw invalidResponse(statusCode, responseRequestId, error);
    throw error;
  }
  if (!successful) throw httpFailure(payload, statusCode, responseRequestId);
  if (!isApiEnvelope(payload)) throw invalidResponse(statusCode, responseRequestId, payload);
  if (!payload.success) throw new ApiError(payload as ApiFailure, statusCode);
  if (format === "blob") throw invalidResponse(statusCode, responseRequestId, payload);
  return payload.data as T;
}

/** 拼出后端文件地址，供下载 / 预览使用。 */
export function apiFileUrl(path: string): string {
  return `${apiBaseUrl}${path}`;
}

/** 上传文件并回报浏览器真实发送进度，使用与普通请求一致的错误契约。 */
export function uploadWithProgress<T>(
  path: string,
  formData: FormData,
  onProgress?: (percent: number) => void,
): Promise<T> {
  return requestResponse<T>(path, {
    method: "POST",
    data: formData,
    timeout: REQUEST_TIMEOUT_MS * 5,
    onUploadProgress: (event) => {
      if (onProgress && event.total !== undefined && event.total > 0) {
        onProgress(Math.min(100, Math.round((event.loaded / event.total) * 100)));
      }
    },
  }, "upload");
}

export type OperationView = OperationAccepted;

const TERMINAL_OPERATION_STATUSES: OperationStatus[] = ["SUCCEEDED", "FAILED", "INTERRUPTED"];

/** 操作是否已进入终态（SUCCEEDED / FAILED / INTERRUPTED）。 */
export function isOperationFinished(status: OperationStatus | null | undefined): boolean {
  return status !== null && status !== undefined && TERMINAL_OPERATION_STATUSES.includes(status);
}

/** 操作是否仍在处理中（PENDING / RUNNING）。 */
export function isOperationActive(status: OperationStatus | null | undefined): boolean {
  return status === "PENDING" || status === "RUNNING";
}

/** 只有真实受理过的操作才在页面上显示为「处理中」；受理状态本身不代表业务成功。 */
export function operationToneClass(status: OperationStatus | null | undefined): "running" | "done" | "failed" {
  if (status === "SUCCEEDED") {
    return "done";
  }
  if (status === "FAILED" || status === "INTERRUPTED") {
    return "failed";
  }
  return "running";
}

/** 按操作编号查询后台操作的真实状态。 */
type OperationDetail = OperationView & { response_files: { stage: string; relative_path: string; created_at: string }[] };

export function getOperation(operationNo: string): Promise<OperationDetail> {
  return request<OperationDetail>(`/operations/${encodeURIComponent(operationNo)}`);
}

/**
 * 幂等标识与请求内容绑定：对象、操作类型、目标节点进入标识，同一次业务动作重发复用同一个
 * 后台操作，换节点或换对象不可能复用旧标识。随机后缀只用于区分用户明确发起的新一次尝试。
 */
export function operationIdempotencyKey(...parts: string[]): string {
  const scope = parts.filter((part) => part.length > 0).join("_");
  return `${scope}_${Math.random().toString(36).slice(2, 10)}`;
}

export interface OperationWatch {
  stop: () => void;
}

/**
 * 轮询后台操作的真实状态，直到完成/失败/中断。
 *
 * - 查询失败立即停止轮询并交给页面显示错误，绝不自动重新提交执行请求；
 * - onUpdate 抛错同样停止轮询并交给页面，由页面决定是否再次查询；
 * - stop() 由调用方在离开页面或组件卸载时执行，组件卸载后不再有定时器。
 */
export function watchOperation(
  operationNo: string,
  onUpdate: (operation: OperationView) => void | Promise<void>,
  onError: (error: unknown) => void,
): OperationWatch {
  let stopped = false;
  let timer: ReturnType<typeof setTimeout> | null = null;

  function stop(): void {
    stopped = true;
    if (timer !== null) {
      clearTimeout(timer);
      timer = null;
    }
  }

  async function tick(): Promise<void> {
    if (stopped) {
      return;
    }
    let operation: OperationView;
    try {
      operation = await getOperation(operationNo);
    } catch (error) {
      if (!stopped) {
        stop();
        onError(error);
      }
      return;
    }
    if (stopped) {
      return;
    }
    try {
      await onUpdate(operation);
    } catch (error) {
      stop();
      onError(error);
      return;
    }
    if (stopped || isOperationFinished(operation.status)) {
      stop();
      return;
    }
    timer = setTimeout(() => {
      void tick();
    }, OPERATION_POLL_INTERVAL_MS);
  }

  timer = setTimeout(() => {
    void tick();
  }, OPERATION_POLL_INTERVAL_MS);

  return { stop };
}
