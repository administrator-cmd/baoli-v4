import { ref, shallowRef, type Ref } from "vue";
import { ElMessage } from "element-plus";

import type { AuditDocument } from "@/api/contracts";
import type { DocumentOcrStatus, OcrReconcileRequest } from "@/api/audits";
import { getDocumentOcr, reconcileDocumentOcr } from "@/api/audits";

export interface OcrReconcileState {
  /** 当前对账的材料位；为 null 表示弹窗关闭。 */
  document: Ref<AuditDocument | null>;
  status: Ref<DocumentOcrStatus | null>;
  statusError: Ref<string | null>;
  statusLoading: Ref<boolean>;
  /** 本次对账动作的服务端返回；未提交时为 null。 */
  result: Ref<{ status: string; job_id: string | null; deadline_at: string | null; message: string } | null>;
  pending: Ref<boolean>;
  open: (document: AuditDocument) => void;
  query: (documentNo: string) => Promise<void>;
  requery: () => void;
  submit: (payload: OcrReconcileRequest) => Promise<boolean>;
  close: () => void;
}

/**
 * OCR 提交结果未知时的人工对账。
 *
 * 页面只展示服务端记录的真实作业状态：没有产物就写「尚未提交」，
 * 不编造作业号或截止时间；本地等待超时也不代表供应商作业已失效，
 * 因此重新提交只能由人工明确动作触发（ADOPT_JOB / RETRY_SUBMIT），不做自动重试。
 */
export function useOcrReconcile(options: {
  taskNo: Ref<string>;
  run: (action: () => Promise<void>) => Promise<void>;
  setFailure: (error: unknown) => void;
  /** 提交成功后重新读取任务状态。 */
  refresh: (taskNo: string) => Promise<void>;
}): OcrReconcileState {
  const document = shallowRef<AuditDocument | null>(null);
  const status = shallowRef<DocumentOcrStatus | null>(null);
  const statusError = ref<string | null>(null);
  const statusLoading = ref(false);
  const result = ref<{ status: string; job_id: string | null; deadline_at: string | null; message: string } | null>(null);
  const pending = ref(false);

  async function query(documentNo: string): Promise<void> {
    const taskNo = options.taskNo.value;
    statusLoading.value = true;
    statusError.value = null;
    try {
      status.value = await getDocumentOcr(taskNo, documentNo);
    } catch (error) {
      status.value = null;
      statusError.value = error instanceof Error ? error.message : "未知原因";
    } finally {
      statusLoading.value = false;
    }
  }

  function open(target: AuditDocument): void {
    document.value = target;
    status.value = null;
    statusError.value = null;
    result.value = null;
    void query(target.document_no);
  }

  function requery(): void {
    if (document.value) void query(document.value.document_no);
  }

  async function submit(payload: OcrReconcileRequest): Promise<boolean> {
    const taskNo = options.taskNo.value;
    const documentNo = document.value?.document_no;
    if (!documentNo) return false;
    pending.value = true;
    let ok = false;
    try {
      await options.run(async () => {
        try {
          const response = await reconcileDocumentOcr(taskNo, documentNo, payload);
          result.value = {
            status: response.status,
            job_id: response.job_id,
            deadline_at: response.deadline_at,
            message: response.message,
          };
          await options.refresh(taskNo);
          ok = true;
        } catch (error) {
          options.setFailure(error);
          ElMessage.error(error instanceof Error ? error.message : "OCR 对账失败");
        }
      });
    } finally {
      pending.value = false;
    }
    return ok;
  }

  function close(): void {
    document.value = null;
    status.value = null;
    statusError.value = null;
    result.value = null;
  }

  return { document, status, statusError, statusLoading, result, pending, open, query, requery, submit, close };
}
