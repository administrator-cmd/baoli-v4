import { ref, type Ref } from "vue";

import type { ApiError } from "@/api/http";

export interface RequestRunner {
  pending: Ref<boolean>;
  failure: Ref<ApiError | null>;
  /** 表单级提示（中文），用于「还不能提交」这类前端可判断的原因。 */
  formMessage: Ref<string>;
  run: (action: () => Promise<void>) => Promise<void>;
  setFailure: (error: unknown) => void;
  clear: () => void;
}

/**
 * 页面统一的请求执行状态。
 *
 * 只做三件事：进入时清空上一次的错误、失败时保留服务端返回的真实原因、结束时复位忙碌状态。
 * 不吞异常、不重试、不降级：错误原样交给页面和 FailureNotice 显示。
 */
export function useRequestRunner(): RequestRunner {
  const pending = ref(false);
  const failure = ref<ApiError | null>(null);
  const formMessage = ref("");

  function setFailure(error: unknown): void {
    if (error instanceof Error && "code" in error && "requestId" in error) {
      failure.value = error as ApiError;
      return;
    }
    failure.value = {
      name: "ApiError",
      message: "无法连接到服务",
      code: "NETWORK_ERROR",
      details: {},
      requestId: "unknown",
    } as ApiError;
  }

  function clear(): void {
    failure.value = null;
    formMessage.value = "";
  }

  async function run(action: () => Promise<void>): Promise<void> {
    clear();
    pending.value = true;
    try {
      await action();
    } catch (error) {
      setFailure(error);
    } finally {
      pending.value = false;
    }
  }

  return { pending, failure, formMessage, run, setFailure, clear };
}
