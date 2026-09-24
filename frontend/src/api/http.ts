import type { ApiFailure, OperationAccepted, OperationStatus } from "./contracts";

const apiBaseUrl = import.meta.env.VITE_API_BASE_URL ?? "/api";
const REQUEST_TIMEOUT_MS = 60_000;

/** 后台操作状态查询间隔。长操作已改为「提交并返回 → 后台执行 → 查询状态」。 */
export const OPERATION_POLL_INTERVAL_MS = 2_000;

export class ApiError extends Error {
  readonly code: string;
  readonly details: ApiFailure["details"];
  readonly requestId: string;

  constructor(failure: ApiFailure) {
    super(failure.message);
    this.name = "ApiError";
    this.code = failure.code;
    this.details = failure.details;
    this.requestId = failure.request_id;
  }
}

function isApiFailure(value: unknown): value is ApiFailure {
  return typeof value === "object" && value !== null && "code" in value && "message" in value && "request_id" in value;
}

async function parseJson(response: Response): Promise<unknown> {
  try {
    return await response.json();
  } catch {
    throw new ApiError({
      code: "INVALID_RESPONSE",
      message: "服务返回了无法识别的响应",
      details: {},
      request_id: response.headers.get("X-Request-ID") ?? "unknown",
    });
  }
}

/** 统一请求入口：60 秒超时、失败统一转成 ApiError（带 request_id），204 返回 undefined。 */
export async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
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

  try {
    let response: Response;
    try {
      response = await fetch(`${apiBaseUrl}${path}`, {
        ...init,
        signal: controller.signal,
        headers: {
          Accept: "application/json",
          ...init.headers,
        },
      });
    } catch {
      throw new ApiError({
        code: timedOut ? "REQUEST_TIMEOUT" : "NETWORK_ERROR",
        message: timedOut
          ? "本次请求超过 60 秒仍未返回。已提交的后台操作不会因此丢失：页面会继续按操作编号查询真实状态，不需要靠刷新页面恢复。"
          : "无法连接服务，请检查网络或服务状态",
        details: {},
        request_id: "unknown",
      });
    }

    // 204 没有响应体（例如删除成功）；其余状态码必须有可解析的响应体。
    if (response.status === 204) {
      return undefined as T;
    }
    const payload = await parseJson(response);
    if (!response.ok) {
      if (isApiFailure(payload)) {
        throw new ApiError(payload);
      }
      throw new ApiError({
        code: "HTTP_ERROR",
        message: "服务未能完成本次请求，请稍后重试",
        details: {},
        request_id: response.headers.get("X-Request-ID") ?? "unknown",
      });
    }
    return payload as T;
  } finally {
    clearTimeout(timeoutId);
    init.signal?.removeEventListener("abort", abortFromCaller);
  }
}

/** 拼出后端文件地址，供下载 / 预览使用。 */
export function apiFileUrl(path: string): string {
  return `${apiBaseUrl}${path}`;
}

/**
 * 上传文件并回报真实进度。
 *
 * fetch 无法获取上传进度，只有 XHR 的 upload.onprogress 能给出已发送字节数；
 * 这里的百分比来自浏览器真实发送进度，不是估算或动画。
 */
export function uploadWithProgress<T>(
  path: string,
  formData: FormData,
  onProgress?: (percent: number) => void,
): Promise<T> {
  return new Promise<T>((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", `${apiBaseUrl}${path}`);
    xhr.responseType = "text";
    xhr.timeout = REQUEST_TIMEOUT_MS * 5;
    xhr.setRequestHeader("Accept", "application/json");

    xhr.upload.onprogress = (event) => {
      if (onProgress && event.lengthComputable && event.total > 0) {
        onProgress(Math.min(100, Math.round((event.loaded / event.total) * 100)));
      }
    };

    xhr.onload = () => {
      let payload: unknown;
      try {
        payload = xhr.responseText ? JSON.parse(xhr.responseText) : undefined;
      } catch {
        reject(
          new ApiError({
            code: "INVALID_RESPONSE",
            message: "服务返回了无法识别的响应",
            details: {},
            request_id: xhr.getResponseHeader("X-Request-ID") ?? "unknown",
          }),
        );
        return;
      }
      if (xhr.status >= 200 && xhr.status < 300) {
        resolve(payload as T);
        return;
      }
      if (isApiFailure(payload)) {
        reject(new ApiError(payload));
        return;
      }
      reject(
        new ApiError({
          code: "HTTP_ERROR",
          message: "服务未能完成本次上传，请稍后重试",
          details: {},
          request_id: xhr.getResponseHeader("X-Request-ID") ?? "unknown",
        }),
      );
    };

    xhr.onerror = () => {
      reject(
        new ApiError({
          code: "NETWORK_ERROR",
          message: "无法连接服务，请检查网络或服务状态",
          details: {},
          request_id: "unknown",
        }),
      );
    };

    xhr.ontimeout = () => {
      reject(
        new ApiError({
          code: "REQUEST_TIMEOUT",
          message: "上传超过等待时间仍未返回；已上传的内容不会因此丢失，请刷新后核对材料位的真实状态。",
          details: {},
          request_id: "unknown",
        }),
      );
    };

    xhr.send(formData);
  });
}

/**
 * 后台操作的完整视图。contracts.ts 的 OperationAccepted 是权威契约；
 * 这里额外声明服务端已持久化、但当前响应模型尚未输出的时间与目标节点字段：
 * 页面只读它们，缺省时只显示状态本身，不猜进度。
 */
export type OperationView = OperationAccepted & {
  target_node?: string | null;
  input_version?: string | null;
  created_at?: string | null;
  started_at?: string | null;
  finished_at?: string | null;
};

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
