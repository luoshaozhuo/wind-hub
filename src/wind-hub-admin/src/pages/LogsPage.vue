<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useViewport } from '../composables/useViewport'

interface Entry {
  time: string
  level: 'ERROR' | 'WARN' | 'INFO'
  source: string
  object: string
  message: string
}

const sources = ['ads', 'modbus', 'iec104', 'task', 'runtime', 'sink']
const samples: [Entry['level'], string, string, string][] = [
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
  const time = new Date(2026, 8, 27, 15, 43 + Math.floor(i / 6), (i * 7) % 60)
  return { time: time.toLocaleTimeString('en-GB'), level, source, object, message }
})

const level = ref<'All' | Entry['level']>('ERROR')
const source = ref('All')
const keyword = ref('')
const page = ref(1)
const pageSize = ref(20)
const { isMobile, isTablet } = useViewport()

const filteredLogs = computed(() => logs.filter(entry =>
  (level.value === 'All' || entry.level === level.value) &&
  (source.value === 'All' || entry.source === source.value) &&
  (!keyword.value || `${entry.source} ${entry.object} ${entry.message}`.toLowerCase().includes(keyword.value.toLowerCase()))
))

const pagedLogs = computed(() => {
  const start = (page.value - 1) * pageSize.value
  return filteredLogs.value.slice(start, start + pageSize.value)
})

watch([level, source, keyword, pageSize], () => {
  page.value = 1
})
</script>

<template>
  <div class="standard-page logs-page">
    <div class="head">
      <div>
        <h1>Logs</h1>
        <p>运行日志与错误查询</p>
      </div>
    </div>

    <el-card shadow="never">
      <div class="logs-toolbar">
        <div class="row"><b>Log Stream</b><el-tag>mock</el-tag></div>
        <div class="logs-filters">
          <el-select v-model="level" aria-label="Log level">
            <el-option v-for="item in ['ERROR','WARN','INFO','All']" :key="item" :label="item" :value="item" />
          </el-select>
          <el-select v-model="source" aria-label="Log source">
            <el-option label="All Sources" value="All" />
            <el-option v-for="item in sources" :key="item" :label="item" :value="item" />
          </el-select>
          <el-input v-model="keyword" clearable placeholder="Search source / object / message..." />
        </div>
      </div>

      <el-table :data="pagedLogs" height="var(--app-table-viewport-height)" empty-text="No logs match current filters">
        <el-table-column v-if="!isMobile" prop="time" label="Time" width="110" />
        <el-table-column label="Level" width="100">
          <template #default="{ row }">
            <el-tag :type="row.level === 'ERROR' ? 'danger' : row.level === 'WARN' ? 'warning' : 'info'">{{ row.level }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column v-if="!isTablet && !isMobile" prop="source" label="Source" width="110" />
        <el-table-column v-if="!isMobile" prop="object" label="Object" width="160" />
        <el-table-column prop="message" label="Message" min-width="320" />
      </el-table>

      <div class="pagination">
        <el-pagination
          v-model:current-page="page"
          v-model:page-size="pageSize"
          :page-sizes="[20, 50, 100]"
          :total="filteredLogs.length"
          :layout="isMobile ? 'prev, pager, next' : 'total, sizes, prev, pager, next'"
        />
      </div>
    </el-card>
  </div>
</template>

<style scoped>
.logs-toolbar{display:flex;align-items:center;justify-content:space-between;gap:var(--app-space-4);margin-bottom:var(--app-space-3)}
.logs-filters{display:grid;grid-template-columns:minmax(0,.7fr) minmax(0,.8fr) minmax(0,1.5fr);gap:var(--app-space-2)}
@media(max-width:1199px){.logs-toolbar{align-items:stretch;flex-direction:column}.logs-filters{grid-template-columns:repeat(2,minmax(0,1fr))}.logs-filters .el-input{grid-column:1/-1}}
@media(max-width:767px){.logs-filters{grid-template-columns:1fr}}
</style>
