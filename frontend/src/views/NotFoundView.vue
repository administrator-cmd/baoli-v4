<script setup lang="ts">
import { useRouter } from "vue-router";

/**
 * 未知路径：明确说明页面不存在并回到默认列表页，
 * 不猜测对象、不跳转到别的业务记录。
 */
const router = useRouter();
</script>

<template>
  <el-card shadow="never">
    <el-empty description="页面不存在">
      <template #description>
        <p>当前地址没有对应的页面；可能是链接已失效或输入有误。</p>
      </template>
      <el-button type="primary" @click="router.push('/audit-tasks')">返回审核任务</el-button>
    </el-empty>
  </el-card>
</template>
