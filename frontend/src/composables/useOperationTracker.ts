import { onUnmounted, ref, type Ref } from "vue";

import { isOperationFinished, watchOperation, type OperationView, type OperationWatch } from "@/api/http";

interface OperationHandlers {
  onFinished: (operation: OperationView) => void | Promise<void>;
  onUpdate?: (operation: OperationView) => void | Promise<void>;
  onError: (error: unknown) => void;
}

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
    handlers: OperationHandlers,
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
  /** 追踪代次：换对象或停止后，旧轮询的异步回调与报错都不再影响当前追踪。 */
  let generation = 0;

  function stopWatch(): void {
    handle?.stop();
    handle = null;
    watching.value = false;
  }

  function stop(): void {
    generation += 1;
    stopWatch();
  }

  async function receive(value: OperationView, token: number): Promise<void> {
    if (token !== generation) {
      return;
    }
    operation.value = value;
    if (isOperationFinished(value.status)) {
      stopWatch();
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
    stopWatch();
    const token = ++generation;
    watching.value = true;
    const currentOperationNo = operationNo;
    handle = watchOperation(
      currentOperationNo,
      (value) => receive(value, token),
      (error) => {
        // 旧代次的回调报错属于已经放弃的那次查询，不能停掉新追踪或交给新对象的处理器。
        if (token !== generation) {
          return;
        }
        stopWatch();
        const handler = onError;
        if (handler) {
          handler(error);
        }
      },
    );
  }

  function track(
    value: OperationView,
    handlers: OperationHandlers,
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
