<script setup lang="ts">
import { computed, reactive, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  devicesForTask,
  effectiveConnection,
  isDefaultPointTable,
  modelOf,
  pointsOfTable,
  store,
  tableOfDevice,
  unitSymbol,
} from '../mock/data'
import { DATA_TYPES, MODBUS_REGISTER_TYPES } from '../mock/types'
import type { PointDef } from '../mock/types'

type StageState = 'idle' | 'running' | 'passed' | 'failed' | 'warning'
type DiagnosisStage = {
  key: string
  title: string
  state: StageState
  detail: string
  duration_ms: number
}
type PointReadResult = {
  point_id: string
  variable_name: string
  address: string
  data_type: string
  state: 'passed' | 'failed'
  latency_ms: number
  value: string
  error_category: string
  error: string
}

const selectedDeviceId = ref(store.devices[0]?.device_id || '')
const selectedTaskId = ref('')
const selectedPointIds = ref<string[]>([])
const diagnosisRunning = ref(false)
const stages = ref<DiagnosisStage[]>([])
const pointResults = ref<PointReadResult[]>([])
const advancedOpen = ref<string[]>([])

const selectedDevice = computed(() => store.devices.find(d => d.device_id === selectedDeviceId.value))
const protocol = computed(() => selectedDevice.value ? modelOf(selectedDevice.value)?.protocol || '' : '')
const relatedTasks = computed(() => store.tasks.filter(t =>
  selectedDevice.value && devicesForTask(t).some(d => d.device_id === selectedDevice.value!.device_id),
))
const selectedTask = computed(() => relatedTasks.value.find(t => t.task_id === selectedTaskId.value))
const resolvedPoints = computed(() => selectedDevice.value ? pointsOfTable(tableOfDevice(selectedDevice.value)) : [])
const taskPoints = computed(() => {
  if (!selectedTask.value) return resolvedPoints.value
  return resolvedPoints.value.filter(p => p.point_groups.includes(selectedTask.value!.point_group))
})
const selectedPoints = computed(() => taskPoints.value.filter(p => selectedPointIds.value.includes(p.point_id)))

function pointAddress(p: PointDef) {
  if (p.address.symbol) return p.address.symbol
  if (p.address.index_group || p.address.index_offset) return `${p.address.index_group || '-'} / ${p.address.index_offset || '-'}`
  if (p.address.type || p.address.address !== undefined) return `${p.address.type || '-'} ${p.address.address ?? '-'}`
  if (p.address.ioa !== undefined) return `IOA ${p.address.ioa}`
  return '—'
}

function resetTarget() {
  selectedTaskId.value = relatedTasks.value[0]?.task_id || ''
  selectedPointIds.value = taskPoints.value.slice(0, 3).map(p => p.point_id)
  stages.value = []
  pointResults.value = []
}
watch(selectedDeviceId, resetTarget)
watch(selectedTaskId, () => {
  selectedPointIds.value = taskPoints.value.slice(0, 3).map(p => p.point_id)
  stages.value = []
  pointResults.value = []
})
resetTarget()

function sleep(ms: number) { return new Promise(resolve => setTimeout(resolve, ms)) }

function initialStages(): DiagnosisStage[] {
  return [
    { key:'preflight', title:'Task / Config', state:'idle', detail:'Validate task, point table and sink references', duration_ms:0 },
    { key:'network', title:'Network', state:'idle', detail:'Reachability from Wind Hub runtime host', duration_ms:0 },
    { key:'transport', title:'TCP / Transport', state:'idle', detail:'Open configured transport endpoint', duration_ms:0 },
    { key:'protocol', title:'Protocol Session', state:'idle', detail:'Establish protocol-level session', duration_ms:0 },
    { key:'mapping', title:'Point Mapping', state:'idle', detail:'Resolve selected points to protocol addresses', duration_ms:0 },
    { key:'read', title:'Selected Point Read', state:'idle', detail:'Read the explicitly selected points', duration_ms:0 },
  ]
}

async function runStage(index:number, result:()=>{state:StageState;detail:string;duration:number}) {
  stages.value[index].state='running'
  await sleep(260)
  const r=result()
  Object.assign(stages.value[index], {state:r.state,detail:r.detail,duration_ms:r.duration})
  return r.state !== 'failed'
}

function taskPreflight() {
  const d=selectedDevice.value
  if(!d) return {state:'failed' as StageState,detail:'Device does not exist',duration:1}
  const model=modelOf(d)
  if(!model) return {state:'failed' as StageState,detail:'Device Model reference is missing',duration:1}
  if(isDefaultPointTable(model.point_table)) return {state:'failed' as StageState,detail:'Device still uses a default placeholder Point Table',duration:2}
  if(selectedTask.value?.valid===false) return {state:'failed' as StageState,detail:selectedTask.value.invalid_reason || 'Task definition is invalid',duration:2}
  if(selectedTask.value && !selectedTask.value.enabled) return {state:'warning' as StageState,detail:'Task is disabled; configuration is valid but acquisition will not run',duration:2}
  return {state:'passed' as StageState,detail:'Task/config references are valid',duration:2}
}

function networkCheck() {
  const d=selectedDevice.value!
  if(!d.enabled) return {state:'failed' as StageState,detail:'Device is disabled',duration:1}
  if(!d.online || d.device_id.endsWith('041')) return {state:'failed' as StageState,detail:`Timeout: no response from ${d.host}`,duration:1000}
  return {state:'passed' as StageState,detail:`Reachable: ${d.host} · RTT 8 ms`,duration:8}
}

function transportCheck() {
  const d=selectedDevice.value!
  const conn=effectiveConnection(d)
  const port=Number(conn.port || (protocol.value==='modbus'?502:protocol.value==='iec104'?2404:48898))
  if(d.device_id.endsWith('043')) return {state:'failed' as StageState,detail:`Connection refused: ${d.host}:${port}`,duration:34}
  return {state:'passed' as StageState,detail:`Transport connected: ${d.host}:${port}`,duration:18}
}

function protocolCheck() {
  const d=selectedDevice.value!
  if(d.device_id.endsWith('042')) return {state:'failed' as StageState,detail:`${protocol.value.toUpperCase()} session timeout / handshake not completed`,duration:3000}
  return {state:'passed' as StageState,detail:`${protocol.value.toUpperCase()} session established`,duration:24}
}

function mappingCheck() {
  if(!selectedPointIds.value.length) return {state:'failed' as StageState,detail:'No points selected for read verification',duration:1}
  const missing=selectedPointIds.value.filter(id=>!taskPoints.value.some(p=>p.point_id===id))
  if(missing.length) return {state:'failed' as StageState,detail:`Point mapping missing: ${missing.join(', ')}`,duration:2}
  return {state:'passed' as StageState,detail:`${selectedPointIds.value.length} selected point(s) resolved from ${tableOfDevice(selectedDevice.value!)}`,duration:3}
}

async function runPointRead() {
  pointResults.value=[]
  for(let i=0;i<selectedPoints.value.length;i++){
    const p=selectedPoints.value[i]
    await sleep(80)
    let failed=false
    let category=''
    let error=''
    if(selectedDevice.value?.device_id.endsWith('044') && i===0){
      failed=true; category='timeout'; error='Read request timed out after 3000 ms'
    } else if(protocol.value==='ads' && (p.variable_name.includes('missing') || p.point_id.includes('missing'))){
      failed=true; category='not_found'; error='ADS symbol not found'
    } else if(i===2 && selectedDevice.value?.device_id.endsWith('045')){
      failed=true; category='decode'; error='Raw payload length does not match configured data_type'
    }
    pointResults.value.push({
      point_id:p.point_id,
      variable_name:p.variable_name,
      address:pointAddress(p),
      data_type:p.data_type,
      state:failed?'failed':'passed',
      latency_ms:failed?3000:12+i*5,
      value:failed?'—':String(Number((24.5+i*13.27).toFixed(3))),
      error_category:category,
      error,
    })
  }
  const failed=pointResults.value.filter(r=>r.state==='failed')
  return failed.length
    ? {state:'failed' as StageState,detail:`${failed.length}/${pointResults.value.length} selected point(s) failed`,duration:Math.max(...pointResults.value.map(r=>r.latency_ms))}
    : {state:'passed' as StageState,detail:`${pointResults.value.length} selected point(s) read successfully`,duration:Math.max(0,...pointResults.value.map(r=>r.latency_ms))}
}

async function runDiagnosis(){
  if(!selectedDevice.value){ElMessage.warning('Select a Device first');return}
  if(!selectedPointIds.value.length){ElMessage.warning('Select at least one point for Read Test');return}
  diagnosisRunning.value=true
  stages.value=initialStages()
  pointResults.value=[]
  try{
    const checks=[taskPreflight,networkCheck,transportCheck,protocolCheck,mappingCheck]
    for(let i=0;i<checks.length;i++){
      const ok=await runStage(i,checks[i])
      if(!ok){
        for(let j=i+1;j<stages.value.length;j++) stages.value[j].detail='Skipped because an upstream dependency failed'
        return
      }
    }
    stages.value[5].state='running'
    const result=await runPointRead()
    Object.assign(stages.value[5],{state:result.state,detail:result.detail,duration_ms:result.duration})
  } finally { diagnosisRunning.value=false }
}

const rootCause = computed<{ type: 'success' | 'error'; title: string; text: string } | null>(() => {
  const failed=stages.value.find(s=>s.state==='failed')
  if(!failed){
    if(stages.value.length && stages.value.every(s=>s.state==='passed'||s.state==='warning'))
      return {type:'success',title:'No blocking acquisition fault found',text:'Network, transport, protocol, mapping and selected reads all passed. Continue with Quality/Logs if the problem is intermittent.'}
    return null
  }
  const map:Record<string,string>={
    preflight:'Fix Task/Point Table/Sink references before communication testing.',
    network:'Check route, interface/source IP, VLAN, device power and network reachability.',
    transport:'Check remote port, firewall, service state and protocol endpoint configuration.',
    protocol:'Transport is reachable but the protocol session failed. Check AMS route/Net ID, Modbus unit/mode, IEC104 common address, credentials or runtime state.',
    mapping:'Communication is available but selected points cannot be resolved. Check Point Table inheritance, group membership and protocol addresses.',
    read:'Session and mapping are valid, but one or more reads failed. Inspect timeout/not-found/decode errors per point.',
  }
  return {type:'error',title:`Likely failure layer: ${failed.title}`,text:map[failed.key]||failed.detail}
})

const manual = reactive({symbol:'',index_group:'',index_offset:'',register_type:'holding',address:0,ioa:1,data_type:'float32'})
const manualResult=ref('')
function manualRead(){
  if(!selectedDevice.value) return
  const addr=protocol.value==='ads'?(manual.symbol||`${manual.index_group}/${manual.index_offset}`):protocol.value==='modbus'?`${manual.register_type} ${manual.address}`:`IOA ${manual.ioa}`
  manualResult.value=`${selectedDevice.value.device_id} · ${addr} · ${manual.data_type} = 42.125 (mock)`
}

const raw=reactive({bytes:'42 C8 00 00',scale:1,offset:0,unit:'kilowatt'})
const rawRows=computed(()=>[
  {type:'int16',value:17096},{type:'uint16',value:17096},{type:'int32',value:1120403456},{type:'uint32',value:1120403456},{type:'float32',value:100},{type:'bool',value:true},
].map(x=>({...x,engineering:typeof x.value==='number'?x.value*raw.scale+raw.offset:x.value})))

const writePoint=ref('')
const writeValue=ref('')
const writablePoints=computed(()=>resolvedPoints.value.filter(p=>p.point_groups.includes('control')))
async function writeTest(){
  if(!writePoint.value||!writeValue.value.trim()){ElMessage.warning('Select a point and enter a value');return}
  await ElMessageBox.confirm('Send one diagnostic write to the selected device? This has a real side effect in the backend implementation.','Diagnostic Write',{type:'warning',confirmButtonText:'Send'})
  ElMessage.success('Diagnostic write completed (mock)')
}
</script>

<template>
  <div class="diagnostics-page">
    <div class="head"><div><h1>Diagnostics</h1><p>从采集失败现象出发，逐层定位设备、协议、点表与读数问题</p></div></div>

    <el-card shadow="never" class="target-card">
      <div class="target-grid">
        <el-form-item label="Device">
          <el-select v-model="selectedDeviceId" filterable style="width:100%">
            <el-option v-for="d in store.devices" :key="d.device_id" :label="`${d.device_id} · ${modelOf(d)?.protocol?.toUpperCase()} · ${d.host}`" :value="d.device_id"/>
          </el-select>
        </el-form-item>
        <el-form-item label="Related Task">
          <el-select v-model="selectedTaskId" clearable style="width:100%">
            <el-option v-for="t in relatedTasks" :key="t.task_id" :label="`${t.task_id} · ${t.runtime}`" :value="t.task_id"/>
          </el-select>
        </el-form-item>
        <div class="target-summary" v-if="selectedDevice">
          <span>{{ protocol.toUpperCase() }}</span><b>{{ selectedDevice.host }}</b><span>{{ selectedTask ? selectedTask.point_group : 'All resolved points' }}</span>
        </div>
      </div>
    </el-card>

    <div class="diagnostic-layout">
      <el-card shadow="never" class="diagnostic-main">
        <div class="section-head"><div><h3>Diagnostic Path</h3><p>上游失败时自动停止后续测试，避免把连通问题误判成点表问题。</p></div><el-button type="primary" :loading="diagnosisRunning" @click="runDiagnosis">Run Full Diagnosis</el-button></div>

        <el-steps direction="vertical" :active="stages.filter(s=>s.state==='passed'||s.state==='warning').length" finish-status="success" process-status="process">
          <el-step v-for="s in (stages.length?stages:initialStages())" :key="s.key">
            <template #title><div class="stage-title"><b>{{s.title}}</b><el-tag v-if="s.state!=='idle'" size="small" :type="s.state==='passed'?'success':s.state==='warning'?'warning':s.state==='failed'?'danger':'info'">{{s.state}}</el-tag></div></template>
            <template #description><div class="stage-detail">{{s.detail}}<span v-if="s.duration_ms"> · {{s.duration_ms}} ms</span></div></template>
          </el-step>
        </el-steps>
      </el-card>

      <el-card shadow="never" class="finding-card">
        <h3>Finding</h3>
        <el-empty v-if="!rootCause" description="Run diagnostics to identify the first failing layer"/>
        <el-result v-else :icon="rootCause.type" :title="rootCause.title" :sub-title="rootCause.text"/>
      </el-card>
    </div>

    <el-card shadow="never" class="point-test-card">
      <div class="section-head">
        <div><h3>Selected Point Read</h3><p>Read Test 只读取你明确选择的点，不再隐式测试未知的“3 个点”。</p></div>
      </div>
      <el-form label-position="top">
        <el-form-item label="Points to verify">
          <el-select v-model="selectedPointIds" multiple filterable collapse-tags collapse-tags-tooltip style="width:100%">
            <el-option v-for="p in taskPoints" :key="p.point_id" :value="p.point_id" :label="`${p.point_id} · ${p.variable_name || pointAddress(p)}`"/>
          </el-select>
        </el-form-item>
      </el-form>
      <el-table :data="pointResults.length?pointResults:selectedPoints.map(p=>({point_id:p.point_id,variable_name:p.variable_name,address:pointAddress(p),data_type:p.data_type,state:'',latency_ms:'',value:'',error_category:'',error:''}))" size="small">
        <el-table-column prop="point_id" label="Point" min-width="150"/>
        <el-table-column prop="address" label="Resolved Address" min-width="190"/>
        <el-table-column prop="data_type" label="Type" width="100"/>
        <el-table-column prop="value" label="Value" width="110"/>
        <el-table-column prop="latency_ms" label="Latency" width="100"><template #default="{row}">{{row.latency_ms?row.latency_ms+' ms':'—'}}</template></el-table-column>
        <el-table-column label="Result" min-width="210"><template #default="{row}"><template v-if="row.state"><el-tag :type="row.state==='passed'?'success':'danger'" size="small">{{row.state}}</el-tag><span v-if="row.error" class="read-error">{{row.error_category}} · {{row.error}}</span></template><span v-else>Pending</span></template></el-table-column>
      </el-table>
    </el-card>

    <el-card shadow="never" class="advanced-card">
      <el-collapse v-model="advancedOpen">
        <el-collapse-item title="Advanced Tools · Manual Read / Raw Decoder / Write Test" name="tools">
          <el-tabs>
            <el-tab-pane label="Manual Read">
              <el-form label-position="top">
                <div class="tool-grid">
                  <template v-if="protocol==='ads'"><el-form-item label="Symbol"><el-input v-model="manual.symbol"/></el-form-item><el-form-item label="Index Group"><el-input v-model="manual.index_group"/></el-form-item><el-form-item label="Index Offset"><el-input v-model="manual.index_offset"/></el-form-item></template>
                  <template v-else-if="protocol==='modbus'"><el-form-item label="Register Type"><el-select v-model="manual.register_type"><el-option v-for="r in MODBUS_REGISTER_TYPES" :key="r" :label="r" :value="r"/></el-select></el-form-item><el-form-item label="Address"><el-input-number v-model="manual.address" :min="0" style="width:100%"/></el-form-item></template>
                  <el-form-item v-else label="IOA"><el-input-number v-model="manual.ioa" :min="0" style="width:100%"/></el-form-item>
                  <el-form-item label="Data Type"><el-select v-model="manual.data_type"><el-option v-for="t in DATA_TYPES" :key="t" :label="t" :value="t"/></el-select></el-form-item>
                </div>
                <el-button type="primary" @click="manualRead">Read Once</el-button>
                <pre v-if="manualResult">{{manualResult}}</pre>
              </el-form>
            </el-tab-pane>

            <el-tab-pane label="Raw Decoder">
              <div class="tool-grid"><el-form-item label="Raw Bytes"><el-input v-model="raw.bytes"/></el-form-item><el-form-item label="Scale"><el-input-number v-model="raw.scale"/></el-form-item><el-form-item label="Offset"><el-input-number v-model="raw.offset"/></el-form-item><el-form-item label="Unit"><el-select v-model="raw.unit"><el-option v-for="(u,id) in store.units" :key="id" :label="`${id}${u.symbol?' ('+u.symbol+')':''}`" :value="id"/></el-select></el-form-item></div>
              <el-table :data="rawRows" size="small"><el-table-column prop="type" label="Interpretation"/><el-table-column prop="value" label="Parsed"/><el-table-column label="Engineering"><template #default="{row}">{{row.engineering}} {{unitSymbol(raw.unit)}}</template></el-table-column></el-table>
            </el-tab-pane>

            <el-tab-pane label="Write Test">
              <div class="tool-grid"><el-form-item label="Point"><el-select v-model="writePoint" filterable><el-option v-for="p in writablePoints" :key="p.point_id" :label="p.point_id" :value="p.point_id"/></el-select></el-form-item><el-form-item label="Value"><el-input v-model="writeValue"/></el-form-item></div>
              <el-button type="primary" @click="writeTest">Send Diagnostic Write</el-button>
            </el-tab-pane>
          </el-tabs>
        </el-collapse-item>
      </el-collapse>
    </el-card>
  </div>
</template>

<style scoped>
.target-card,.point-test-card,.advanced-card{margin-bottom:var(--app-space-4)}.target-grid{display:grid;grid-template-columns:minmax(220px,1fr) minmax(220px,1fr) minmax(260px,.9fr);gap:var(--app-space-4);align-items:end}.target-grid :deep(.el-form-item){margin-bottom:0}.target-summary{display:flex;gap:var(--app-space-2);align-items:center;justify-content:flex-end;padding-bottom:9px;color:var(--app-text-muted);font-size:var(--app-font-caption)}.target-summary b{color:var(--app-text-primary)}.diagnostic-layout{display:grid;grid-template-columns:minmax(0,1.4fr) minmax(300px,.6fr);gap:var(--app-space-4);margin-bottom:var(--app-space-4)}.section-head{display:flex;align-items:flex-start;justify-content:space-between;gap:var(--app-space-3);margin-bottom:var(--app-space-4)}.section-head h3,.finding-card h3{margin:0}.section-head p{margin:4px 0 0;color:var(--app-text-muted);font-size:var(--app-font-caption)}.stage-title{display:flex;align-items:center;gap:var(--app-space-2)}.stage-detail{color:var(--app-text-muted);font-size:var(--app-font-caption)}.read-error{margin-left:8px;color:var(--el-color-danger);font-size:var(--app-font-caption)}.tool-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:0 var(--app-space-4)}pre{padding:var(--app-space-3);background:var(--el-fill-color-light);border-radius:var(--app-radius-control);overflow:auto}
@media(max-width:1000px){.target-grid,.diagnostic-layout{grid-template-columns:1fr}.target-summary{justify-content:flex-start;padding-bottom:0}}
@media(max-width:767px){.tool-grid{grid-template-columns:1fr}.section-head{flex-direction:column}}
</style>
