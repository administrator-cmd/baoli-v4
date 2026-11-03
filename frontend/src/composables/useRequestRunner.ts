import { ref, type Ref } from "vue";

import { ApiError } from "@/api/http";

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
    if (error instanceof ApiError) {
      failure.value = error;
      return;
    }
    // 网络、超时、HTTP 与响应格式错误已由 HTTP 层分成 ApiError；走到这里是页面自身
    // 处理逻辑抛出的异常，按原样保留原因，不能改写成「无法连接到服务」。
    const message = error instanceof Error ? error.message : String(error);
    failure.value = new ApiError({
      code: "UNEXPECTED_ERROR",
      message: `页面处理出错：${message}`,
      data: {},
      request_id: "",
      success: false,
    }, null, error);
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
