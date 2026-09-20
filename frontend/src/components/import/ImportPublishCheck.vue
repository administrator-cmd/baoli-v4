<script setup lang="ts">
import { computed, ref } from "vue";

import type { PrecheckResult } from "@/api/contracts";
import StateBlock from "@/components/StateBlock.vue";
import { failureNextStep } from "@/constants/labels";

/**
 * 发布检查与发布：发布流程里唯一一处执行检查、确认能力缺口、发布的地方。
 *
 * 检查结果全部来自服务端的发布检查接口，不在这里推算；
 * 存在阻断项时发布按钮禁用并写明原因；发布前用对话框确认「发布后不可修改」。
 *
 * 发布意图（模板名称、发布方式、目标模板）的表单不在这里：它由调用页面渲染，
 * 但必须与本组件同处一步——改发布意图会使检查结果失效，两者分开会逼出第二个检查入口。
 */

/** 一条能力缺口在界面上的呈现：口径由调用页面算好，这里只负责渲染。 */
export interface CapabilityGapRow {
  key: string;
  ruleName: string;
  /** 缺口所属范围的中文标签（审核节点或算子）。 */
  scope: string;
  message: string;
  capabilityVersion?: string;
  acknowledged: boolean;
}

const props = withDefaults(
  defineProps<{
    precheck: PrecheckResult | null;
    /** 本次将发布的模板名称，用于确认对话框。 */
    templateName: string;
    /** 发布按钮是否可用。 */
    canPublish: boolean;
    /** 禁用发布的中文原因。 */
    disabledReason: string | null;
    pending: boolean;
    /** 检查是否正在执行。 */
    checking: boolean;
    /** 发布检查给出的能力缺口；空数组表示没有缺口。 */
    gaps?: CapabilityGapRow[];
  }>(),
  { gaps: () => [] },
);

const emit = defineEmits<{
  "run-precheck": [];
  publish: [];
  "toggle-gap": [key: string];
}>();

const confirmVisible = ref(false);

const blockers = computed(() => props.precheck?.blockers ?? []);

function openConfirm(): void {
  if (!props.canPublish) {
    return;
  }
  confirmVisible.value = true;
}

function confirmPublish(): void {
  confirmVisible.value = false;
  emit("publish");
}
</script>

<template>
  <div>
    <StateBlock
      v-if="!precheck"
      kind="empty"
      title="还没有执行发布检查"
      description="发布检查会校验引用是否唯一、参数是否有效、是否还有未处理的业务问题，并列出当前无法执行的能力缺口。检查通过后才能发布。"
    >
      <el-button type="primary" :loading="checking" @click="emit('run-precheck')">执行发布检查</el-button>
    </StateBlock>

    <template v-else>
      <el-descriptions :column="4" size="small" border>
        <el-descriptions-item label="材料位">{{ precheck.summary.materials }}</el-descriptions-item>
        <el-descriptions-item label="任务字段">{{ precheck.summary.fields }}</el-descriptions-item>
        <el-descriptions-item label="启用规则">{{ precheck.summary.rules }}</el-descriptions-item>
        <el-descriptions-item label="未处理问题">{{ precheck.summary.issues }}</el-descriptions-item>
      </el-descriptions>

      <div v-if="blockers.length" class="block-gap">
        <el-alert
          v-for="(blocker, index) in blockers"
          :key="`${blocker.code}-${index}`"
          type="error"
          show-icon
          :closable="false"
          :title="blocker.message"
          class="publish-blocker"
        >
          <div>错误码：{{ blocker.code }}</div>
          <div>{{ failureNextStep(blocker.code) }}</div>
        </el-alert>
      </div>

      <el-alert
        v-else
        class="block-gap"
        type="success"
        show-icon
        :closable="false"
        title="发布检查通过"
      >
        未发现阻断项，可以发布。发布后该版本不可修改，只能通过再次导入生成新版本。
      </el-alert>

      <!-- 能力缺口是发布检查的结论之一，与阻断项并列展示：
           它是「这条检查本次给不出结论」的事实，不是可以忽略的提示。 -->
      <el-alert
        v-if="gaps.length"
        class="block-gap"
        type="warning"
        show-icon
        :closable="false"
        :title="`有 ${gaps.length} 项检查引用的能力当前没有判定实现，默认阻断发布`"
      >
        <div v-for="gap in gaps" :key="gap.key" class="gap-row">
          <div>
            {{ gap.ruleName }}（{{ gap.scope }}）：{{ gap.message }}
            <span v-if="gap.capabilityVersion" class="text-hint">能力版本 {{ gap.capabilityVersion }}</span>
          </div>
          <el-checkbox
            :model-value="gap.acknowledged"
            @change="emit('toggle-gap', gap.key)"
          >
            我确认该项本次不可判定
          </el-checkbox>
        </div>
        <div class="text-hint">
          未确认的检查会阻断发布。确认是逐条的：它只表示你知道这项运行时会明确报「不可判定」、
          不会给出结论，并不等于该检查通过；确认过的缺口会随模板版本一起冻结，任务与报告里继续可见。
          改变确认集合后需要重新执行发布检查。
        </div>
      </el-alert>
    </template>

    <div class="sticky-actions">
      <div class="sticky-actions__bar">
        <span :class="disabledReason ? 'text-danger' : 'text-muted'">
          {{ disabledReason ?? "发布后会生成一个新的模板版本，可在审核工作台用它创建审核任务。" }}
        </span>
        <div class="card-actions">
          <el-button :loading="checking" :disabled="pending" @click="emit('run-precheck')">执行发布检查</el-button>
          <el-button
            type="primary"
            :disabled="!canPublish || pending"
            :title="disabledReason ?? ''"
            @click="openConfirm"
          >
            发布模板版本
          </el-button>
        </div>
      </div>
    </div>

    <el-dialog v-model="confirmVisible" title="确认发布模板版本" width="480px" append-to-body>
      <el-descriptions :column="1" size="small" border>
        <el-descriptions-item label="模板名称">{{ templateName || "未填写" }}</el-descriptions-item>
        <el-descriptions-item label="当前状态">
          {{ canPublish ? "发布检查已通过，可以发布" : disabledReason }}
        </el-descriptions-item>
      </el-descriptions>
      <el-alert
        class="block-gap"
        type="warning"
        show-icon
        :closable="false"
        title="发布后该版本不可修改"
      >
        已发布的模板版本不能再改动；如需调整规则，请重新导入并再次发布新版本。
      </el-alert>
      <template #footer>
        <el-button @click="confirmVisible = false">取消</el-button>
        <el-button type="primary" :loading="pending" @click="confirmPublish">确认发布</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.publish-blocker + .publish-blocker {
  margin-top: 8px;
}
</style>
