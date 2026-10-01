<script setup lang="ts">
import { onMounted, ref, watch } from 'vue'
import { useViewport } from '../composables/useViewport'
import { api } from '../api/client'
import { queryLogs } from '../api/runtime'
import type { RuntimeLogEntry } from '../api/runtime'

type Entry = RuntimeLogEntry
const rows=ref<Entry[]>([])
const sources=ref<string[]>([])
const total=ref(0)
const loading=ref(false)
const level=ref<'All'|Entry['level']>('ERROR')
const source=ref('All')
const keyword=ref('')
const page=ref(1)
const pageSize=ref(20)
const {isMobile,isTablet}=useViewport()

async function load(){
  if(loading.value)return
  loading.value=true
  try{
    const result=await queryLogs({
      page:page.value,pageSize:pageSize.value,
      level:level.value,source:source.value,keyword:keyword.value,
    })
    rows.value=result.items.map(entry=>({
      time:entry.timestamp.replace('T',' ').replace('Z','').slice(0,19),
      level:(entry.level==='WARNING'?'WARN':entry.level) as Entry['level'],
      source:entry.source,object:entry.object,message:entry.message,
    }))
    total.value=result.page.total
  }finally{loading.value=false}
}
async function loadSources(){
  sources.value=await api<string[]>('/logs/sources')
}
watch([level,source,keyword,pageSize],()=>{page.value=1;void load()})
watch(page,()=>{void load()})
onMounted(()=>{void Promise.all([load(),loadSources()])})
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
        <div class="row"><b>Log Stream</b><el-tag type="success">live</el-tag></div>
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

      <el-table v-loading="loading" :data="rows" height="var(--app-table-viewport-height)" empty-text="No logs match current filters">
        <el-table-column v-if="!isMobile" prop="time" label="Time" width="180" />
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
          :total="total"
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
