import { onUnmounted, ref, type Ref } from "vue";

import { isOperationFinished, watchOperation, type OperationView, type OperationWatch } from "@/api/http";

/**
 * 后台操作的页面侧查询状态。
 *
 * 按 operation_no 查询真实状态，执行中通知可选更新回调，结束后回调一次；卸载时停止轮询。
 * 查询失败只回调给页面并停止轮询，绝不在这里重新提交执行请求；再次查询用 requery()。
 */
export interface OperationTracker {
  operation: Ref<OperationView | null>;
  watching: Ref<boolean>;
  /** 追踪一次已受理（或已结束）的操作。 */
  track: (
    operation: OperationView,
    handlers: {
      onFinished: (operation: OperationView) => void | Promise<void>;
      onUpdate?: (operation: OperationView) => void | Promise<void>;
      onError: (error: unknown) => void;
    },
  ) => void;
  /** 只再次查询当前操作，不重新提交执行请求。 */
  requery: () => void;
  stop: () => void;
  /**
   * 一次性完整重置：停止轮询，并清空操作编号、当前操作与回调。
   * 切换任务必须用它，不能只用 stop()——否则会残留上一个任务的操作面板，
   * 「再次查询」还会按旧编号查询。
   */
  reset: () => void;
}

export function useOperationTracker(): OperationTracker {
  const operation = ref<OperationView | null>(null);
  const watching = ref(false);
  let operationNo: string | null = null;
  let handle: OperationWatch | null = null;
  let onFinished: ((operation: OperationView) => void | Promise<void>) | null = null;
  let onUpdate: ((operation: OperationView) => void | Promise<void>) | null = null;
  let onError: ((error: unknown) => void) | null = null;

  function stop(): void {
    handle?.stop();
    handle = null;
    watching.value = false;
  }

  async function receive(value: OperationView): Promise<void> {
    operation.value = value;
    if (isOperationFinished(value.status)) {
      stop();
      const handler = onFinished;
      if (handler) {
        await handler(value);
      }
    } else if (onUpdate) {
      await onUpdate(value);
    }
  }

  function poll(): void {
    if (!operationNo) {
      return;
    }
    handle?.stop();
    watching.value = true;
    handle = watchOperation(
      operationNo,
      receive,
      (error) => {
        stop();
        const handler = onError;
        if (handler) {
          handler(error);
        }
      },
    );
  }

  function track(
    value: OperationView,
    handlers: {
      onFinished: (operation: OperationView) => void | Promise<void>;
      onUpdate?: (operation: OperationView) => void | Promise<void>;
      onError: (error: unknown) => void;
    },
  ): void {
    onFinished = handlers.onFinished;
    onUpdate = handlers.onUpdate ?? null;
    onError = handlers.onError;
    operationNo = value.operation_no;
    operation.value = value;
    if (isOperationFinished(value.status)) {
      stop();
      void Promise.resolve(handlers.onFinished(value)).catch(handlers.onError);
      return;
    }
    poll();
  }

  function reset(): void {
    stop();
    operationNo = null;
    operation.value = null;
    onFinished = null;
    onUpdate = null;
    onError = null;
  }

  onUnmounted(stop);

  return { operation, watching, track, requery: poll, stop, reset };
}
