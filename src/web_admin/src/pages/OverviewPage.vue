<script setup lang="ts">
import { onMounted, onUnmounted, ref } from 'vue'
import { runtimeApi } from '../api/runtime'

const deviceTotal = ref(0)
const deviceOnline = ref(0)
const taskTotal = ref(0)
const taskRunning = ref(0)
const sinkTotal = ref(0)
const sinkHealthy = ref(0)
const pointTotal = ref(0)
const warning = ref<string[]>([])
const loading = ref(false)
let timer: ReturnType<typeof setInterval> | undefined
let active = true

async function refresh() {
  if (loading.value) return
  loading.value = true
  try {
    const results = await Promise.allSettled([
      runtimeApi.devices(), runtimeApi.tasks(), runtimeApi.sinks(), runtimeApi.latestValues(),
    ] as const)
    if (!active) return
    const errors: string[] = []
    const [devices, tasks, sinks, values] = results
    if (devices.status === 'fulfilled') {
      deviceTotal.value = devices.value.length
      deviceOnline.value = devices.value.filter(d => d.connected).length
    } else errors.push('设备状态不可用：' + String(devices.reason))
    if (tasks.status === 'fulfilled') {
      taskTotal.value = tasks.value.length
      taskRunning.value = tasks.value.filter(t => t.runtime_state.toLowerCase() === 'running').length
    } else errors.push('采集任务不可用：' + String(tasks.reason))
    if (sinks.status === 'fulfilled') {
      sinkTotal.value = sinks.value.length
      sinkHealthy.value = sinks.value.filter(s => s.healthy).length
    } else errors.push('Sink 状态不可用：' + String(sinks.reason))
    if (values.status === 'fulfilled') pointTotal.value = values.value.length
    else errors.push('最新测点值不可用：' + String(values.reason))
    warning.value = errors
  } finally {
    loading.value = false
  }
}
onMounted(() => {
  active = true
  void refresh()
  timer = setInterval(() => void refresh(), 5000)
})
onUnmounted(() => { active = false; if (timer) clearInterval(timer) })
</script>

<template>
  <section class="runtime-page">
    <h2>运行总览</h2>
    <el-alert v-for="item in warning" :key="item" :title="item" type="warning" show-icon :closable="false" />
    <el-row :gutter="16">
      <el-col :xs="24" :sm="12" :lg="6">
        <el-card shadow="never"><el-statistic title="通信正常设备 / 全部设备" :value="deviceOnline" /><div>总数 {{ deviceTotal }}</div></el-card>
      </el-col>
      <el-col :xs="24" :sm="12" :lg="6">
        <el-card shadow="never"><el-statistic title="运行中任务 / 全部任务" :value="taskRunning" /><div>总数 {{ taskTotal }}</div></el-card>
      </el-col>
      <el-col :xs="24" :sm="12" :lg="6">
        <el-card shadow="never"><el-statistic title="健康 Sink / 全部 Sink" :value="sinkHealthy" /><div>总数 {{ sinkTotal }}</div></el-card>
      </el-col>
      <el-col :xs="24" :sm="12" :lg="6">
        <el-card shadow="never"><el-statistic title="最新测点记录" :value="pointTotal" /><div>由 Collector 最新值接口提供</div></el-card>
      </el-col>
    </el-row>
    <p>每 5 秒自动更新；接口异常时保留上次成功值并显示警告。</p>
  </section>
</template>
