<script setup lang="ts">
import { computed, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { useViewport } from '../composables/useViewport'
import { refreshTaskValidity, store } from '../mock/data'
import type { SinkDef, SinkRuntimeState, SinkType, SinkVerificationCheck } from '../mock/types'

const { isMobile }=useViewport()
const drawerSize=computed(()=>isMobile.value?'100%':'min(920px, 84vw)')
const search=ref('')
const typeFilter=ref<'All'|SinkType>('All')
const stateFilter=ref<'All'|SinkRuntimeState>('All')
const drawerOpen=ref(false)
const drawerTab=ref('Summary')
const creating=ref(false)
const selectedName=ref('')
const sinkSnapshot=ref('')
const testLoading=ref(false)
const verifyAllRunning=ref(false)
const batchVerification=ref<{checked_at:string;checked:number;passed:number;failed:number;warning:number}|null>(null)
const testResult=ref<{ok:boolean;title:string;detail:string;latency:number}|null>(null)


const selected=computed(()=>store.sinks.find(s=>s.name===selectedName.value))
const rows=computed(()=>store.sinks.filter(s=>{
  const q=search.value.trim().toLowerCase()
  return (!q||[s.name,s.type,endpointSummary(s)].some(x=>x.toLowerCase().includes(q))) &&
    (typeFilter.value==='All'||s.type===typeFilter.value) &&
    (stateFilter.value==='All'||s.runtime_state===stateFilter.value)
}))

const draft=reactive({
  name:'',type:'file' as SinkType,enabled:true,
  bootstrap_servers:'',topic:'',key_field:'',compression_type:'',acks:'all',retries:3,kafka_batch_size:16384,linger_ms:0,
  dsn:'',table:'points',db_batch_size:1000,create_table:false,pool_min_size:1,pool_max_size:10,
  path:'',format:'jsonl',max_size_mb:100,max_age_hours:24,compress:false,compress_level:6,buffer_size:100,flush_interval:1,write_header:true,
})
const sinkDraftState=computed(()=>JSON.stringify(draft))
const sinkDirty=computed(()=>creating.value ? sinkDraftState.value!==sinkSnapshot.value : (!!selected.value && sinkDraftState.value!==sinkSnapshot.value))

function nowText(){
  const d=new Date(); const p=(n:number)=>String(n).padStart(2,'0')
  return `${d.getFullYear()}-${p(d.getMonth()+1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`
}
function sleep(ms:number){return new Promise(resolve=>setTimeout(resolve,ms))}
function stateType(state:SinkRuntimeState):''|'success'|'warning'|'danger'|'info'{
  if(state==='healthy')return'success'; if(state==='warning')return'warning'; if(state==='failed')return'danger'; if(state==='testing')return''; return'info'
}
function stateLabel(s:SinkDef){return !s.enabled?'Disabled':s.runtime_state.charAt(0).toUpperCase()+s.runtime_state.slice(1)}
function endpointSummary(s:SinkDef){
  if(s.type==='kafka')return `${s.params.bootstrap_servers||'—'} · ${s.params.topic||'—'}`
  if(s.type==='db'){const dsn=String(s.params.dsn||'');return `${dsn.replace(/:\/\/([^:]+):[^@]+@/,'://$1:***@')||'—'} · ${s.params.table||'—'}`}
  return String(s.params.path||'—')
}
function taskRefs(name:string){return store.tasks.filter(t=>t.sinks.includes(name))}
function verifyLabel(s:SinkDef){
  if(s.verification.state==='never')return'Never'
  return `${s.verification.state==='passed'?'PASS':'FAIL'} · ${s.verification.passed}/${s.verification.total}`
}
function verifyType(s:SinkDef):''|'success'|'danger'|'info'{return s.verification.state==='passed'?'success':s.verification.state==='failed'?'danger':'info'}

function resetDraft(type:SinkType='file'){
  Object.assign(draft,{name:'',type,enabled:true,bootstrap_servers:'',topic:'',key_field:'',compression_type:'',acks:'all',retries:3,kafka_batch_size:16384,linger_ms:0,dsn:'',table:'points',db_batch_size:1000,create_table:false,pool_min_size:1,pool_max_size:10,path:'',format:'jsonl',max_size_mb:100,max_age_hours:24,compress:false,compress_level:6,buffer_size:100,flush_interval:1,write_header:true})
}
function loadDraft(s:SinkDef){
  resetDraft(s.type); draft.name=s.name;draft.enabled=s.enabled;const p=s.params
  if(s.type==='kafka'){draft.bootstrap_servers=String(p.bootstrap_servers||'');draft.topic=String(p.topic||'');draft.key_field=String(p.key_field||'');draft.compression_type=String(p.compression_type||'');draft.acks=String(p.acks??'all');draft.retries=Number(p.retries??3);draft.kafka_batch_size=Number(p.batch_size??16384);draft.linger_ms=Number(p.linger_ms??0)}
  else if(s.type==='db'){draft.dsn=String(p.dsn||'');draft.table=String(p.table||'points');draft.db_batch_size=Number(p.batch_size??1000);draft.create_table=Boolean(p.create_table);draft.pool_min_size=Number(p.pool_min_size??1);draft.pool_max_size=Number(p.pool_max_size??10)}
  else{draft.path=String(p.path||'');draft.format=String(p.format||'jsonl');draft.max_size_mb=Number(p.max_size_mb??100);draft.max_age_hours=Number(p.max_age_hours??24);draft.compress=Boolean(p.compress);draft.compress_level=Number(p.compress_level??6);draft.buffer_size=Number(p.buffer_size??100);draft.flush_interval=Number(p.flush_interval??1);draft.write_header=Boolean(p.write_header??true)}
  sinkSnapshot.value=JSON.stringify(draft)
}
function openSink(s:SinkDef){selectedName.value=s.name;creating.value=false;drawerTab.value='Summary';testResult.value=null;loadDraft(s);drawerOpen.value=true}
function openAdd(){selectedName.value='';creating.value=true;drawerTab.value='Summary';testResult.value=null;resetDraft('file');sinkSnapshot.value=JSON.stringify(draft);drawerOpen.value=true}
function onTypeChange(){const name=draft.name,enabled=draft.enabled,type=draft.type;resetDraft(type);draft.name=name;draft.enabled=enabled}
async function beforeSinkClose(done:()=>void){
  if(!sinkDirty.value){done();return}
  try{await ElMessageBox.confirm('Discard unsaved Sink changes?','Unsaved Changes',{type:'warning',confirmButtonText:'Discard'});done()}catch{}
}
function paramsFromDraft():Record<string,unknown>{
  if(draft.type==='kafka')return{bootstrap_servers:draft.bootstrap_servers.trim(),topic:draft.topic.trim(),key_field:draft.key_field||undefined,compression_type:draft.compression_type||undefined,acks:draft.acks,retries:draft.retries,batch_size:draft.kafka_batch_size,linger_ms:draft.linger_ms}
  if(draft.type==='db')return{dsn:draft.dsn.trim(),table:draft.table.trim(),batch_size:draft.db_batch_size,create_table:draft.create_table,pool_min_size:draft.pool_min_size,pool_max_size:draft.pool_max_size}
  return{path:draft.path.trim(),format:draft.format,max_size_mb:draft.max_size_mb,max_age_hours:draft.max_age_hours,compress:draft.compress,compress_level:draft.compress_level,buffer_size:draft.buffer_size,flush_interval:draft.flush_interval,write_header:draft.write_header}
}
function validateDraft(){
  if(!draft.name.trim())return'Sink name is required'
  if(creating.value&&store.sinks.some(s=>s.name===draft.name.trim()))return'Sink name already exists'
  if(draft.type==='kafka'&&(!draft.bootstrap_servers.trim()||!draft.topic.trim()))return'Kafka bootstrap_servers and topic are required'
  if(draft.type==='db'&&(!draft.dsn.trim()||!draft.table.trim()))return'PostgreSQL DSN and table are required'
  if(draft.type==='db'&&draft.pool_min_size>draft.pool_max_size)return'Pool min size must be <= max size'
  if(draft.type==='file'&&!draft.path.trim())return'File path is required'
  return''
}
async function saveSink(){
  const error=validateDraft();if(error){ElMessage.error(error);return}
  const params=paramsFromDraft()
  const wasCreating=creating.value
  if(creating.value){
    const sink:SinkDef={name:draft.name.trim(),type:draft.type,enabled:draft.enabled,params,runtime_state:draft.enabled?'unknown':'disabled',last_test_at:'',last_write_at:'',latency_ms:0,error:'',queue_depth:0,writes_total:0,failures_total:0,dropped_points:0,verification:{state:'never',checked_at:'',passed:0,total:0,checks:[]}}
    store.sinks.push(sink);selectedName.value=sink.name;creating.value=false;loadDraft(sink)
  }else if(selected.value){
    const refs=taskRefs(selected.value.name),running=refs.filter(t=>t.runtime==='RUNNING')
    if(refs.length)await ElMessageBox.confirm('<b>Sink Change Impact</b><br><br>'+refs.length+' Task(s) reference this Sink; '+running.length+' currently running.<br>The Sink instance will be reopened.','Apply Sink Changes',{type:'warning',confirmButtonText:'Apply Changes',dangerouslyUseHTMLString:true})
    selected.value.params=params;selected.value.enabled=draft.enabled;selected.value.runtime_state=draft.enabled?'unknown':'disabled';selected.value.error='';loadDraft(selected.value)
  }
  refreshTaskValidity();ElMessage.success(wasCreating?'Sink created (mock)':'Sink configuration saved (mock)')
}
function networkEndpoint(s:SinkDef){
  if(s.type==='kafka'){
    const first=String(s.params.bootstrap_servers||'localhost:9092').split(',')[0].trim()
    const [host,portText]=first.split(':')
    return {host:host||'localhost',port:Number(portText||9092)}
  }
  if(s.type==='db'){
    const dsn=String(s.params.dsn||'')
    const match=dsn.match(/^[a-zA-Z0-9+.-]+:\/\/(?:[^@/]+@)?([^:/]+)(?::(\d+))?/)
    return {host:match?.[1]||'localhost',port:Number(match?.[2]||5432)}
  }
  return {host:'',port:0}
}
function resolvedHost(host:string){
  if(host==='localhost') return '127.0.0.1'
  if(/^\d+\.\d+\.\d+\.\d+$/.test(host)) return host
  return '192.168.10.25'
}
function checkPlan(s:SinkDef):Array<{layer:'network'|'protocol'|'target'|'filesystem';name:string;target:string;detail:string;blocks:boolean}>{
  if(s.type==='file'){
    const path=String(s.params.path||'')
    const parent=path.includes('/')?path.slice(0,path.lastIndexOf('/'))||'/':'.'
    return[
      {layer:'filesystem',name:'Parent Path',target:parent,detail:'Resolve parent directory',blocks:true},
      {layer:'filesystem',name:'Permission',target:parent,detail:'Check create/append permission',blocks:true},
      {layer:'filesystem',name:'Open / Append',target:path,detail:'Open target for append without business payload',blocks:true},
    ]
  }
  const ep=networkEndpoint(s)
  const ip=resolvedHost(ep.host)
  const common=[
    {layer:'network' as const,name:'DNS',target:ep.host,detail:`Resolve ${ep.host} → ${ip}`,blocks:true},
    {layer:'network' as const,name:'ICMP',target:ip,detail:'ICMP reachability check (advisory only)',blocks:false},
    {layer:'network' as const,name:'TCP Port',target:`${ip}:${ep.port}`,detail:'Open TCP connection to remote endpoint',blocks:true},
  ]
  if(s.type==='kafka') return[
    ...common,
    {layer:'protocol',name:'Broker Session',target:`${ep.host}:${ep.port}`,detail:'Establish Kafka producer / broker session',blocks:true},
    {layer:'target',name:'Metadata',target:String(s.params.topic||''),detail:'Request broker metadata',blocks:true},
    {layer:'target',name:'Topic',target:String(s.params.topic||''),detail:'Verify configured topic is addressable',blocks:true},
  ]
  return[
    ...common,
    {layer:'protocol',name:'PostgreSQL Session',target:`${ep.host}:${ep.port}`,detail:'Establish PostgreSQL protocol session',blocks:true},
    {layer:'protocol',name:'Authentication',target:ep.host,detail:'Authenticate configured DSN credentials',blocks:true},
    {layer:'target',name:'SELECT 1',target:String(s.params.table||'points'),detail:'Execute lightweight read-only query',blocks:true},
  ]
}
function simulatedCheckFailure(s:SinkDef,name:string){
  if(s.name==='kafka_main'&&name==='Broker Session') return {code:'BROKER_TIMEOUT',latency:3000}
  if(s.name==='db_main'&&name==='TCP Port') return {code:'TCP_CONNECTION_REFUSED',latency:32}
  return null
}
function simulatedIcmpWarning(s:SinkDef,name:string){
  return name==='ICMP' && s.type==='kafka'
}
async function verifySink(s:SinkDef,quiet=false){
  if(!quiet)testLoading.value=true
  const plan=checkPlan(s)
  const checks:SinkVerificationCheck[]=[]
  s.runtime_state='testing'
  let blocked=false
  for(const step of plan){
    await sleep(90)
    if(blocked){
      checks.push({layer:step.layer,name:step.name,state:'skipped',target:step.target,latency_ms:0,detail:'Skipped after upstream failure',error_code:''})
      continue
    }
    if(simulatedIcmpWarning(s,step.name)){
      checks.push({layer:step.layer,name:step.name,state:'warning',target:step.target,latency_ms:1000,detail:'No ICMP reply; continue because TCP is authoritative',error_code:'ICMP_NO_REPLY'})
      continue
    }
    const failure=simulatedCheckFailure(s,step.name)
    if(failure){
      checks.push({layer:step.layer,name:step.name,state:'failed',target:step.target,latency_ms:failure.latency,detail:step.detail+' failed',error_code:failure.code})
      if(step.blocks) blocked=true
    }else{
      checks.push({layer:step.layer,name:step.name,state:'passed',target:step.target,latency_ms:2+checks.length*4,detail:step.detail+' passed',error_code:''})
    }
  }
  const passed=checks.filter(c=>c.state==='passed').length
  const failed=checks.some(c=>c.state==='failed')
  s.verification={state:failed?'failed':'passed',checked_at:nowText(),passed,total:checks.length,checks}
  s.last_test_at=s.verification.checked_at
  s.latency_ms=Math.max(0,...checks.map(c=>c.latency_ms))
  s.runtime_state=s.enabled?(failed?'failed':'healthy'):'disabled'
  s.error=failed?(checks.find(c=>c.state==='failed')?.error_code||'Verification failed'):''
  if(!quiet){
    if(failed)ElMessage.error(`${s.name}: ${verifyLabel(s)}`)
    else ElMessage.success(`${s.name}: ${verifyLabel(s)}`)
    testLoading.value=false
  }
}
async function verifyAll(){
  if(verifyAllRunning.value)return
  const targets=[...store.sinks]
  if(!targets.length){ElMessage.warning('No Sinks configured');return}
  verifyAllRunning.value=true
  try{
    for(const s of targets)await verifySink(s,true)
    const failed=targets.filter(s=>s.verification.state==='failed').length
    const warning=targets.filter(s=>s.verification.checks.some(c=>c.state==='warning')).length
    batchVerification.value={
      checked_at:nowText(),
      checked:targets.length,
      passed:targets.length-failed,
      failed,
      warning,
    }
    if(failed)ElMessage.error(`Sink verification complete: ${failed} failed`)
    else if(warning)ElMessage.warning(`Sink verification complete: ${warning} warning`)
    else ElMessage.success('All Sink verifications passed')
  }finally{verifyAllRunning.value=false}
}
async function writeTest(s:SinkDef){
  await ElMessageBox.confirm('Write one synthetic PointValue to "'+s.name+'"? This test has a real side effect in the backend implementation.','Write Test',{type:'warning',confirmButtonText:'Write Test'})
  testLoading.value=true;await sleep(420);s.last_write_at=nowText();s.writes_total+=1;testResult.value={ok:true,title:'Write test passed',detail:'Synthetic PointValue accepted by the Sink (mock).',latency:18};testLoading.value=false
}
async function toggleEnabled(s:SinkDef,enabled:boolean){
  const refs=taskRefs(s.name)
  if(!enabled&&refs.length){try{await ElMessageBox.confirm('Disabling "'+s.name+'" makes '+refs.length+' referencing Task(s) INVALID.','Disable Sink',{type:'warning',confirmButtonText:'Disable'})}catch{s.enabled=true;return}}
  s.runtime_state=enabled?'unknown':'disabled';refreshTaskValidity()
}
async function deleteSink(s:SinkDef){
  const refs=taskRefs(s.name);if(refs.length){ElMessage.warning('Cannot delete: referenced by '+refs.length+' Task(s)');return}
  await ElMessageBox.confirm('Delete Sink "'+s.name+'"?','Delete Sink',{type:'warning',confirmButtonText:'Delete'})
  store.sinks.splice(store.sinks.indexOf(s),1);drawerOpen.value=false;ElMessage.success('Sink deleted (mock)')
}
</script>

<template>
  <div class="sinks-page">
    <div class="head page-head"><div><h1>Sinks</h1><p>输出端配置、运行状态、连通性与写入测试</p></div><div class="head-actions"><el-button type="primary" @click="openAdd">+ Add Sink</el-button><el-dropdown trigger="click"><el-button :loading="verifyAllRunning">Actions</el-button><template #dropdown><el-dropdown-menu><el-dropdown-item :disabled="verifyAllRunning||!store.sinks.length" title="Non-writing staged verification; Write Test is never included" @click="verifyAll">Verify All Sinks</el-dropdown-item></el-dropdown-menu></template></el-dropdown></div></div>

    <el-card shadow="never">
      <div class="sink-filter-row">
        <div class="sink-filters"><el-input v-model="search" clearable placeholder="Search name / endpoint..."/><el-select v-model="typeFilter"><el-option label="All Types" value="All"/><el-option label="Kafka" value="kafka"/><el-option label="PostgreSQL" value="db"/><el-option label="File" value="file"/></el-select><el-select v-model="stateFilter"><el-option label="All States" value="All"/><el-option label="Healthy" value="healthy"/><el-option label="Failed" value="failed"/><el-option label="Disabled" value="disabled"/><el-option label="Unknown" value="unknown"/></el-select></div>
        <div v-if="batchVerification" class="batch-verify-summary">
          <span>Last Verification</span>
          <b>{{batchVerification.checked}} checked · {{batchVerification.passed}} passed · {{batchVerification.failed}} failed<template v-if="batchVerification.warning"> · {{batchVerification.warning}} warning</template></b>
          <time>{{batchVerification.checked_at}}</time>
        </div>
      </div>
    </el-card>

    <el-card shadow="never">
      <el-table :data="rows" row-key="name">
        <el-table-column label="Sink" min-width="150"><template #default="{row}"><el-link :underline="false" @click="openSink(row)"><b>{{row.name}}</b></el-link></template></el-table-column>
        <el-table-column label="Type" width="110"><template #default="{row}">{{row.type==='db'?'PostgreSQL':row.type.toUpperCase()}}</template></el-table-column>
        <el-table-column label="Endpoint" min-width="220" show-overflow-tooltip><template #default="{row}">{{endpointSummary(row)}}</template></el-table-column>
        <el-table-column label="Verification" width="125"><template #default="{row}"><el-tag :type="verifyType(row)" size="small">{{verifyLabel(row)}}</el-tag></template></el-table-column>
        <el-table-column label="Last Verified" min-width="145"><template #default="{row}">{{row.verification.checked_at||'Never'}}</template></el-table-column>
        <el-table-column label="State" width="105"><template #default="{row}"><el-tag :type="stateType(row.enabled?row.runtime_state:'disabled')" size="small">{{stateLabel(row)}}</el-tag></template></el-table-column>
        <el-table-column label="Tasks" width="75" align="right"><template #default="{row}">{{taskRefs(row.name).length}}</template></el-table-column>
        <el-table-column label="Enabled" width="95"><template #default="{row}"><el-switch v-model="row.enabled" @change="toggleEnabled(row,!!$event)"/></template></el-table-column>
      </el-table>
    </el-card>

    <el-drawer v-model="drawerOpen" :title="creating?'Add Sink':selected?.name||'Sink'" direction="rtl" :size="drawerSize" append-to-body destroy-on-close :before-close="beforeSinkClose">
      <template v-if="creating">
        <h3>Create Sink</h3>
        <p class="subtle">Choose a type and configure its required parameters.</p>
        <el-form label-position="top">
          <div class="sink-form-grid"><el-form-item label="Name"><el-input v-model="draft.name"/></el-form-item><el-form-item label="Type"><el-select v-model="draft.type" @change="onTypeChange" class="app-full-width"><el-option label="Kafka" value="kafka"/><el-option label="PostgreSQL" value="db"/><el-option label="File" value="file"/></el-select></el-form-item><el-form-item label="Enabled"><el-switch v-model="draft.enabled"/></el-form-item></div>
          <template v-if="draft.type==='kafka'"><div class="sink-form-grid"><el-form-item label="Bootstrap Servers"><el-input v-model="draft.bootstrap_servers" placeholder="localhost:9092"/></el-form-item><el-form-item label="Topic"><el-input v-model="draft.topic"/></el-form-item><el-form-item label="Acks"><el-input v-model="draft.acks"/></el-form-item><el-form-item label="Retries"><el-input-number v-model="draft.retries" :min="1"/ class="app-full-width"></el-form-item></div></template>
          <template v-else-if="draft.type==='db'"><div class="sink-form-grid"><el-form-item label="DSN"><el-input v-model="draft.dsn" type="password" show-password/></el-form-item><el-form-item label="Table"><el-input v-model="draft.table"/></el-form-item><el-form-item label="Batch Size"><el-input-number v-model="draft.db_batch_size" :min="1"/ class="app-full-width"></el-form-item><el-form-item label="Create Table"><el-switch v-model="draft.create_table"/></el-form-item></div></template>
          <template v-else><div class="sink-form-grid"><el-form-item label="Path"><el-input v-model="draft.path" placeholder="/var/tmp/wind-hub/archive.jsonl"/></el-form-item><el-form-item label="Format"><el-select v-model="draft.format" class="app-full-width"><el-option label="JSONL" value="jsonl"/><el-option label="CSV" value="csv"/></el-select></el-form-item><el-form-item label="Max Size (MB)"><el-input-number v-model="draft.max_size_mb" :min="0"/ class="app-full-width"></el-form-item><el-form-item label="Flush Interval"><el-input-number v-model="draft.flush_interval" :min="0" :step="0.1"/ class="app-full-width"></el-form-item></div></template>
        </el-form>
        <div class="editor-actions"><el-button type="primary" @click="saveSink">Create Sink</el-button></div>
      </template>

      <el-tabs v-else v-model="drawerTab">
        <el-tab-pane label="Summary" name="Summary">
          <div class="sink-summary-grid" v-if="selected">
            <section class="sink-editor-card">
              <div class="section-head"><div><h3>Parameters</h3><p>Effective configuration is directly editable.</p></div><el-button type="primary" :disabled="!sinkDirty" @click="saveSink">Save</el-button></div>
              <el-form label-position="top">
                <div class="sink-form-grid"><el-form-item label="Name"><el-input v-model="draft.name" disabled/></el-form-item><el-form-item label="Type"><el-input :model-value="draft.type==='db'?'PostgreSQL':draft.type.toUpperCase()" disabled/></el-form-item><el-form-item label="Enabled"><el-switch v-model="draft.enabled"/></el-form-item></div>
                <template v-if="draft.type==='kafka'"><div class="sink-form-grid"><el-form-item label="Bootstrap Servers"><el-input v-model="draft.bootstrap_servers"/></el-form-item><el-form-item label="Topic"><el-input v-model="draft.topic"/></el-form-item><el-form-item label="Key Field"><el-input v-model="draft.key_field"/></el-form-item><el-form-item label="Compression"><el-input v-model="draft.compression_type"/></el-form-item><el-form-item label="Acks"><el-input v-model="draft.acks"/></el-form-item><el-form-item label="Retries"><el-input-number v-model="draft.retries" :min="1"/ class="app-full-width"></el-form-item><el-form-item label="Batch Size"><el-input-number v-model="draft.kafka_batch_size" :min="1"/ class="app-full-width"></el-form-item><el-form-item label="Linger (ms)"><el-input-number v-model="draft.linger_ms" :min="0"/ class="app-full-width"></el-form-item></div></template>
                <template v-else-if="draft.type==='db'"><div class="sink-form-grid"><el-form-item label="DSN"><el-input v-model="draft.dsn" type="password" show-password/></el-form-item><el-form-item label="Table"><el-input v-model="draft.table"/></el-form-item><el-form-item label="Batch Size"><el-input-number v-model="draft.db_batch_size" :min="1"/ class="app-full-width"></el-form-item><el-form-item label="Create Table"><el-switch v-model="draft.create_table"/></el-form-item><el-form-item label="Pool Min"><el-input-number v-model="draft.pool_min_size" :min="1"/ class="app-full-width"></el-form-item><el-form-item label="Pool Max"><el-input-number v-model="draft.pool_max_size" :min="1"/ class="app-full-width"></el-form-item></div></template>
                <template v-else><div class="sink-form-grid"><el-form-item label="Path"><el-input v-model="draft.path"/></el-form-item><el-form-item label="Format"><el-select v-model="draft.format" class="app-full-width"><el-option label="JSONL" value="jsonl"/><el-option label="CSV" value="csv"/></el-select></el-form-item><el-form-item label="Max Size (MB)"><el-input-number v-model="draft.max_size_mb" :min="0"/ class="app-full-width"></el-form-item><el-form-item label="Max Age (h)"><el-input-number v-model="draft.max_age_hours" :min="0"/ class="app-full-width"></el-form-item><el-form-item label="Compress"><el-switch v-model="draft.compress"/></el-form-item><el-form-item label="Compression Level"><el-input-number v-model="draft.compress_level" :min="1" :max="9"/ class="app-full-width"></el-form-item><el-form-item label="Buffer Size"><el-input-number v-model="draft.buffer_size" :min="1"/ class="app-full-width"></el-form-item><el-form-item label="Flush Interval"><el-input-number v-model="draft.flush_interval" :min="0" :step="0.1"/ class="app-full-width"></el-form-item></div></template>
              </el-form>
            </section>
            <aside class="sink-runtime-card">
              <h3>Runtime</h3>
              <div class="runtime-list"><div><span>State</span><b>{{stateLabel(selected)}}</b></div><div><span>Referenced Tasks</span><b>{{taskRefs(selected.name).length}}</b></div><div><span>Queue Depth</span><b>{{selected.queue_depth}}</b></div><div><span>Last Write</span><b>{{selected.last_write_at||'Never'}}</b></div><div><span>Writes</span><b>{{selected.writes_total}}</b></div><div><span>Failures</span><b>{{selected.failures_total}}</b></div><div><span>Dropped</span><b>{{selected.dropped_points}}</b></div></div>
              <div class="danger-row"><el-button type="danger" plain @click="deleteSink(selected)">Delete Sink</el-button></div>
            </aside>
          </div>
        </el-tab-pane>

        <el-tab-pane label="Test" name="Test" v-if="selected">
          <div class="section-head"><div><h3>Connection Verification</h3><p>No business data is written. The same staged checks are used by Verify All Sinks.</p></div><el-button type="primary" :loading="testLoading" @click="verifySink(selected)">Verify</el-button></div>
          <el-table :data="selected.verification.checks" size="small" empty-text="Not verified yet" table-layout="fixed">
            <el-table-column label="Check" width="190">
              <template #default="{row}">
                <div class="verify-check"><span>{{row.layer}}</span><b>{{row.name}}</b></div>
              </template>
            </el-table-column>
            <el-table-column label="Result" width="100">
              <template #default="{row}">
                <el-tag :type="row.state==='passed'?'success':row.state==='warning'?'warning':row.state==='failed'?'danger':'info'" size="small">{{row.state}}</el-tag>
              </template>
            </el-table-column>
            <el-table-column label="Evidence" min-width="260">
              <template #default="{row}">
                <div class="verify-evidence">
                  <b>{{row.target || '—'}}</b>
                  <span>{{row.error_code ? row.error_code + ' · ' + row.detail : row.detail}}</span>
                </div>
              </template>
            </el-table-column>
            <el-table-column label="Time" width="90" align="right">
              <template #default="{row}">{{row.latency_ms ? row.latency_ms + ' ms' : '—'}}</template>
            </el-table-column>
          </el-table>
          <div class="test-meta">Last verified: {{selected.verification.checked_at||'Never'}} · {{verifyLabel(selected)}}</div>
          <el-divider content-position="left">Write Test</el-divider>
          <el-alert type="warning" :closable="false" title="Write Test sends one synthetic PointValue and therefore has a real side effect."/>
          <el-button class="app-mt-3" :loading="testLoading" @click="writeTest(selected)">Write Test</el-button>
          <el-result v-if="testResult" :icon="testResult.ok?'success':'error'" :title="testResult.title" :sub-title="testResult.detail"/>
        </el-tab-pane>
      </el-tabs>
    </el-drawer>
  </div>
</template>

<style scoped>
.sink-filter-row{display:flex;align-items:center;justify-content:space-between;gap:var(--app-space-4)}.sink-filters{display:grid;grid-template-columns:minmax(260px,1fr) 180px 180px;gap:var(--app-space-3);flex:1}.batch-verify-summary{display:grid;gap:2px;min-width:260px;text-align:right}.batch-verify-summary span,.batch-verify-summary time{color:var(--app-text-muted);font-size:var(--app-font-caption)}.batch-verify-summary b{font-size:var(--app-font-body);font-weight:var(--app-font-weight-semibold)}.sink-summary-grid{display:grid;grid-template-columns:minmax(0,1.45fr) minmax(280px,.55fr);gap:var(--app-space-4)}.sink-editor-card,.sink-runtime-card{border:1px solid var(--app-border-soft);border-radius:var(--app-card-radius);padding:var(--app-space-4);min-width:0}.section-head{display:flex;align-items:flex-start;justify-content:space-between;gap:var(--app-space-3);margin-bottom:var(--app-space-4)}.section-head h3,.sink-runtime-card h3{margin:0}.section-head p{margin:4px 0 0;color:var(--app-text-muted);font-size:var(--app-font-caption)}.sink-form-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:0 var(--app-space-4)}.runtime-list{display:grid;gap:var(--app-space-2)}.runtime-list>div{display:flex;justify-content:space-between;gap:var(--app-space-3);padding:7px 0;border-bottom:1px solid var(--app-border-soft)}.runtime-list span{color:var(--app-text-muted)}.runtime-list b{text-align:right}.danger-row{display:flex;justify-content:flex-end;margin-top:var(--app-space-4)}.verify-check,.verify-evidence{display:grid;gap:2px;min-width:0}.verify-check span{color:var(--app-text-muted);font-size:var(--app-font-caption);text-transform:capitalize}.verify-check b{font-size:var(--app-font-body);font-weight:var(--app-font-weight-semibold)}.verify-evidence b{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:var(--app-font-label);font-weight:var(--app-font-weight-medium)}.verify-evidence span{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:var(--app-text-muted);font-size:var(--app-font-caption)}
.test-meta{margin-top:var(--app-space-3);color:var(--app-text-muted);font-size:var(--app-font-caption)}.editor-actions{display:flex;justify-content:flex-end;margin-top:var(--app-space-4)}
@media(max-width:1199px){.sink-summary-grid{grid-template-columns:1fr}}
@media(max-width:1199px){.sink-filter-row{align-items:stretch;flex-direction:column}.batch-verify-summary{text-align:left}}
@media(max-width:767px){.sink-filters,.sink-form-grid{grid-template-columns:1fr}.section-head{align-items:flex-start;flex-direction:column}}
</style>
