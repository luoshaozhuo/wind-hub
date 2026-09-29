<script setup lang="ts">
import { computed, onBeforeUnmount, ref } from 'vue'
import { protocolOfDevice, store } from '../mock/data'

const viewportWidth = ref(window.innerWidth)
const isMobile = computed(() => viewportWidth.value < 768)
const isTablet = computed(() => viewportWidth.value < 1200)
function updateViewport() { viewportWidth.value = window.innerWidth }
window.addEventListener('resize', updateViewport)
onBeforeUnmount(() => window.removeEventListener('resize', updateViewport))

// 采集服务质量（节拍、缺失、中断、超时与恢复）—— 非电能质量
const metrics = [
  { title: '平均采样间隔', value: 1.002, precision: 3, suffix: 's' },
  { title: 'Jitter P95', value: 36, suffix: 'ms' },
  { title: '当前中断', value: 2, suffix: '' },
  { title: 'Reconnect', value: 7, suffix: '· 24 h' },
  { title: 'Read Timeout', value: 13, suffix: '· 24 h' },
  { title: 'Read Failure', value: 4, suffix: '· 24 h' },
]

// 任务级质量：期望节拍 vs 实际节拍 / 抖动 / 缺失 / 中断 / 成功率
const taskRows = computed(() => store.tasks.map((t, i) => ({
  task: t.task_id,
  interval: t.interval === null ? '订阅' : `${Number(t.interval).toFixed(3)} s`,
  actual: t.interval === null ? '—' : `${(Number(t.interval) + 0.002 + i * 0.003).toFixed(3)} s`,
  jitter: `${36 + i * 15} ms`,
  missing: t.runtime === 'RUNNING' ? 3 + i * 6 : 0,
  interruptions: t.runtime === 'RUNNING' ? 1 + (i % 2) : 0,
  last: t.runtime === 'RUNNING' ? `${80 + i * 120} ms ago` : '—',
  rate: t.runtime === 'RUNNING' ? `${(99.93 - i * 0.22).toFixed(2)}%` : '—',
})))

// 设备级采集健康：最后成功采集 / 超时 / 读失败 / 状态
const deviceRows = computed(() => store.devices.filter(d => d.enabled).slice(0, 12).map((d, i) => ({
  device: d.device_id,
  protocol: protocolOfDevice(d),
  last: d.online ? `${120 + i * 45} ms ago` : '—',
  timeouts: d.online ? i % 4 : 0,
  failures: d.online ? i % 3 : 18,
  status: d.online ? 'healthy' : 'interrupted',
})))
</script>

<template><div class="standard-page"><div class="head"><div><h1>Quality</h1><p>工程诊断入口：持续发现节拍、缺失、中断、超时与恢复异常</p></div></div>
<div class="metrics"><el-card v-for="x in metrics" :key="x.title" shadow="never"><el-statistic :title="x.title" :value="x.value" :precision="x.precision" :suffix="x.suffix" /></el-card></div>
<el-card shadow="never"><h3>任务采集质量</h3><el-table :data="taskRows"><el-table-column prop="task" label="Task"/><el-table-column prop="interval" label="Expected"/><el-table-column v-if="!isMobile" prop="actual" label="Actual avg"/><el-table-column v-if="!isTablet" prop="jitter" label="Jitter P95"/><el-table-column v-if="!isTablet" prop="missing" label="Missing"/><el-table-column v-if="!isMobile" prop="interruptions" label="Interruptions"/><el-table-column v-if="!isMobile" prop="last" label="Last Success"/><el-table-column prop="rate" label="Success"/></el-table></el-card>
<el-card shadow="never" style="margin-top:16px"><h3>设备采集健康</h3><el-table :data="deviceRows" height="360"><el-table-column prop="device" label="Device"/><el-table-column v-if="!isMobile" prop="protocol" label="Protocol"/><el-table-column prop="last" label="Last Success"/><el-table-column v-if="!isTablet" prop="timeouts" label="Timeouts"/><el-table-column v-if="!isMobile" prop="failures" label="Read Failures"/><el-table-column label="Status"><template #default="s"><el-tag :type="s.row.status==='healthy'?'success':'danger'">{{s.row.status}}</el-tag></template></el-table-column></el-table></el-card></div></template>
