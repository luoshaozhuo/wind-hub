<script setup lang="ts">
import { computed } from 'vue'
import { store } from '../mock/data'

// 运行总览：状态与采集健康摘要（非业务功率曲线页）
const online = computed(() => store.devices.filter(d => d.online).length)
const running = computed(() => store.tasks.filter(t => t.runtime === 'RUNNING').length)
const sinkOk = computed(() => store.sinks.filter(s => s.enabled).length)

const metrics = computed(() => [
  ['Runtime', 'RUNNING'],
  ['Devices', `${online.value} / ${store.devices.length}`],
  ['Tasks', `${running.value} / ${store.tasks.length}`],
  ['Acquisition', '99.92%'],
  ['Active Errors', String(errors.length)],
  ['Sinks', `${sinkOk.value} / ${store.sinks.length}`],
])

// 采集异常摘要：断连 / 超时 / 节拍 overrun / sink 错误
const errors = [
  { level: 'ERROR', object: 'wtg-041', event: 'ADS disconnected' },
  { level: 'ERROR', object: 'file_archive', event: 'sink write error (recovered)' },
  { level: 'WARN', object: 'turbine-ads-all', event: 'Interval overrun 42 ms' },
  { level: 'WARN', object: 'wtg-003', event: 'Modbus read timeout' },
]
</script>

<template><div><div class="head"><div><h1>Overview</h1><p>Wind Hub 运行状态与采集健康摘要</p></div></div><div class="metrics"><el-card v-for="x in metrics" shadow="never"><small>{{x[0]}}</small><strong>{{x[1]}}</strong></el-card></div><el-card shadow="never"><h3>当前异常</h3><el-table :data="errors"><el-table-column prop="level" label="Level" width="110"><template #default="s"><el-tag :type="s.row.level==='ERROR'?'danger':'warning'">{{s.row.level}}</el-tag></template></el-table-column><el-table-column prop="object" label="Object"/><el-table-column prop="event" label="Event"/></el-table></el-card></div></template>
