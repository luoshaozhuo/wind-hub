<script setup lang="ts">
import { computed, onBeforeUnmount, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { refreshTaskValidity, store } from '../mock/data'
import type { SinkDef, SinkRuntimeState, SinkType } from '../mock/types'

const viewportWidth = ref(window.innerWidth)
const isMobile = computed(() => viewportWidth.value < 768)
const drawerSize = computed(() => isMobile.value ? '100%' : 'min(860px, 82vw)')
const search = ref('')
const typeFilter = ref<'All' | SinkType>('All')
const stateFilter = ref<'All' | SinkRuntimeState>('All')
const drawerOpen = ref(false)
const drawerTab = ref('Overview')
const creating = ref(false)
const selectedName = ref('')
const testLoading = ref(false)
const testStage = ref<'idle' | 'connection' | 'write'>('idle')
const testResult = ref<{ ok: boolean; title: string; detail: string; latency: number } | null>(null)

function updateViewport(){ viewportWidth.value = window.innerWidth }
window.addEventListener('resize', updateViewport)
onBeforeUnmount(() => window.removeEventListener('resize', updateViewport))

const selected = computed(() => store.sinks.find(s => s.name === selectedName.value))
const rows = computed(() => store.sinks.filter(s => {
  const q = search.value.trim().toLowerCase()
  const matchQ = !q || [s.name, s.type, endpointSummary(s)].some(x => x.toLowerCase().includes(q))
  const matchType = typeFilter.value === 'All' || s.type === typeFilter.value
  const matchState = stateFilter.value === 'All' || s.runtime_state === stateFilter.value
  return matchQ && matchType && matchState
}))

const draft = reactive({
  name: '',
  type: 'file' as SinkType,
  enabled: true,
  bootstrap_servers: '',
  topic: '',
  key_field: '',
  compression_type: '',
  acks: 'all',
  retries: 3,
  kafka_batch_size: 16384,
  linger_ms: 0,
  dsn: '',
  table: 'points',
  db_batch_size: 1000,
  create_table: false,
  pool_min_size: 1,
  pool_max_size: 10,
  path: '',
  format: 'jsonl',
  max_size_mb: 100,
  max_age_hours: 24,
  compress: false,
  compress_level: 6,
  buffer_size: 100,
  flush_interval: 1,
  write_header: true,
})

function nowText() {
  const d = new Date()
  const p = (n:number) => String(n).padStart(2,'0')
  return `${d.getFullYear()}-${p(d.getMonth()+1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`
}
function sleep(ms:number){ return new Promise(resolve => setTimeout(resolve, ms)) }

function stateType(state: SinkRuntimeState): ''|'success'|'warning'|'danger'|'info' {
  if (state === 'healthy') return 'success'
  if (state === 'warning') return 'warning'
  if (state === 'failed') return 'danger'
  if (state === 'testing') return ''
  return 'info'
}
function stateLabel(s: SinkDef) {
  if (!s.enabled) return 'Disabled'
  return s.runtime_state.charAt(0).toUpperCase() + s.runtime_state.slice(1)
}
function endpointSummary(s: SinkDef) {
  if (s.type === 'kafka') return `${s.params.bootstrap_servers || '—'} · ${s.params.topic || '—'}`
  if (s.type === 'db') {
    const dsn = String(s.params.dsn || '')
    return `${dsn.replace(/:\/\/([^:]+):[^@]+@/, '://$1:***@') || '—'} · ${s.params.table || '—'}`
  }
  return String(s.params.path || '—')
}
function taskRefs(name:string){ return store.tasks.filter(t => t.sinks.includes(name)) }

function resetDraft(type: SinkType = 'file') {
  Object.assign(draft, {
    name: '', type, enabled: true,
    bootstrap_servers: '', topic: '', key_field: '', compression_type: '', acks: 'all', retries: 3, kafka_batch_size: 16384, linger_ms: 0,
    dsn: '', table: 'points', db_batch_size: 1000, create_table: false, pool_min_size: 1, pool_max_size: 10,
    path: '', format: 'jsonl', max_size_mb: 100, max_age_hours: 24, compress: false, compress_level: 6, buffer_size: 100, flush_interval: 1, write_header: true,
  })
}
function loadDraft(s: SinkDef) {
  resetDraft(s.type)
  draft.name = s.name
  draft.enabled = s.enabled
  const p=s.params
  if(s.type==='kafka'){
    draft.bootstrap_servers=String(p.bootstrap_servers||''); draft.topic=String(p.topic||'')
    draft.key_field=String(p.key_field||''); draft.compression_type=String(p.compression_type||'')
    draft.acks=String(p.acks??'all'); draft.retries=Number(p.retries??3)
    draft.kafka_batch_size=Number(p.batch_size??16384); draft.linger_ms=Number(p.linger_ms??0)
  }else if(s.type==='db'){
    draft.dsn=String(p.dsn||''); draft.table=String(p.table||'points')
    draft.db_batch_size=Number(p.batch_size??1000); draft.create_table=Boolean(p.create_table)
    draft.pool_min_size=Number(p.pool_min_size??1); draft.pool_max_size=Number(p.pool_max_size??10)
  }else{
    draft.path=String(p.path||''); draft.format=String(p.format||'jsonl')
    draft.max_size_mb=Number(p.max_size_mb??100); draft.max_age_hours=Number(p.max_age_hours??24)
    draft.compress=Boolean(p.compress); draft.compress_level=Number(p.compress_level??6)
    draft.buffer_size=Number(p.buffer_size??100); draft.flush_interval=Number(p.flush_interval??1)
    draft.write_header=Boolean(p.write_header??true)
  }
}
function openSink(s: SinkDef) {
  selectedName.value=s.name
  drawerTab.value='Config'
  creating.value=false
  testResult.value=null
  loadDraft(s)
  drawerOpen.value=true
}
function openAdd() {
  selectedName.value=''
  creating.value=true
  drawerTab.value='Config'
  testResult.value=null
  resetDraft('file')
  drawerOpen.value=true
}
function cancelEdit(){
  if(creating.value){ drawerOpen.value=false; return }
  if(selected.value) loadDraft(selected.value)
  drawerTab.value='Overview'
}
function onTypeChange(){ const name=draft.name; const enabled=draft.enabled; resetDraft(draft.type); draft.name=name; draft.enabled=enabled }

function paramsFromDraft(): Record<string,unknown> {
  if(draft.type==='kafka') return {
    bootstrap_servers:draft.bootstrap_servers.trim(), topic:draft.topic.trim(),
    key_field:draft.key_field || undefined, compression_type:draft.compression_type || undefined,
    acks:draft.acks, retries:draft.retries, batch_size:draft.kafka_batch_size, linger_ms:draft.linger_ms,
  }
  if(draft.type==='db') return {
    dsn:draft.dsn.trim(), table:draft.table.trim(), batch_size:draft.db_batch_size,
    create_table:draft.create_table, pool_min_size:draft.pool_min_size, pool_max_size:draft.pool_max_size,
  }
  return {
    path:draft.path.trim(), format:draft.format, max_size_mb:draft.max_size_mb,
    max_age_hours:draft.max_age_hours, compress:draft.compress, compress_level:draft.compress_level,
    buffer_size:draft.buffer_size, flush_interval:draft.flush_interval, write_header:draft.write_header,
  }
}
function validateDraft(): string {
  if(!draft.name.trim()) return 'Sink name is required'
  if(creating.value && store.sinks.some(s=>s.name===draft.name.trim())) return 'Sink name already exists'
  if(!creating.value && selected.value && draft.name.trim()!==selected.value.name) return 'Sink name is stable after creation'
  if(draft.type==='kafka' && (!draft.bootstrap_servers.trim() || !draft.topic.trim())) return 'Kafka bootstrap_servers and topic are required'
  if(draft.type==='db'){
    if(!draft.dsn.trim() || !draft.table.trim()) return 'PostgreSQL DSN and table are required'
    if(!/^[a-zA-Z_][a-zA-Z0-9_]*$/.test(draft.table.trim())) return 'Table must be a valid SQL identifier'
    if(draft.pool_min_size > draft.pool_max_size) return 'Pool min size must be <= max size'
  }
  if(draft.type==='file' && !draft.path.trim()) return 'File path is required'
  return ''
}
async function saveSink(){
  const error=validateDraft(); if(error){ ElMessage.error(error); return }
  const params=paramsFromDraft()
  if(creating.value){
    const sink: SinkDef = {
      name:draft.name.trim(), type:draft.type, enabled:draft.enabled, params,
      runtime_state:draft.enabled?'unknown':'disabled', last_test_at:'', last_write_at:'',
      latency_ms:0,error:'',queue_depth:0,writes_total:0,failures_total:0,dropped_points:0,
    }
    store.sinks.push(sink); selectedName.value=sink.name; creating.value=false
  }else if(selected.value){
    const refs=taskRefs(selected.value.name)
    const running=refs.filter(t=>t.runtime==='RUNNING')
    if((selected.value.type!==draft.type || JSON.stringify(selected.value.params)!==JSON.stringify(params) || selected.value.enabled!==draft.enabled) && refs.length){
      await ElMessageBox.confirm(
        '<b>Sink Change Impact</b><br><br>'+refs.length+' Task(s) reference this Sink; '+running.length+' currently running.<br>'+
        'The Sink instance will be reopened. Disabling it makes referencing Tasks INVALID.',
        'Apply Sink Changes',
        {type:'warning',confirmButtonText:'Apply Changes',dangerouslyUseHTMLString:true},
      )
    }
    selected.value.type=draft.type
    selected.value.params=params
    selected.value.enabled=draft.enabled
    selected.value.runtime_state=draft.enabled?'unknown':'disabled'
    selected.value.error=''
  }
  refreshTaskValidity()
  ElMessage.success('Sink configuration saved (mock)')
}
async function toggleEnabled(s:SinkDef, enabled:boolean){
  const refs=taskRefs(s.name)
  if(!enabled && refs.length){
    try{
      await ElMessageBox.confirm(
        'Disabling "'+s.name+'" makes '+refs.length+' referencing Task(s) INVALID and stops any currently running affected Task.',
        'Disable Sink',
        {type:'warning',confirmButtonText:'Disable'},
      )
    }catch{ s.enabled=true; return }
  }
  s.runtime_state=enabled?'unknown':'disabled'
  refreshTaskValidity()
}
async function deleteSink(s:SinkDef){
  const refs=taskRefs(s.name)
  if(refs.length){ ElMessage.warning('Cannot delete: referenced by '+refs.length+' Task(s)'); return }
  await ElMessageBox.confirm('Delete Sink "'+s.name+'"?','Delete Sink',{type:'warning',confirmButtonText:'Delete'})
  store.sinks.splice(store.sinks.indexOf(s),1)
  drawerOpen.value=false
  ElMessage.success('Sink deleted (mock)')
}
function simulatedFailure(s:SinkDef){
  if(s.type==='kafka') return String(s.params.bootstrap_servers||'').includes('bad-host')
  if(s.type==='db') return String(s.params.dsn||'').includes('bad-host')
  return String(s.params.path||'').includes('/readonly/')
}
async function runConnectionTest(s:SinkDef){
  if(testLoading.value) return
  testLoading.value=true; testStage.value='connection'; testResult.value=null
  s.runtime_state='testing'
  await sleep(650)
  const failed=simulatedFailure(s)
  const latency=s.type==='file'?3:s.type==='db'?21:16
  s.last_test_at=nowText(); s.latency_ms=latency
  if(failed){
    s.runtime_state='failed'; s.error='Connection/open test failed (mock)'
    testResult.value={ok:false,title:'Connection test failed',detail:s.error,latency}
  }else{
    s.runtime_state=s.enabled?'healthy':'disabled'; s.error=''
    testResult.value={ok:true,title:'Connection test passed',detail: s.type==='file'?'Path can be opened for append.':'Remote endpoint accepted the connection/open operation.',latency}
  }
  testLoading.value=false; testStage.value='idle'
}
async function runWriteTest(s:SinkDef){
  if(testLoading.value) return
  await ElMessageBox.confirm(
    'Write one synthetic PointValue to "'+s.name+'". This test writes to the real configured destination in the future backend implementation.',
    'Write Test',
    {type:'warning',confirmButtonText:'Write Test'},
  )
  testLoading.value=true; testStage.value='write'; testResult.value=null; s.runtime_state='testing'
  await sleep(720)
  const failed=simulatedFailure(s)
  const latency=s.type==='file'?5:s.type==='db'?29:24
  s.last_test_at=nowText(); s.latency_ms=latency
  if(failed){
    s.runtime_state='failed'; s.error='Synthetic write failed (mock)'; s.failures_total += 1
    testResult.value={ok:false,title:'Write test failed',detail:s.error,latency}
  }else{
    s.runtime_state=s.enabled?'healthy':'disabled'; s.error=''; s.last_write_at=nowText(); s.writes_total += 1
    testResult.value={ok:true,title:'Write test passed',detail:'Synthetic PointValue accepted by the Sink (mock).',latency}
  }
  testLoading.value=false; testStage.value='idle'
}
async function testAll(){
  for(const s of store.sinks) await runConnectionTest(s)
  ElMessage.success('Sink verification complete (mock)')
}
</script>

<template>
  <div class="sinks-page">
    <div class="head page-head">
      <div><h1>Sinks</h1><p>输出端配置、运行状态、连通性与写入测试</p></div>
      <div class="head-actions">
        <el-button type="primary" @click="openAdd">+ Add Sink</el-button>
        <el-dropdown trigger="click">
          <el-button>Actions</el-button>
          <template #dropdown>
            <el-dropdown-menu>
              <el-dropdown-item :disabled="testLoading || !store.sinks.length" @click="testAll">Verify All Sinks</el-dropdown-item>
            </el-dropdown-menu>
          </template>
        </el-dropdown>
      </div>
    </div>

    <el-card shadow="never" class="filter-card">
      <div class="sink-filters">
        <el-input v-model="search" clearable placeholder="Search name / endpoint..." />
        <el-select v-model="typeFilter"><el-option label="All Types" value="All"/><el-option label="Kafka" value="kafka"/><el-option label="PostgreSQL" value="db"/><el-option label="File" value="file"/></el-select>
        <el-select v-model="stateFilter"><el-option label="All States" value="All"/><el-option label="Healthy" value="healthy"/><el-option label="Warning" value="warning"/><el-option label="Failed" value="failed"/><el-option label="Disabled" value="disabled"/><el-option label="Unknown" value="unknown"/></el-select>
      </div>
    </el-card>

    <el-card shadow="never">
      <el-table :data="rows" row-key="name">
        <el-table-column label="Sink" min-width="160"><template #default="{row}"><el-link :underline="false" @click="openSink(row)"><b>{{ row.name }}</b></el-link></template></el-table-column>
        <el-table-column label="Type" width="120"><template #default="{row}">{{ row.type==='db'?'PostgreSQL':row.type.toUpperCase() }}</template></el-table-column>
        <el-table-column label="Endpoint" min-width="260" show-overflow-tooltip><template #default="{row}">{{ endpointSummary(row) }}</template></el-table-column>
        <el-table-column label="State" width="120"><template #default="{row}"><el-tag :type="stateType(row.enabled?row.runtime_state:'disabled')" size="small">{{ stateLabel(row) }}</el-tag></template></el-table-column>
        <el-table-column label="Tasks" width="90" align="right"><template #default="{row}">{{ taskRefs(row.name).length }}</template></el-table-column>
        <el-table-column label="Queue" width="90" align="right"><template #default="{row}">{{ row.queue_depth }}</template></el-table-column>
        <el-table-column label="Enabled" width="105"><template #default="{row}"><el-switch v-model="row.enabled" @change="toggleEnabled(row, !!$event)"/></template></el-table-column>
        <el-table-column label="Last Test" min-width="160"><template #default="{row}">{{ row.last_test_at || 'Never' }}</template></el-table-column>
      </el-table>
    </el-card>

    <el-drawer v-model="drawerOpen" :title="creating ? 'Add Sink' : selected?.name || 'Sink'" direction="rtl" :size="drawerSize" append-to-body destroy-on-close>
      <el-tabs v-model="drawerTab">
        <el-tab-pane v-if="!creating" label="Overview" name="Overview">
          <template v-if="selected">
                        <el-descriptions :column="isMobile?1:2" border>
              <el-descriptions-item label="Name">{{ selected.name }}</el-descriptions-item>
              <el-descriptions-item label="Type">{{ selected.type==='db'?'PostgreSQL':selected.type.toUpperCase() }}</el-descriptions-item>
              <el-descriptions-item label="Enabled">{{ selected.enabled?'Yes':'No' }}</el-descriptions-item>
              <el-descriptions-item label="Runtime"><el-tag :type="stateType(selected.enabled?selected.runtime_state:'disabled')" size="small">{{ stateLabel(selected) }}</el-tag></el-descriptions-item>
              <el-descriptions-item label="Endpoint" :span="2">{{ endpointSummary(selected) }}</el-descriptions-item>
              <el-descriptions-item label="Referenced Tasks">{{ taskRefs(selected.name).length }}</el-descriptions-item>
              <el-descriptions-item label="Queue Depth">{{ selected.queue_depth }}</el-descriptions-item>
              <el-descriptions-item label="Writes">{{ selected.writes_total }}</el-descriptions-item>
              <el-descriptions-item label="Failures">{{ selected.failures_total }}</el-descriptions-item>
              <el-descriptions-item label="Dropped Points">{{ selected.dropped_points }}</el-descriptions-item>
              <el-descriptions-item label="Last Write">{{ selected.last_write_at || 'Never' }}</el-descriptions-item>
            </el-descriptions>
            <el-alert v-if="selected.error" type="error" :closable="false" :title="selected.error" style="margin-top:16px"/>
            <div class="danger-row"><el-button type="danger" plain @click="deleteSink(selected)">Delete Sink</el-button></div>
          </template>
        </el-tab-pane>

        <el-tab-pane :label="creating?'Config':'Config'" name="Config">
                    <template>
            <el-form label-position="top">
              <div class="sink-form-grid">
                <el-form-item label="Name"><el-input v-model="draft.name" :disabled="!creating"/></el-form-item>
                <el-form-item label="Type"><el-select v-model="draft.type" style="width:100%" :disabled="!creating" @change="onTypeChange"><el-option label="Kafka" value="kafka"/><el-option label="PostgreSQL" value="db"/><el-option label="File" value="file"/></el-select></el-form-item>
                <el-form-item label="Enabled"><el-switch v-model="draft.enabled"/></el-form-item>
              </div>
              <el-divider content-position="left">{{ draft.type==='db'?'PostgreSQL':draft.type.toUpperCase() }} Parameters</el-divider>
              <div v-if="draft.type==='kafka'" class="sink-form-grid">
                <el-form-item label="Bootstrap Servers"><el-input v-model="draft.bootstrap_servers" placeholder="localhost:9092"/></el-form-item>
                <el-form-item label="Topic"><el-input v-model="draft.topic"/></el-form-item>
                <el-form-item label="Key Field"><el-select v-model="draft.key_field" clearable style="width:100%"><el-option label="device_id" value="device_id"/><el-option label="point_id" value="point_id"/><el-option label="source" value="source"/></el-select></el-form-item>
                <el-form-item label="Compression"><el-select v-model="draft.compression_type" clearable style="width:100%"><el-option label="gzip" value="gzip"/><el-option label="snappy" value="snappy"/><el-option label="lz4" value="lz4"/><el-option label="zstd" value="zstd"/></el-select></el-form-item>
                <el-form-item label="Acks"><el-select v-model="draft.acks" style="width:100%"><el-option label="all" value="all"/><el-option label="1" value="1"/><el-option label="0" value="0"/></el-select></el-form-item>
                <el-form-item label="Retries"><el-input-number v-model="draft.retries" :min="1" style="width:100%"/></el-form-item>
                <el-form-item label="Batch Size (bytes)"><el-input-number v-model="draft.kafka_batch_size" :min="1" style="width:100%"/></el-form-item>
                <el-form-item label="Linger (ms)"><el-input-number v-model="draft.linger_ms" :min="0" style="width:100%"/></el-form-item>
              </div>
              <div v-else-if="draft.type==='db'" class="sink-form-grid">
                <el-form-item label="DSN"><el-input v-model="draft.dsn" type="password" show-password placeholder="postgresql://user:pass@host/db"/></el-form-item>
                <el-form-item label="Table"><el-input v-model="draft.table"/></el-form-item>
                <el-form-item label="Batch Size"><el-input-number v-model="draft.db_batch_size" :min="1" style="width:100%"/></el-form-item>
                <el-form-item label="Create Table"><el-switch v-model="draft.create_table"/></el-form-item>
                <el-form-item label="Pool Min Size"><el-input-number v-model="draft.pool_min_size" :min="1" style="width:100%"/></el-form-item>
                <el-form-item label="Pool Max Size"><el-input-number v-model="draft.pool_max_size" :min="1" style="width:100%"/></el-form-item>
              </div>
              <div v-else class="sink-form-grid">
                <el-form-item label="Path"><el-input v-model="draft.path" placeholder="/var/tmp/wind-hub/archive.jsonl"/></el-form-item>
                <el-form-item label="Format"><el-select v-model="draft.format" style="width:100%"><el-option label="JSONL" value="jsonl"/><el-option label="CSV" value="csv"/></el-select></el-form-item>
                <el-form-item label="Max Size (MB)"><el-input-number v-model="draft.max_size_mb" :min="0" style="width:100%"/></el-form-item>
                <el-form-item label="Max Age (h)"><el-input-number v-model="draft.max_age_hours" :min="0" style="width:100%"/></el-form-item>
                <el-form-item label="Compress"><el-switch v-model="draft.compress"/></el-form-item>
                <el-form-item label="Compression Level"><el-input-number v-model="draft.compress_level" :min="1" :max="9" :disabled="!draft.compress" style="width:100%"/></el-form-item>
                <el-form-item label="Buffer Size"><el-input-number v-model="draft.buffer_size" :min="1" style="width:100%"/></el-form-item>
                <el-form-item label="Flush Interval (s)"><el-input-number v-model="draft.flush_interval" :min="0" :step="0.1" style="width:100%"/></el-form-item>
                <el-form-item v-if="draft.format==='csv'" label="Write Header"><el-switch v-model="draft.write_header"/></el-form-item>
              </div>
            </el-form>
            <div class="editor-actions"><el-button @click="cancelEdit">{{ creating?'Cancel':'Reset' }}</el-button><el-button type="primary" @click="saveSink">{{ creating?'Create':'Save' }}</el-button></div>
          </template>

        </el-tab-pane>

        <el-tab-pane v-if="!creating" label="Test" name="Test">
          <template v-if="selected">
            <el-alert type="info" :closable="false" title="Connection Test only opens/checks the destination. Write Test sends one synthetic PointValue to the configured destination."/>
            <el-card shadow="never" class="test-card">
              <div class="test-buttons">
                <el-button type="primary" :loading="testLoading && testStage==='connection'" :disabled="testLoading" @click="runConnectionTest(selected)">Connection Test</el-button>
                <el-button :loading="testLoading && testStage==='write'" :disabled="testLoading" @click="runWriteTest(selected)">Write Test</el-button>
              </div>
              <el-descriptions :column="1" border style="margin-top:16px">
                <el-descriptions-item label="Sample device_id">sink-test-device</el-descriptions-item>
                <el-descriptions-item label="Sample point_id">sink_test</el-descriptions-item>
                <el-descriptions-item label="Sample value">1.0</el-descriptions-item>
                <el-descriptions-item label="Quality">GOOD</el-descriptions-item>
              </el-descriptions>
              <el-result v-if="testResult" :icon="testResult.ok?'success':'error'" :title="testResult.title" :sub-title="testResult.detail">
                <template #extra><span>{{ testResult.latency }} ms</span></template>
              </el-result>
            </el-card>
          </template>
        </el-tab-pane>
      </el-tabs>
    </el-drawer>
  </div>
</template>

<style scoped>
.sink-filters{display:grid;grid-template-columns:minmax(260px,1fr) 180px 180px;gap:var(--app-space-3)}
.drawer-actions{display:flex;justify-content:flex-end;margin-bottom:var(--app-space-3)}
.sink-form-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:0 var(--app-space-4)}
.editor-actions{display:flex;justify-content:flex-end;gap:var(--app-space-2);margin-top:var(--app-space-4)}
.test-card{margin-top:var(--app-space-4)}
.test-buttons{display:flex;gap:var(--app-space-2);flex-wrap:wrap}
.danger-row{display:flex;justify-content:flex-end;margin-top:var(--app-space-4);padding-top:var(--app-space-4);border-top:1px solid var(--app-border-soft)}
@media(max-width:767px){.sink-filters,.sink-form-grid{grid-template-columns:1fr}}
</style>
