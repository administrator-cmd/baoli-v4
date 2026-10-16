import type { ApiFailure, OperationAccepted, OperationStatus } from "./contracts";

const apiBaseUrl = import.meta.env.VITE_API_BASE_URL ?? "/api";
const REQUEST_TIMEOUT_MS = 60_000;

/** 后台操作状态查询间隔。 */
export const OPERATION_POLL_INTERVAL_MS = 2_000;

export class ApiError extends Error {
  readonly code: string;
  readonly details: ApiFailure["details"];
  readonly requestId: string;
  /** 没有收到 HTTP 响应时为 null。 */
  readonly statusCode: number | null;
  readonly cause: unknown;

  constructor(failure: ApiFailure, statusCode: number | null = null, cause?: unknown) {
    super(failure.message);
    this.name = "ApiError";
    this.code = failure.code;
    this.details = failure.details;
    this.requestId = failure.request_id;
    this.statusCode = statusCode;
    this.cause = cause;
  }
}

function isApiFailure(value: unknown): value is ApiFailure {
  if (typeof value !== "object" || value === null) return false;
  const failure = value as Record<string, unknown>;
  return typeof failure.code === "string"
    && typeof failure.message === "string"
    && typeof failure.request_id === "string"
    && typeof failure.details === "object"
    && failure.details !== null
    && !Array.isArray(failure.details);
}

function invalidResponse(statusCode: number, requestId: string, cause: unknown): ApiError {
  return new ApiError({
    code: "INVALID_RESPONSE",
    message: "服务返回了无法识别的响应",
    details: {},
    request_id: requestId,
  }, statusCode, cause);
}

function httpFailure(payload: unknown, statusCode: number, requestId: string): ApiError {
  if (isApiFailure(payload)) return new ApiError(payload, statusCode);
  return new ApiError({
    code: "HTTP_ERROR",
    message: `服务返回 HTTP ${statusCode}，错误响应不符合接口约定`,
    details: { status_code: statusCode },
    request_id: requestId,
  }, statusCode);
}

/** 统一请求入口：沿用 60 秒超时；取消、网络、HTTP 和响应格式错误分别暴露。 */
export async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers);
  const requestId = headers.get("X-Request-ID") || crypto.randomUUID();
  headers.set("X-Request-ID", requestId);
  if (!headers.has("Accept")) headers.set("Accept", "application/json");

  const controller = new AbortController();
  let timedOut = false;
  const timeoutId = setTimeout(() => {
    timedOut = true;
    controller.abort();
  }, REQUEST_TIMEOUT_MS);
  const abortFromCaller = () => controller.abort();
  if (init.signal?.aborted) {
    controller.abort();
  } else {
    init.signal?.addEventListener("abort", abortFromCaller, { once: true });
  }

  let statusCode: number | null = null;
  let responseRequestId = requestId;
  try {
    const response = await fetch(`${apiBaseUrl}${path}`, {
      ...init, signal: controller.signal, headers,
    });
    statusCode = response.status;
    responseRequestId = response.headers.get("X-Request-ID") || requestId;
    if (response.status === 204) return undefined as T;

    let payload: unknown;
    try {
      payload = await response.json();
    } catch (error) {
      // 读取响应时的超时/断网由外层处理，只有 JSON 语法失败属于响应格式错误。
      if (error instanceof SyntaxError) throw invalidResponse(statusCode, responseRequestId, error);
      throw error;
    }
    if (!response.ok) throw httpFailure(payload, statusCode, responseRequestId);
    return payload as T;
  } catch (error) {
    if (error instanceof ApiError) throw error;
    const cancelled = init.signal?.aborted === true;
    throw new ApiError({
      code: timedOut ? "REQUEST_TIMEOUT" : cancelled ? "REQUEST_CANCELLED" : "NETWORK_ERROR",
      message: timedOut
        ? "请求超过 60 秒仍未返回，尚未确认操作结果；请查询真实状态后再决定是否重新提交"
        : cancelled ? "请求已取消，尚未确认操作结果" : "请求通信失败，请检查网络或服务状态",
      details: {},
      request_id: responseRequestId,
    }, statusCode, error);
  } finally {
    clearTimeout(timeoutId);
    init.signal?.removeEventListener("abort", abortFromCaller);
  }
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
  return new Promise<T>((resolve, reject) => {
    const requestId = crypto.randomUUID();
    const xhr = new XMLHttpRequest();
    xhr.open("POST", `${apiBaseUrl}${path}`);
    xhr.responseType = "text";
    xhr.timeout = REQUEST_TIMEOUT_MS * 5;
    xhr.setRequestHeader("Accept", "application/json");
    xhr.setRequestHeader("X-Request-ID", requestId);

    xhr.upload.onprogress = (event) => {
      if (onProgress && event.lengthComputable && event.total > 0) {
        onProgress(Math.min(100, Math.round((event.loaded / event.total) * 100)));
      }
    };

    xhr.onload = () => {
      if (xhr.status === 204) {
        resolve(undefined as T);
        return;
      }
      const responseRequestId = xhr.getResponseHeader("X-Request-ID") || requestId;
      let payload: unknown;
      try {
        payload = JSON.parse(xhr.responseText);
      } catch (error) {
        reject(invalidResponse(xhr.status, responseRequestId, error));
        return;
      }
      if (xhr.status >= 200 && xhr.status < 300) {
        resolve(payload as T);
      } else {
        reject(httpFailure(payload, xhr.status, responseRequestId));
      }
    };

    xhr.onerror = (event) => reject(new ApiError({
      code: "NETWORK_ERROR",
      message: "上传通信失败，请检查网络或服务状态",
      details: {},
      request_id: requestId,
    }, null, event));
    xhr.ontimeout = (event) => reject(new ApiError({
      code: "REQUEST_TIMEOUT",
      message: "上传超过等待时间，尚未确认操作结果；请查询材料的真实状态后再决定是否重新提交",
      details: {},
      request_id: requestId,
    }, null, event));
    xhr.onabort = (event) => reject(new ApiError({
      code: "REQUEST_CANCELLED",
      message: "上传已取消，尚未确认操作结果",
      details: {},
      request_id: requestId,
    }, null, event));
    xhr.send(formData);
  });
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
export function getOperation(operationNo: string): Promise<OperationView> {
  return request<OperationView>(`/operations/${encodeURIComponent(operationNo)}`);
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
