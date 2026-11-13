import { ref, type Ref } from "vue";
import { ElMessageBox } from "element-plus";

import type {
  AuditDocument,
  ReplacementImpact,
} from "@/api/contracts";

import { removeTaskDocumentFile, uploadTaskDocument } from "@/api/audits";

export interface MaterialActionsState {
  /** 正在上传/移除的材料位编号；null 表示没有进行中的操作。 */
  busyDocumentNo: Ref<string | null>;
  /** 浏览器真实上报的上传进度百分比；不是估算。 */
  uploadPercent: Ref<number | null>;
  /** 最近一次替换/补传/移除的真实影响范围，供页面说明失效了什么。 */
  replacementImpact: Ref<(ReplacementImpact & { materialName: string }) | null>;
  upload: (file: File, document: AuditDocument, materialType: string) => Promise<void>;
  remove: (document: AuditDocument) => Promise<void>;
  reset: () => void;
}

/**
 * 材料位的上传与移除。
 *
 * 替换与移除都是输入修订：服务端会让依赖旧文件的处理产物、字段事实与规则结果失效，
 * 页面把服务端返回的真实影响范围原样展示，不自行估算数字。
 * 移除前必须二次确认：确认文案里写清「旧文件保留为历史版本、任务回到材料检查」。
 */
export function useMaterialActions(options: {
  taskNo: Ref<string>;
  run: (action: () => Promise<void>) => Promise<void>;
  /** 每次输入变化后重新读取任务状态。 */
  refresh: (taskNo: string) => Promise<void>;
}): MaterialActionsState {
  const busyDocumentNo = ref<string | null>(null);
  const uploadPercent = ref<number | null>(null);
  const replacementImpact = ref<(ReplacementImpact & { materialName: string }) | null>(null);
  let generation = 0;

  async function upload(file: File, document: AuditDocument, materialType: string): Promise<void> {
    const taskNo = options.taskNo.value;
    const token = generation;
    const isCurrent = () => token === generation && taskNo === options.taskNo.value;
    const replacing = Boolean(document.filename);
    busyDocumentNo.value = document.document_no;
    uploadPercent.value = 0;
    try {
      await options.run(async () => {
        const uploaded = await uploadTaskDocument(
          taskNo,
          document.document_no,
          file,
          materialType,
          replacing,
          // 百分比来自浏览器真实发送进度，不是估算。
          (percent) => {
            if (isCurrent()) uploadPercent.value = percent;
          },
        );
        if (!isCurrent()) return;
        uploadPercent.value = 100;
        replacementImpact.value = uploaded.impact ? { ...uploaded.impact, materialName: document.material_name } : null;
        await options.refresh(taskNo);
      });
    } finally {
      if (isCurrent()) {
        busyDocumentNo.value = null;
        uploadPercent.value = null;
      }
    }
  }

  async function remove(document: AuditDocument): Promise<void> {
    const taskNo = options.taskNo.value;
    const token = generation;
    const isCurrent = () => token === generation && taskNo === options.taskNo.value;
    if (!document.filename) return;
    try {
      await ElMessageBox.confirm(
        `确认移除「${document.material_name}」的当前文件？旧文件保留为历史版本；依赖它的处理产物、字段事实与规则结果会失效，任务回到材料检查。`,
        "移除当前文件",
        { type: "warning", confirmButtonText: "移除", cancelButtonText: "取消" },
      );
    } catch {
      return;
    }
    if (!isCurrent()) return;
    busyDocumentNo.value = document.document_no;
    try {
      await options.run(async () => {
        const result = await removeTaskDocumentFile(taskNo, document.document_no);
        if (!isCurrent()) return;
        replacementImpact.value = result.impact ? { ...result.impact, materialName: result.material_name } : null;
        await options.refresh(taskNo);
      });
    } finally {
      if (isCurrent()) busyDocumentNo.value = null;
    }
  }

  function reset(): void {
    generation += 1;
    busyDocumentNo.value = null;
    uploadPercent.value = null;
    replacementImpact.value = null;
  }

  return { busyDocumentNo, uploadPercent, replacementImpact, upload, remove, reset };
}
