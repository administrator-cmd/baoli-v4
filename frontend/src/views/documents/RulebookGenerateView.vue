<script setup lang="ts">
import { onMounted, onUnmounted, ref, watch } from "vue";
import { useRouter } from "vue-router";
import { ElMessage } from "element-plus";
import { ArrowRight, Check, FileSpreadsheet, MessageSquareText, ShieldCheck, Sparkles } from "lucide-vue-next";

import { createGenerationJob, startGeneration } from "@/api/rulebookGenerations";
import { getUploadPolicy } from "@/api/system";
import type { GenerationJob, UploadKindPolicy } from "@/api/contracts";
import { ApiError, operationIdempotencyKey } from "@/api/http";
import { useRequestRunner } from "@/composables/useRequestRunner";
import BackButton from "@/components/BackButton.vue";
import FailureNotice from "@/components/FailureNotice.vue";
import FileUploadCard from "@/components/import/FileUploadCard.vue";

/**
 * 用户点击“上传并开始编制”后，依次保存原件并提交真实后台编制。
 * 启动失败时保留已创建记录和幂等键，不重复上传或伪称已开始。
 */
const router = useRouter();
const { pending, failure, formMessage, run, clear } = useRequestRunner();

const selectedFile = ref<File | null>(null);
const selectedFileSize = ref<number | null>(null);
const uploadKey = ref("");
const uploadPercent = ref<number | null>(null);
const businessBackground = ref("");
const generationRequirements = ref("");
const policy = ref<UploadKindPolicy | null>(null);
const createdJob = ref<GenerationJob | null>(null);
const generationKey = ref("");
const stage = ref("");
let disposed = false;

async function loadPolicy(): Promise<void> {
  try {
    const payload = await getUploadPolicy();
    policy.value = payload.rulebook;
  } catch (error) {
    // 上传策略只用于提示；服务端仍会做真实校验。读取失败不伪装成已配置。
    formMessage.value = error instanceof Error ? `上传策略读取失败：${error.message}` : "上传策略读取失败";
  }
}

function onPick(file: File): void {
  if (pending.value) return;
  selectedFile.value = file;
  selectedFileSize.value = file.size;
  // 同一次上传提交的重发复用这个键；重新选择文件或修改生成输入就是新的显式上传操作。
  regenerateUploadKey();
  uploadPercent.value = null;
  clear();
}

function removeSelectedFile(): void {
  if (pending.value) return;
  selectedFile.value = null;
  selectedFileSize.value = null;
  uploadKey.value = "";
  uploadPercent.value = null;
  createdJob.value = null;
  generationKey.value = "";
  stage.value = "";
  clear();
}

async function submit(): Promise<void> {
  if (pending.value) return;
  const file = selectedFile.value;
  if (!file) {
    ElMessage.warning("请先选择规则库 Excel 文件");
    return;
  }
  if (!uploadKey.value) {
    uploadKey.value = operationIdempotencyKey("generation-upload", file.name);
  }
  await run(async () => {
    if (!createdJob.value) {
      stage.value = "正在保存原始业务资料";
      const created = await createGenerationJob(
        file,
        {
          businessBackground: businessBackground.value,
          generationRequirements: generationRequirements.value,
          idempotencyKey: uploadKey.value,
        },
        (percent) => { if (!disposed) uploadPercent.value = percent; },
      );
      if (disposed) return;
      createdJob.value = created.job;
    }
    const job = createdJob.value;
    if (job.can_generate) {
      stage.value = "原始资料已保存，正在开始 AI 编制";
      if (!generationKey.value) generationKey.value = operationIdempotencyKey("generation", job.job_no);
      try {
        await startGeneration(job.job_no, generationKey.value);
      } catch (error) {
        if (error instanceof ApiError && error.statusCode !== null && error.statusCode >= 400 && error.statusCode < 500) {
          generationKey.value = "";
        }
        stage.value = "原始资料已保存，编制是否开始请查看记录；重试将继续使用此记录";
        throw error;
      }
      if (disposed) return;
      ElMessage.success("AI 编制已受理，可在记录中查看进度");
    }
    await router.push({ name: "rulebook-generation-detail", params: { jobNo: job.job_no } });
  });
}

function regenerateUploadKey(): void {
  if (!selectedFile.value) return;
  createdJob.value = null;
  generationKey.value = "";
  stage.value = "";
  uploadKey.value = operationIdempotencyKey(
    "generation-upload", selectedFile.value.name, String(selectedFile.value.size),
    String(selectedFile.value.lastModified),
  );
}

// 修改业务背景或补充要求就是不同的上传请求，不能复用旧标识。
watch([businessBackground, generationRequirements], regenerateUploadKey);

onMounted(() => { void loadPolicy(); });
onUnmounted(() => { disposed = true; });
</script>

<template>
  <div class="app-panel generation-create">
    <div class="generation-create__topline">
      <BackButton label="返回规则库" @click="router.push({ name: 'rulebooks' })" />
      <span class="generation-create__eyebrow"><Sparkles :size="15" /> 规则库编制工作台</span>
    </div>

    <header class="generation-create__intro">
      <div>
        <h1>把业务资料，整理成清晰规则</h1>
        <p>上传现有 Excel，无需先编号或拆表。AI 整理规则，只把需要你决定的问题留给你。</p>
      </div>
      <div class="generation-create__flow" aria-label="编制流程">
        <span class="is-current"><i>1</i> 准备资料</span><ArrowRight :size="15" />
        <span><i>2</i> AI 整理</span><ArrowRight :size="15" />
        <span><i>3</i> 核对使用</span>
      </div>
    </header>

    <FailureNotice :error="failure" @dismiss="clear" />
    <el-alert v-if="formMessage" class="block-gap-sm" type="warning" show-icon :closable="false" :title="formMessage" />

    <div class="generation-create__grid">
      <section class="generation-create__form-pane">
        <section class="generation-create__section">
          <div class="generation-create__section-heading">
            <span class="generation-create__step">01</span>
            <div><h2>上传业务资料</h2><p>选择作为编制依据的 Excel 工作簿</p></div>
          </div>
          <div class="generation-create__upload">
            <FileUploadCard
              :job-filename="null"
              :job-created-at="null"
              :job-status="null"
              :selected-filename="selectedFile?.name"
              :selected-file-size="selectedFileSize"
              :policy="policy"
              :pending="pending"
              :disabled="pending"
              @pick="onPick"
              @remove="removeSelectedFile"
            >
              <template #selected-status>
                <el-tag :type="createdJob ? 'success' : failure ? 'warning' : 'primary'" size="small">
                  {{ createdJob ? '原件已保存' : pending ? '正在上传' : failure ? '上传未完成' : '待上传' }}
                </el-tag>
              </template>
              <template #selected-hint>
                <div v-if="pending && uploadPercent !== null && !createdJob" class="generation-file-progress">
                  <el-progress :percentage="uploadPercent" :stroke-width="6" />
                </div>
                <span v-if="stage && (pending || createdJob)">{{ stage }}</span>
              </template>
            </FileUploadCard>
          </div>
        </section>

        <el-divider />

        <section class="generation-create__section">
          <div class="generation-create__section-heading">
            <span class="generation-create__step">02</span>
            <div><h2>补充业务背景 <span>选填</span></h2><p>给 AI 一点上下文，帮助它理解规则的适用范围</p></div>
          </div>
          <el-form label-position="top" :disabled="pending" class="generation-create__fields">
            <el-form-item label="这份规则库用于什么业务？">
              <el-input v-model="businessBackground" type="textarea" :maxlength="16000" show-word-limit :rows="3"
                placeholder="例如：用于采购合同审核，适用于集团各事业部的设备及服务采购。" />
            </el-form-item>
            <el-form-item label="希望 AI 怎样整理？">
              <el-input v-model="generationRequirements" type="textarea" :maxlength="16000" show-word-limit :rows="3"
                placeholder="例如：保留原有条款编号，合并重复内容，并把适用条件和判断标准写清楚。" />
            </el-form-item>
          </el-form>
        </section>

        <footer class="generation-create__submit">
          <div class="generation-create__submit-note">
            <el-icon><ShieldCheck /></el-icon>
            <span>原始文件会保留，整理结果可继续修改</span>
          </div>
          <div class="generation-actions">
            <el-button type="primary" size="large" :loading="pending" :disabled="!selectedFile" @click="submit">
              <template #icon><Sparkles :size="16" /></template>
              {{ createdJob ? '继续开始 AI 编制' : '上传并开始编制' }}
            </el-button>
            <el-button v-if="createdJob && !pending" size="large" @click="router.push({ name: 'rulebook-generation-detail', params: { jobNo: createdJob.job_no } })">查看编制记录</el-button>
          </div>
        </footer>
      </section>

      <aside class="generation-create__aside">
        <div class="generation-create__aside-heading"><span><Sparkles :size="16" /></span><div><strong>编制完成后</strong><small>你始终拥有最终决定权</small></div></div>
        <ul class="generation-create__benefits">
          <li><i><Check :size="14" /></i><span><strong>保留业务原意</strong><small>沿用原编号、条款和真实关联</small></span></li>
          <li><i><Check :size="14" /></i><span><strong>整理成可读结构</strong><small>按业务内容归纳成表格与栏目</small></span></li>
          <li><i><Check :size="14" /></i><span><strong>标出真正的疑问</strong><small>有依据的内容整理，不确定处留给你确认</small></span></li>
          <li><i><Check :size="14" /></i><span><strong>导出用于场景</strong><small>确认后的规则写回 Excel，可在创建场景时导入</small></span></li>
        </ul>
        <div class="generation-create__aside-foot"><FileSpreadsheet :size="17" /><span>支持的格式与文件大小以上传策略为准</span></div>
        <div class="generation-create__aside-foot"><MessageSquareText :size="17" /><span>编制结果建议经业务人员核对后再使用</span></div>
      </aside>
    </div>
  </div>
</template>

<style scoped>
.generation-create { --generation-ink: #182a3a; --generation-muted: #748292; --generation-line: #e7edf2; padding: 24px 28px 28px; }
.generation-create__topline { display: flex; align-items: center; gap: 18px; }
.generation-create__eyebrow { display: inline-flex; align-items: center; gap: 7px; color: #317c78; font-size: 12px; font-weight: 700; letter-spacing: .045em; text-transform: uppercase; }
.generation-create__intro { display: flex; align-items: flex-end; justify-content: space-between; gap: 28px; padding: 28px 0 24px; }
.generation-create__intro h1 { margin: 0; color: var(--generation-ink); font-size: clamp(25px, 3vw, 34px); font-weight: 650; letter-spacing: -.035em; line-height: 1.25; }
.generation-create__intro p { max-width: 650px; margin: 10px 0 0; color: var(--generation-muted); font-size: 14px; line-height: 1.75; }
.generation-create__flow { display: flex; flex: 0 0 auto; align-items: center; gap: 12px; padding-bottom: 4px; color: #a0abb5; font-size: 12px; }
.generation-create__flow span { display: flex; align-items: center; gap: 7px; white-space: nowrap; }
.generation-create__flow span.is-current { color: #287570; font-weight: 650; }
.generation-create__flow i { display: grid; width: 22px; height: 22px; place-items: center; border: 1px solid #dce4e8; border-radius: 50%; font-size: 11px; font-style: normal; }
.generation-create__flow .is-current i { border-color: #b8d9d5; background: #eaf5f3; }
.generation-create__grid { display: grid; grid-template-columns: minmax(0, 1fr) 292px; align-items: start; gap: 24px; margin-top: 20px; }
.generation-create__form-pane { min-width: 0; padding: 22px 24px 0; border: 1px solid var(--generation-line); border-radius: 8px; background: #fff; }
.generation-create__section-heading { display: flex; align-items: flex-start; gap: 13px; margin-bottom: 18px; }
.generation-create__step { display: grid; flex: 0 0 32px; width: 32px; height: 32px; place-items: center; border: 1px solid #cfe3e1; border-radius: 50%; background: #f1f8f7; color: #317c78; font-size: 11px; font-weight: 700; }
.generation-create__section-heading h2 { margin: 0; color: var(--generation-ink); font-size: 16px; font-weight: 650; line-height: 1.4; }
.generation-create__section-heading h2 span { margin-left: 6px; color: #8c99a4; font-size: 11px; font-weight: 500; }
.generation-create__section-heading p { margin: 4px 0 0; color: var(--generation-muted); font-size: 12px; line-height: 1.5; }
.generation-create__upload { padding-left: 45px; }
.generation-create__upload :deep(.upload-block .el-upload-dragger) { padding: 25px 16px; border-color: #d8e2e7; background: #fbfcfd; }
.generation-create__upload :deep(.upload-block__icon) { color: #4b928c; font-size: 34px; }
.generation-create__upload :deep(.el-upload__text) { color: #405366; font-size: 13px; }
.generation-create__upload :deep(.el-upload__text em) { color: #287570; font-style: normal; font-weight: 650; }
.generation-create__upload :deep(.el-upload__tip) { margin-top: 9px; color: #8996a1; font-size: 11px; }
.generation-create :deep(.el-divider) { margin: 22px 0; border-color: #edf0f3; }
.generation-create__fields { padding-left: 45px; }
.generation-create__fields :deep(.el-form-item) { margin-bottom: 16px; }
.generation-create__fields :deep(.el-form-item__label) { padding-bottom: 6px; color: #455667; font-size: 12px; }
.generation-create__fields :deep(.el-textarea__inner) { min-height: 82px !important; padding: 10px 12px; border-color: #e0e7eb; box-shadow: none; font-size: 13px; line-height: 1.65; }
.generation-create__fields :deep(.el-textarea__inner:focus) { border-color: #79aaa5; }
.generation-create__fields :deep(.el-input__count) { background: transparent; color: #9aa5ae; font-size: 10px; }
.generation-create__submit { display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between; gap: 14px; margin: 8px -24px 0; padding: 16px 24px; border-top: 1px solid #edf0f3; background: #fbfcfc; border-radius: 0 0 8px 8px; }
.generation-create__submit-note { display: flex; align-items: center; gap: 8px; color: #72818d; font-size: 11px; }
.generation-create__submit-note .el-icon { color: #478d77; font-size: 15px; }
.generation-actions { display: flex; flex-wrap: wrap; align-items: center; gap: 12px; }
.generation-actions :deep(.el-button + .el-button) { margin-left: 0; }
.generation-actions :deep(.el-button--primary) { --el-button-bg-color: #287a75; --el-button-border-color: #287a75; --el-button-hover-bg-color: #216b66; --el-button-hover-border-color: #216b66; }
.generation-file-progress { max-width: 420px; margin: 8px 0 4px; }
.generation-create__aside { position: sticky; top: 18px; overflow: hidden; padding: 21px 19px 17px; border: 1px solid #243949; border-radius: 8px; background: #203544; color: #fff; }
.generation-create__aside-heading { display: flex; align-items: center; gap: 11px; padding-bottom: 17px; border-bottom: 1px solid rgba(255,255,255,.12); }
.generation-create__aside-heading > span { display: grid; width: 32px; height: 32px; place-items: center; border: 1px solid rgba(142,202,190,.28); border-radius: 7px; background: rgba(94,165,149,.15); color: #a7d7ca; }
.generation-create__aside-heading div { display: flex; flex-direction: column; gap: 3px; }
.generation-create__aside-heading strong { font-size: 13px; font-weight: 650; }
.generation-create__aside-heading small { color: #a6b6c0; font-size: 10px; }
.generation-create__benefits { display: grid; gap: 18px; margin: 19px 0; padding: 0; list-style: none; }
.generation-create__benefits li { display: flex; align-items: flex-start; gap: 10px; }
.generation-create__benefits li > i { display: grid; flex: 0 0 19px; width: 19px; height: 19px; place-items: center; border-radius: 50%; background: rgba(113,190,167,.14); color: #9bd5c1; font-style: normal; }
.generation-create__benefits li span { display: flex; flex-direction: column; gap: 4px; }
.generation-create__benefits strong { color: #f0f5f6; font-size: 12px; font-weight: 600; }
.generation-create__benefits small { color: #a9b8c1; font-size: 10px; line-height: 1.55; }
.generation-create__aside-foot { display: flex; align-items: flex-start; gap: 8px; padding: 11px 0 0; border-top: 1px solid rgba(255,255,255,.1); color: #aab9c2; font-size: 10px; line-height: 1.5; }
.generation-create__aside-foot + .generation-create__aside-foot { margin-top: 10px; padding-top: 0; border-top: 0; }
.generation-create__aside-foot svg { flex: 0 0 auto; color: #98c8bd; }
@media (max-width: 1050px) { .generation-create__grid { grid-template-columns: minmax(0, 1fr) 260px; gap: 16px; } .generation-create__intro { align-items: flex-start; flex-direction: column; gap: 18px; } }
@media (max-width: 760px) { .generation-create { padding: 18px 16px 20px; } .generation-create__topline { flex-wrap: wrap; gap: 12px; } .generation-create__intro { padding: 22px 0 18px; } .generation-create__flow { flex-wrap: wrap; gap: 8px; } .generation-create__grid { grid-template-columns: minmax(0, 1fr); } .generation-create__aside { position: static; } .generation-create__form-pane { padding: 18px 16px 0; } .generation-create__upload, .generation-create__fields { padding-left: 0; } .generation-create__submit { margin-right: -16px; margin-left: -16px; padding: 14px 16px; } .generation-create__submit-note { flex-basis: 100%; } .generation-actions { width: 100%; } .generation-actions :deep(.el-button) { flex: 1; } }
@media (max-width: 440px) { .generation-create__flow { font-size: 10px; gap: 5px; } .generation-create__flow span { gap: 4px; } .generation-create__flow i { width: 19px; height: 19px; } .generation-create__intro h1 { font-size: 25px; } .generation-actions { flex-direction: column; align-items: stretch; } .generation-actions :deep(.el-button) { width: 100%; } }
</style>
