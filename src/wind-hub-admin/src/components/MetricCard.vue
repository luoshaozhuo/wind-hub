<script setup lang="ts">
defineProps<{ title: string; value: string | number; hint?: string }>()
</script>

<template>
  <el-card shadow="never" class="metric-card">
    <div class="metric-title">{{ title }}</div>
    <div class="metric-value">{{ value }}</div>
    <div v-if="hint" class="metric-hint">{{ hint }}</div>
  </el-card>
</template>
