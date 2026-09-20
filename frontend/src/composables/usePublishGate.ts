import { computed, ref, type ComputedRef, type Ref } from "vue";
import { ElMessage } from "element-plus";

import type { PrecheckBinding, PrecheckResult, ScenarioVersion } from "@/api/contracts";
import type { ImportJobView, PublishPayload, TemplateMode } from "@/api/imports";
import { precheckImport, publishImport } from "@/api/imports";
import { nodeLabel } from "@/constants/labels";
import type { CapabilityGapRow } from "@/components/import/ImportPublishCheck.vue";

export interface PublishGateState {
  precheck: Ref<PrecheckResult | null>;
  /** 已逐条确认「这条检查本次确实无法执行」的规则键。 */
  acknowledgedGaps: Ref<string[]>;
  capabilityGaps: ComputedRef<PrecheckResult["capability_gaps"]>;
  capabilityGapRows: ComputedRef<CapabilityGapRow[]>;
  unacknowledgedGapCount: ComputedRef<number>;
  /** 预检查是否仍与当前发布意图一致（模板模式、目标场景名与目标场景）。 */
  intentMatchesBinding: (binding: PrecheckBinding) => boolean;
  toggleGapByKey: (key: string) => void;
  /** 发布意图改变：旧预检查立即失效。 */
  invalidate: () => void;
  /** 预检查；意图不完整时返回 false 并给出原因。 */
  check: (onMessage: (message: string) => void) => Promise<boolean>;
  /** 发布；成功后回传新场景版本，并清空预检查。 */
  publish: (onPublished: (version: ScenarioVersion) => void) => Promise<void>;
  reset: () => void;
}

/**
 * 发布前的能力缺口确认与发布执行。
 *
 * 能力缺口**默认阻断发布**；确认是**逐条**的，不是「忽略全部缺口」的开关。
 * 确认集合属于发布意图：改过之后旧预检查立即失效，必须重新检查——
 * 后端把它算进发布意图，绑定对不上就拒绝发布。
 *
 * 缺口的稳定键在这里解析：列表里的位置会随检查结果变化，让组件自己按下标算键，
 * 会在列表长度变化时把确认标记落到另一条缺口上。
 */
export function usePublishGate(options: {
  job: Ref<ImportJobView | null>;
  /** 当前发布意图：三项任一变化都必须使旧预检查失效。 */
  intent: { templateMode: Ref<TemplateMode>; scenarioName: Ref<string>; updateTargetCode: Ref<string> };
  run: (action: () => Promise<void>) => Promise<void>;
}): PublishGateState {
  const precheck = ref<PrecheckResult | null>(null);
  const acknowledgedGaps = ref<string[]>([]);

  const capabilityGaps = computed(() => precheck.value?.capability_gaps ?? []);

  /** 缺口的稳定键：优先用规则键；没有规则键时用它在列表里的位置，避免误标到别的缺口。 */
  function gapKey(ruleKey: string | undefined, index: number): string {
    return ruleKey || `#${index}`;
  }

  function isAcknowledged(ruleKey: string | undefined, index: number): boolean {
    return acknowledgedGaps.value.includes(gapKey(ruleKey, index));
  }

  const capabilityGapRows = computed<CapabilityGapRow[]>(() =>
    capabilityGaps.value.map((gap, index) => ({
      key: gapKey(gap.rule_key, index),
      ruleName: gap.rule_name,
      scope: gap.node ? nodeLabel(gap.node) : gap.operator || "检查规则",
      message: gap.message,
      capabilityVersion: gap.capability_version,
      acknowledged: isAcknowledged(gap.rule_key, index),
    })),
  );

  /** 尚未确认的缺口数量：>0 时发布按钮必须保持禁用，不能默认放行。 */
  const unacknowledgedGapCount = computed(
    () => capabilityGaps.value.filter((gap, index) => !isAcknowledged(gap.rule_key, index)).length,
  );

  function intentMatchesBinding(binding: PrecheckBinding): boolean {
    if (binding.template_mode !== options.intent.templateMode.value) return false;
    if (binding.target_scenario_name !== options.intent.scenarioName.value.trim()) return false;
    if (options.intent.templateMode.value === "update" && binding.target_scenario_code !== options.intent.updateTargetCode.value) {
      return false;
    }
    return true;
  }

  function toggleGapByKey(key: string): void {
    acknowledgedGaps.value = acknowledgedGaps.value.includes(key)
      ? acknowledgedGaps.value.filter((item) => item !== key)
      : [...acknowledgedGaps.value, key];
    // 确认集合属于发布意图：改过之后旧预检查立即失效，必须重新检查。
    precheck.value = null;
  }

  function invalidate(): void {
    precheck.value = null;
  }

  function payloadOf(): PublishPayload {
    const payload: PublishPayload = {
      scenario_name: options.intent.scenarioName.value.trim(),
      template_mode: options.intent.templateMode.value,
      // 逐条确认过的能力缺口随每次预检查/发布请求一起发送：后端把它当作发布意图的一部分，
      // 换了确认集合就要求重新预检查，不能用「已确认某项不可执行」的旧检查放行新配置。
      acknowledged_capability_gaps: [...acknowledgedGaps.value],
    };
    if (options.intent.templateMode.value === "update") {
      payload.scenario_code = options.intent.updateTargetCode.value;
    }
    return payload;
  }

  async function check(onMessage: (message: string) => void): Promise<boolean> {
    if (!options.job.value) return false;
    if (options.intent.scenarioName.value.trim().length < 2) {
      onMessage("请填写至少 2 个字符的模板名称。");
      return false;
    }
    if (options.intent.templateMode.value === "update" && !options.intent.updateTargetCode.value) {
      onMessage("请选择要更新的审核模板。");
      return false;
    }
    let ok = false;
    await options.run(async () => {
      precheck.value = await precheckImport(options.job.value!.job_no, payloadOf());
      ok = true;
    });
    return ok;
  }

  async function publish(onPublished: (version: ScenarioVersion) => void): Promise<void> {
    if (!options.job.value) return;
    const jobNo = options.job.value.job_no;
    await options.run(async () => {
      try {
        const version = await publishImport(jobNo, payloadOf());
        onPublished(version);
        precheck.value = null;
        ElMessage.success("模板版本已发布");
      } catch (error) {
        const code = error instanceof Error && "code" in error ? (error as { code: string }).code : "";
        if (code === "PUBLISH_PRECHECK_REQUIRED" || code === "PUBLISH_PREVIEW_MISMATCH") {
          // 发布意图已变化或检查缺失：清空旧检查结果，由调用方提示重新执行发布检查。
          precheck.value = null;
        }
        throw error;
      }
    });
  }

  function reset(): void {
    precheck.value = null;
    acknowledgedGaps.value = [];
  }

  return {
    precheck,
    acknowledgedGaps,
    capabilityGaps,
    capabilityGapRows,
    unacknowledgedGapCount,
    intentMatchesBinding,
    toggleGapByKey,
    invalidate,
    check,
    publish,
    reset,
  };
}
