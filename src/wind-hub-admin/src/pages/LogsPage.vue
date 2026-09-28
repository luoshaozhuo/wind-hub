<script setup lang="ts">
import { computed, onBeforeUnmount, ref } from 'vue'

// 运行日志查询：level 过滤 + 关键字搜索（mock 数据，不接日志后端）
interface Entry { time: string, level: string, source: string, object: string, message: string }
const sources = ['ads', 'modbus', 'iec104', 'task', 'runtime', 'sink']
const samples: [string, string, string, string][] = [
  ['INFO', 'ads', 'wtg-040', 'connected (AMS route OK)'],
  ['INFO', 'modbus', 'wtg-002', 'connected 192.168.100.102:502'],
  ['WARN', 'task', 'turbine-ads-all', 'interval overrun 42 ms'],
  ['ERROR', 'modbus', 'wtg-003', 'read timeout after 5000 ms'],
  ['ERROR', 'ads', 'wtg-041', 'disconnected, retrying'],
  ['INFO', 'runtime', 'engine', 'config reloaded (3 tasks updated)'],
  ['WARN', 'sink', 'file_archive', 'queue usage 82%'],
  ['INFO', 'task', 'pcs-fast', 'started'],
  ['ERROR', 'sink', 'file_archive', 'write error: disk almost full (recovered)'],
  ['INFO', 'ads', 'wtg-041', 'reconnect failed, backoff 8 s'],
]
const logs: Entry[] = Array.from({ length: 140 }, (_, i) => {
  const [level, source, object, message] = samples[i % samples.length]
  const t = new Date(2026, 8, 27, 15, 43 + Math.floor(i / 6), (i * 7) % 60)
  return { time: t.toLocaleTimeString('en-GB'), level, source, object, message }
})

const level = ref('ERROR')
const limit = ref(20)
const keyword = ref('')
const viewportWidth = ref(window.innerWidth)
const isMobile = computed(() => viewportWidth.value < 768)
const isTablet = computed(() => viewportWidth.value < 1200)
function updateViewport() { viewportWidth.value = window.innerWidth }
window.addEventListener('resize', updateViewport)
onBeforeUnmount(() => window.removeEventListener('resize', updateViewport))
const visible = computed(() => logs
  .filter(l =>
    (level.value === 'All' || l.level === level.value) &&
    (!keyword.value || `${l.source} ${l.object} ${l.message}`.toLowerCase().includes(keyword.value.toLowerCase()))
  )
  .slice(0, limit.value))
</script>

<template><div class="standard-page"><div class="head"><div><h1>Logs</h1><p>运行日志与错误查询</p></div></div><el-card shadow="never"><div class="toolbar"><div class="row"><b>Log Stream</b><el-tag>mock</el-tag></div><div class="row">
  <el-select v-model="level" style="width:130px">
    <el-option v-for="l in ['ERROR','WARN','INFO','All']" :key="l" :label="l" :value="l"/>
  </el-select>
  <el-select v-model="limit" style="width:120px">
    <el-option :value="20" label="20 rows"/>
    <el-option :value="50" label="50 rows"/>
    <el-option :value="100" label="100 rows"/>
  </el-select>
  <el-input v-model="keyword" placeholder="Search source / object / message..." clearable style="width:280px"/>
</div></div><el-table :data="visible" height="560"><el-table-column v-if="!isMobile" prop="time" label="Time" width="110"/><el-table-column label="Level" width="100"><template #default="s"><el-tag :type="s.row.level==='ERROR'?'danger':s.row.level==='WARN'?'warning':'info'">{{s.row.level}}</el-tag></template></el-table-column><el-table-column v-if="!isTablet" prop="source" label="Source" width="110"/><el-table-column v-if="!isMobile" prop="object" label="Object" width="160"/><el-table-column prop="message" label="Message" min-width="320"/></el-table></el-card></div></template>
