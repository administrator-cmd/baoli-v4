<script setup lang="ts">
import { useRouter } from "vue-router";
import type { ConfigOrigin, DraftCalculationItem } from "@/api/contracts/scenes";

defineProps<{
  origin?: ConfigOrigin | null;
  parameterSources: DraftCalculationItem["parameter_sources"];
  bandSources: DraftCalculationItem["band_sources"];
  showJobLink?: boolean;
}>();

const router = useRouter();

function sourceText(origin: ConfigOrigin | null | undefined): string {
  return origin ? `${origin.sheet_name} 第 ${origin.row_no} 行` : "未记录原表来源";
}

function openSource(origin: ConfigOrigin): void {
  if (origin.import_job_no) {
    void router.push({ name: "scene-import-detail", params: { jobNo: origin.import_job_no } });
  }
}
</script>

<template>
  <div class="calculation-sources">
    <div class="calculation-source-row">
      <strong>定义</strong>
      <span>{{ sourceText(origin) }}</span>
      <el-button v-if="showJobLink !== false && origin?.import_job_no" link type="primary" @click="openSource(origin)">查看导入作业</el-button>
    </div>
    <div v-for="(parameter, index) in parameterSources" :key="`parameter-${index}`" class="calculation-source-row">
      <strong>参数 {{ parameter.code || "未填写编号" }}</strong>
      <span>{{ sourceText(parameter.origin) }}</span>
      <el-button v-if="showJobLink !== false && parameter.origin?.import_job_no" link type="primary" @click="openSource(parameter.origin)">查看导入作业</el-button>
    </div>
    <div v-for="(band, index) in bandSources" :key="`band-${index}`" class="calculation-source-row">
      <strong>判定 {{ band.code || "未填写编号" }}</strong>
      <span>{{ sourceText(band.origin) }}</span>
      <el-button v-if="showJobLink !== false && band.origin?.import_job_no" link type="primary" @click="openSource(band.origin)">查看导入作业</el-button>
    </div>
  </div>
</template>

<style scoped>
.calculation-sources {
  display: grid;
  gap: 8px;
  padding: 8px 16px;
}
.calculation-source-row {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px 16px;
}
.calculation-source-row strong {
  min-width: 100px;
}
</style>
