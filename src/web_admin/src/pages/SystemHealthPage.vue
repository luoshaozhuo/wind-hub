<script setup lang="ts">
import { onMounted, onUnmounted, ref } from 'vue'
import { runtimeApi } from '../api/runtime'

type StatusSection = { title: string; data: Record<string, unknown> | null; error: string }
const sections = ref<StatusSection[]>([
  { title: 'Collector 状态', data: null, error: '' },
  { title: 'Commander 状态', data: null, error: '' },
  { title: 'Collector 指标', data: null, error: '' },
  { title: 'Collector 信息', data: null, error: '' },
])
let interval: ReturnType<typeof setInterval> | undefined
let active = true
let pending = false

async function refresh() {
  if (pending) return
  pending = true
  try {
    const results = await Promise.allSettled([
      runtimeApi.collectorStatus(),
      runtimeApi.commanderStatus(),
      runtimeApi.collectorMetrics(),
      runtimeApi.collectorInfo(),
    ])
    if (!active) return
    sections.value = sections.value.map((item, index) => {
      const result = results[index]
      return result.status === 'fulfilled'
        ? { ...item, data: result.value, error: '' }
        : { ...item, error: String(result.reason) }
    })
  } finally {
    pending = false
  }
}
onMounted(() => {
  active = true
  void refresh()
  interval = setInterval(() => void refresh(), 5000)
})
onUnmounted(() => {
  active = false
  if (interval) clearInterval(interval)
})
</script>

<template>
  <section class="runtime-page">
    <h2>系统健康</h2>
    <p>直接读取 Web Server 当前暴露的 Collector / Commander 状态和指标，每 5 秒自动刷新。通信失败不会显示为健康。</p>
    <el-row :gutter="16">
      <el-col v-for="section in sections" :key="section.title" :xs="24" :lg="12">
        <el-card shadow="never" style="margin-bottom: 16px">
          <template #header><strong>{{ section.title }}</strong></template>
          <el-alert v-if="section.error" type="error" :title="section.error" show-icon :closable="false" />
          <el-empty v-else-if="!section.data" description="尚未收到数据" />
          <el-descriptions v-else :column="1" border>
            <el-descriptions-item v-for="(value, key) in section.data" :key="String(key)" :label="String(key)">
              {{ typeof value === 'object' ? JSON.stringify(value) : String(value) }}
            </el-descriptions-item>
          </el-descriptions>
        </el-card>
      </el-col>
    </el-row>
  </section>
</template>
