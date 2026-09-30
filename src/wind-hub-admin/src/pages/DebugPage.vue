<script setup lang="ts">
import { computed, onBeforeUnmount, reactive, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { effectiveConnection, pointsOfTable, protocolOfDevice, store, tableOfDevice, unitSymbol } from '../mock/data'
import { DATA_TYPES, MODBUS_REGISTER_TYPES } from '../mock/types'

type TargetMode='defined'|'manual'
type DefinedKind='device'|'sink'|'point'
type NetworkTool='reachability'|'ports'|'discovery'

const targetMode=ref<TargetMode>('defined')
const definedKind=ref<DefinedKind>('device')
const selectedDeviceId=ref(store.devices[0]?.device_id||'')
const selectedSinkName=ref(store.sinks[0]?.name||'')
const selectedPointId=ref('')
const activeTool=ref<'network'|'protocol'|'data'|'write'>('network')
const networkTool=ref<NetworkTool>('reachability')
const running=ref(false)
const viewportWidth=ref(window.innerWidth)
const isTablet=computed(()=>viewportWidth.value<1200)
function updateViewport(){viewportWidth.value=window.innerWidth}
window.addEventListener('resize',updateViewport)
onBeforeUnmount(()=>window.removeEventListener('resize',updateViewport))

const manual=reactive({host:'192.168.151.25',protocol:'ads'})
const discovery=reactive({cidr:'192.168.151.0/24',knownOnly:false})
const portProbe=reactive({profile:'configured',customPorts:'48898, 502, 2404'})

const selectedDevice=computed(()=>store.devices.find(d=>d.device_id===selectedDeviceId.value))
const selectedSink=computed(()=>store.sinks.find(s=>s.name===selectedSinkName.value))
const devicePoints=computed(()=>selectedDevice.value?pointsOfTable(tableOfDevice(selectedDevice.value)):[])
watch(selectedDeviceId,()=>{selectedPointId.value=devicePoints.value[0]?.point_id||''},{immediate:true})

function sinkHost(){
  const s=selectedSink.value
  if(!s)return ''
  const raw=String(s.params.bootstrap_servers||s.params.dsn||s.params.path||'')
  if(raw.includes('://'))return raw.split('://')[1]?.split(/[/:]/)[0]||raw
  return raw.split(':')[0]
}
function sinkPort(){
  const s=selectedSink.value
  if(!s)return 0
  if(s.type==='kafka')return Number(String(s.params.bootstrap_servers||'localhost:9092').split(':').pop())||9092
  if(s.type==='db')return 5432
  return 0
}
const resolvedTarget=computed(()=>{
  if(targetMode.value==='manual'){
    return {name:'Manual target',host:manual.host,port:0,protocol:manual.protocol,pointTable:'—',group:'—'}
  }
  if(definedKind.value==='sink'){
    return {name:selectedSink.value?.name||'Sink',host:sinkHost(),port:sinkPort(),protocol:selectedSink.value?.type||'',pointTable:'—',group:'—'}
  }
  const d=selectedDevice.value
  const conn=d?effectiveConnection(d):{}
  return {
    name:d?.device_id||'Device',
    host:d?.host||'',
    port:Number(conn.port||0),
    protocol:d?protocolOfDevice(d):'',
    pointTable:d?tableOfDevice(d):'—',
    group:d?.device_group||'—',
  }
})

const selectedPoint=computed(()=>devicePoints.value.find(p=>p.point_id===selectedPointId.value))
function pointAddress(p:any){
  if(p?.address?.symbol)return p.address.symbol
  if(p?.address?.address!==undefined)return String(p.address.type||'')+' '+p.address.address
  if(p?.address?.ioa!==undefined)return 'IOA '+p.address.ioa
  return '—'
}

function sleep(ms:number){return new Promise(resolve=>setTimeout(resolve,ms))}
const results=ref<Array<Record<string,string|number>>>([])
const resultPage=ref(1)
const resultPageSize=ref(20)
const pagedResults=computed(()=>{
  const start=(resultPage.value-1)*resultPageSize.value
  return results.value.slice(start,start+resultPageSize.value)
})

async function runReachability(){
  running.value=true
  await sleep(300)
  results.value=[{IP:resolvedTarget.value.host,Reachable:resolvedTarget.value.host?'Yes':'No',RTT:'8 ms',KnownObject:resolvedTarget.value.name,Time:new Date().toLocaleTimeString()}]
  running.value=false
}
function configuredPorts(){
  const protocol=resolvedTarget.value.protocol
  if(protocol==='ads')return [{Port:48898,Service:'ADS Router'}]
  if(protocol==='modbus')return [{Port:resolvedTarget.value.port||502,Service:'Modbus TCP'}]
  if(protocol==='iec104')return [{Port:resolvedTarget.value.port||2404,Service:'IEC 104'}]
  if(protocol==='db')return [{Port:resolvedTarget.value.port||5432,Service:'PostgreSQL'}]
  if(protocol==='redis')return [{Port:resolvedTarget.value.port||6379,Service:'Redis'}]
  if(protocol==='kafka')return [{Port:resolvedTarget.value.port||9092,Service:'Kafka'}]
  return []
}
function commonPorts(){
  return [
    {Port:48898,Service:'ADS Router'},{Port:502,Service:'Modbus TCP'},{Port:2404,Service:'IEC 104'},
    {Port:5432,Service:'PostgreSQL'},{Port:6379,Service:'Redis'},{Port:9092,Service:'Kafka'},
  ]
}
function customPorts(){
  return portProbe.customPorts.split(',').map(x=>Number(x.trim())).filter(x=>Number.isInteger(x)&&x>0&&x<=65535).map(Port=>({Port,Service:'Custom'}))
}
async function runPortProbe(){
  running.value=true
  await sleep(380)
  const profile=portProbe.profile==='configured'?configuredPorts():portProbe.profile==='common'?commonPorts():customPorts()
  results.value=profile.map((p,index)=>({
    Host:resolvedTarget.value.host,
    Port:p.Port,
    Service:p.Service,
    State:index===0?'Open':'Closed',
    Latency:(6+index*3)+' ms',
  }))
  resultPage.value=1
  running.value=false
}
async function runDiscovery(){
  running.value=true
  await sleep(450)
  const prefix=discovery.cidr.split('/')[0].split('.').slice(0,3).join('.')
  const rows=store.devices.filter(d=>!prefix||d.host.startsWith(prefix+'.')).map(d=>({
    IP:d.host,Reachable:d.online?'Yes':'No',RTT:d.online?8+(Number(d.device_id.replace(/\D/g,''))%13)+' ms':'—',
    KnownObject:d.device_id,Protocol:protocolOfDevice(d).toUpperCase(),
  }))
  results.value=discovery.knownOnly?rows.filter(x=>x.KnownObject):rows
  resultPage.value=1
  running.value=false
}
async function runNetwork(){
  if(networkTool.value==='reachability')await runReachability()
  else if(networkTool.value==='ports')await runPortProbe()
  else await runDiscovery()
}

const protocolResult=ref<Array<Record<string,string|number>>>([])
const protocolActions=computed(()=>{
  const p=resolvedTarget.value.protocol
  if(p==='ads')return ['Connect','Read State','Read Symbol','Read IG/IO']
  if(p==='modbus')return ['Connect','Read Holding','Read Input','Read Coil','Read Discrete']
  if(p==='iec104')return ['Connect','General Interrogation']
  return ['TCP Connect','Session / Auth','Target Check']
})
const protocolContext=computed(()=>{
  const d=selectedDevice.value
  const conn=d?effectiveConnection(d):{}
  return [
    ['Protocol',resolvedTarget.value.protocol.toUpperCase()||'UNSPECIFIED'],
    ['Host',resolvedTarget.value.host||'—'],
    ['TCP Port',String(resolvedTarget.value.port||'—')],
    ...(resolvedTarget.value.protocol==='ads'?[['AMS Net ID',String(d?.extensions?.target_net_id||'—')],['ADS Target Port',String(conn.target_port||801)]]:[]),
    ...(resolvedTarget.value.protocol==='modbus'?[['Unit ID',String(conn.unit_id||1)]]:[]),
  ]
})
async function protocolAction(action:string){
  running.value=true
  await sleep(350)
  protocolResult.value=[{Check:action,Target:resolvedTarget.value.host,Result:'Passed',Latency:'18 ms',Evidence:resolvedTarget.value.protocol.toUpperCase()+' response received'}]
  running.value=false
}

const readMode=ref<'defined'|'manual'>('defined')
const manualRead=reactive({symbol:'',register_type:'holding',address:0,ioa:1,data_type:'float32'})
const readResults=ref<Array<Record<string,string|number>>>([])
async function readOnce(){
  running.value=true
  await sleep(320)
  const p=selectedPoint.value
  const address=readMode.value==='defined'
    ?pointAddress(p)
    :resolvedTarget.value.protocol==='ads'?manualRead.symbol
    :resolvedTarget.value.protocol==='modbus'?manualRead.register_type+' '+manualRead.address
    :'IOA '+manualRead.ioa
  readResults.value=[{
    Target:resolvedTarget.value.name,Address:address||'—',Type:readMode.value==='defined'?(p?.data_type||'—'):manualRead.data_type,
    Raw:'42 C8 00 00',Decoded:'100',Engineering:'100 '+(p?unitSymbol(p.unit):''),Timestamp:new Date().toLocaleTimeString(),Latency:'14 ms',
  }]
  running.value=false
}

const writeMode=ref<'defined'|'manual'>('defined')
const writePointId=ref('')
const writeValue=ref('')
const writablePoints=computed(()=>devicePoints.value.filter(p=>p.point_groups.includes('control')))
const writeResult=ref<Array<Record<string,string|number>>>([])
async function runWrite(){
  if(!writeValue.value.trim()){ElMessage.warning('Enter a write value');return}
  const p=writeMode.value==='defined'?writablePoints.value.find(x=>x.point_id===writePointId.value):null
  await ElMessageBox.confirm(
    'Send one diagnostic write to '+resolvedTarget.value.name+'? Production backend must audit this operation and perform readback.',
    'Diagnostic Write',
    {type:'warning',confirmButtonText:'Write Once'},
  )
  running.value=true
  await sleep(380)
  writeResult.value=[{Target:resolvedTarget.value.name,Point:p?.point_id||'Manual address',Value:writeValue.value,Result:'Passed',Readback:writeValue.value,Latency:'21 ms'}]
  running.value=false
}
</script>

<template>
  <div class="diagnostics-page">
    <div class="head">
      <div><h1>Diagnostics</h1><p>工程探索工作台：选择已定义对象或手工目标，再执行网络、协议、读写探索。</p></div>
    </div>

    <el-card shadow="never" class="target-card">
      <div class="target-heading">
        <div><h2>Target</h2><p>下面所有工具共享同一个目标上下文；CIDR 与端口参数由具体工具提供。</p></div>
        <el-segmented v-model="targetMode" :options="[{label:'Defined Object',value:'defined'},{label:'Manual Target',value:'manual'}]"/>
      </div>

      <div v-if="targetMode==='defined'" class="target-inputs">
        <el-form-item label="Object Type">
          <el-radio-group v-model="definedKind">
            <el-radio-button value="device">Device</el-radio-button>
            <el-radio-button value="sink">Sink</el-radio-button>
            <el-radio-button value="point">Point</el-radio-button>
          </el-radio-group>
        </el-form-item>
        <el-form-item v-if="definedKind!=='sink'" label="Device">
          <el-select v-model="selectedDeviceId" filterable style="width:100%">
            <el-option v-for="d in store.devices" :key="d.device_id" :label="d.device_id+' · '+protocolOfDevice(d).toUpperCase()+' · '+d.host" :value="d.device_id"/>
          </el-select>
        </el-form-item>
        <el-form-item v-if="definedKind==='sink'" label="Sink">
          <el-select v-model="selectedSinkName" filterable style="width:100%">
            <el-option v-for="s in store.sinks" :key="s.name" :label="s.name+' · '+s.type" :value="s.name"/>
          </el-select>
        </el-form-item>
        <el-form-item v-if="definedKind==='point'" label="Point">
          <el-select v-model="selectedPointId" filterable style="width:100%">
            <el-option v-for="p in devicePoints" :key="p.point_id" :label="p.point_id+' · '+pointAddress(p)" :value="p.point_id"/>
          </el-select>
        </el-form-item>
      </div>

      <div v-else class="target-inputs">
        <el-form-item label="Host / IP"><el-input v-model="manual.host"/></el-form-item>
        <el-form-item label="Protocol">
          <el-select v-model="manual.protocol">
            <el-option label="ADS" value="ads"/><el-option label="Modbus TCP" value="modbus"/><el-option label="IEC 104" value="iec104"/><el-option label="Other" value="other"/>
          </el-select>
        </el-form-item>
      </div>

      <el-descriptions :column="isNaN(0)?1:4" class="target-context">
        <el-descriptions-item label="Resolved Target">{{resolvedTarget.name}}</el-descriptions-item>
        <el-descriptions-item label="Protocol">{{resolvedTarget.protocol.toUpperCase()||'UNSPECIFIED'}}</el-descriptions-item>
        <el-descriptions-item label="Host">{{resolvedTarget.host||'—'}}<template v-if="resolvedTarget.port">:{{resolvedTarget.port}}</template></el-descriptions-item>
        <el-descriptions-item label="Context">{{definedKind==='point'&&selectedPoint?selectedPoint.point_id:resolvedTarget.pointTable}}</el-descriptions-item>
      </el-descriptions>
    </el-card>

    <el-tabs v-model="activeTool" class="workspace-tabs">
      <el-tab-pane label="Network" name="network">
        <div class="tool-heading"><div><h2>Network Explorer</h2><p>选择工具后填写该工具需要的参数，再执行并查看结果。</p></div></div>
        <el-segmented v-model="networkTool" :options="[{label:'Reachability',value:'reachability'},{label:'Port Probe',value:'ports'},{label:'Host Discovery',value:'discovery'}]"/>

        <el-card shadow="never" class="tool-card">
          <div class="tool-form-row">
            <template v-if="networkTool==='reachability'">
              <el-form-item label="Host / IP"><el-input :model-value="resolvedTarget.host" readonly/></el-form-item>
              <el-form-item label="Timeout"><el-input model-value="1 s" readonly/></el-form-item>
            </template>

            <template v-else-if="networkTool==='ports'">
              <el-form-item label="Host"><el-input :model-value="resolvedTarget.host" readonly/></el-form-item>
              <el-form-item label="Port Profile">
                <el-select v-model="portProbe.profile">
                  <el-option label="Configured" value="configured"/><el-option label="Wind Hub Common" value="common"/><el-option label="Custom" value="custom"/>
                </el-select>
              </el-form-item>
              <el-form-item v-if="portProbe.profile==='custom'" label="Custom Ports"><el-input v-model="portProbe.customPorts" placeholder="502, 2404, 48898"/></el-form-item>
            </template>

            <template v-else>
              <el-form-item label="Subnet / CIDR"><el-input v-model="discovery.cidr"/></el-form-item>
              <el-form-item label="Known Objects Only"><el-switch v-model="discovery.knownOnly"/></el-form-item>
            </template>

            <div class="run-action"><el-button type="primary" :loading="running" @click="runNetwork">Run</el-button></div>
          </div>
        </el-card>

        <div class="result-heading"><h3>Results</h3><span>{{results.length}} rows</span></div>
        <el-card shadow="never">
          <el-table :data="pagedResults" empty-text="Run the selected network tool">
            <el-table-column v-for="key in Object.keys(results[0]||{})" :key="key" :prop="key" :label="key" min-width="120"/>
          </el-table>
          <div v-if="results.length>resultPageSize" class="pagination">
            <el-pagination v-model:current-page="resultPage" v-model:page-size="resultPageSize" :page-sizes="[20,50,100]" :total="results.length" layout="total, sizes, prev, pager, next"/>
          </div>
        </el-card>
      </el-tab-pane>

      <el-tab-pane label="Protocol" name="protocol">
        <div class="tool-heading"><div><h2>Protocol Explorer</h2><p>参数从当前 Target 解析；临时诊断覆盖不反写正式配置。</p></div></div>
        <el-descriptions :column="isTablet?2:4" border class="protocol-context">
          <el-descriptions-item v-for="item in protocolContext" :key="item[0]" :label="item[0]">{{item[1]}}</el-descriptions-item>
        </el-descriptions>
        <div class="protocol-actions"><el-button v-for="action in protocolActions" :key="action" :loading="running" @click="protocolAction(action)">{{action}}</el-button></div>
        <el-card shadow="never"><el-table :data="protocolResult" empty-text="Choose a protocol action"><el-table-column prop="Check" label="Check"/><el-table-column prop="Target" label="Target" min-width="180"/><el-table-column prop="Result" label="Result"/><el-table-column prop="Latency" label="Latency"/><el-table-column prop="Evidence" label="Evidence" min-width="240"/></el-table></el-card>
      </el-tab-pane>

      <el-tab-pane label="Data" name="data">
        <div class="tool-heading"><div><h2>Data Explorer</h2><p>Defined Point 使用配置；Manual Address 仅用于探索。</p></div></div>
        <el-segmented v-model="readMode" :options="[{label:'Defined Point',value:'defined'},{label:'Manual Address',value:'manual'}]"/>
        <el-card shadow="never" class="tool-card">
          <el-form label-position="top" class="data-form">
            <el-form-item v-if="readMode==='defined'" label="Point"><el-select v-model="selectedPointId" filterable style="width:100%"><el-option v-for="p in devicePoints" :key="p.point_id" :label="p.point_id+' · '+pointAddress(p)" :value="p.point_id"/></el-select></el-form-item>
            <template v-else>
              <el-form-item v-if="resolvedTarget.protocol==='ads'" label="Symbol"><el-input v-model="manualRead.symbol"/></el-form-item>
              <div v-else-if="resolvedTarget.protocol==='modbus'" class="tool-form-row"><el-form-item label="Register Type"><el-select v-model="manualRead.register_type"><el-option v-for="r in MODBUS_REGISTER_TYPES" :key="r" :label="r" :value="r"/></el-select></el-form-item><el-form-item label="0-based Address"><el-input-number v-model="manualRead.address" :min="0" style="width:100%"/></el-form-item></div>
              <el-form-item v-else label="IOA"><el-input-number v-model="manualRead.ioa" :min="0"/></el-form-item>
              <el-form-item label="Data Type"><el-select v-model="manualRead.data_type"><el-option v-for="t in DATA_TYPES" :key="t" :label="t" :value="t"/></el-select></el-form-item>
            </template>
            <el-button type="primary" :loading="running" @click="readOnce">Read Once</el-button>
          </el-form>
        </el-card>
        <el-card shadow="never"><el-table :data="readResults" empty-text="Run a read"><el-table-column prop="Target" label="Target"/><el-table-column prop="Address" label="Address" min-width="180"/><el-table-column prop="Type" label="Type"/><el-table-column prop="Raw" label="Raw"/><el-table-column prop="Decoded" label="Decoded"/><el-table-column prop="Engineering" label="Engineering"/><el-table-column prop="Timestamp" label="Timestamp"/><el-table-column prop="Latency" label="Latency"/></el-table></el-card>
      </el-tab-pane>

      <el-tab-pane label="Write" name="write">
        <div class="tool-heading"><div><h2>Write Explorer</h2><p>写入单次执行、二次确认、生产后端审计并 readback。</p></div></div>
        <el-alert type="warning" :closable="false" title="Write tests can change real equipment state."/>
        <el-card shadow="never" class="tool-card">
          <el-segmented v-model="writeMode" :options="[{label:'Defined Point',value:'defined'},{label:'Manual Address',value:'manual'}]"/>
          <el-form label-position="top" class="data-form">
            <el-form-item v-if="writeMode==='defined'" label="Writable Point"><el-select v-model="writePointId" filterable style="width:100%"><el-option v-for="p in writablePoints" :key="p.point_id" :label="p.point_id+' · '+pointAddress(p)" :value="p.point_id"/></el-select></el-form-item>
            <el-form-item v-else label="Manual Address / Symbol"><el-input v-model="manualRead.symbol"/></el-form-item>
            <el-form-item label="Write Value"><el-input v-model="writeValue"/></el-form-item>
            <el-button type="danger" :loading="running" @click="runWrite">Write Once & Readback</el-button>
          </el-form>
        </el-card>
        <el-card shadow="never"><el-table :data="writeResult" empty-text="No write executed"><el-table-column prop="Target" label="Target"/><el-table-column prop="Point" label="Point"/><el-table-column prop="Value" label="Write"/><el-table-column prop="Result" label="Result"/><el-table-column prop="Readback" label="Readback"/><el-table-column prop="Latency" label="Latency"/></el-table></el-card>
      </el-tab-pane>
    </el-tabs>
  </div>
</template>


<style scoped>
.target-card{margin-bottom:var(--app-space-4)}.target-heading{display:flex;align-items:flex-start;justify-content:space-between;gap:var(--app-space-4)}.target-heading h2,.tool-heading h2{margin:0;font-size:var(--app-font-section-title)}.target-heading p,.tool-heading p{margin:var(--app-space-1) 0 0;color:var(--app-text-muted);font-size:var(--app-font-caption)}.target-inputs{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:var(--app-space-3);margin-top:var(--app-space-4)}.target-inputs :deep(.el-form-item){margin-bottom:0}.target-context{margin-top:var(--app-space-4);padding-top:var(--app-space-3);border-top:1px solid var(--app-border-soft)}
.workspace-tabs{margin-top:var(--app-space-2)}.tool-heading{margin:var(--app-space-3) 0}.tool-card{margin:var(--app-space-3) 0}.tool-form-row{display:grid;grid-template-columns:repeat(3,minmax(0,1fr)) auto;gap:var(--app-space-3);align-items:end}.tool-form-row :deep(.el-form-item){margin-bottom:0}.run-action{display:flex;justify-content:flex-end}.result-heading{display:flex;align-items:center;justify-content:space-between;gap:var(--app-space-3);margin:var(--app-space-4) 0 var(--app-space-2)}.result-heading h3{margin:0;font-size:var(--app-font-panel-title)}.result-heading span{color:var(--app-text-muted);font-size:var(--app-font-caption)}.pagination{display:flex;justify-content:flex-end;padding-top:var(--app-space-3)}.protocol-context{margin-bottom:var(--app-space-3)}.protocol-actions{display:flex;gap:var(--app-space-2);flex-wrap:wrap;margin-bottom:var(--app-space-3)}.data-form{display:grid;gap:var(--app-space-3);max-width:44rem}
@media(max-width:1199px){.target-inputs{grid-template-columns:repeat(2,minmax(0,1fr))}.tool-form-row{grid-template-columns:repeat(2,minmax(0,1fr))}.run-action{justify-content:flex-start}}
@media(max-width:767px){.target-heading{flex-direction:column}.target-inputs,.tool-form-row{grid-template-columns:1fr}.pagination{justify-content:center;overflow-x:auto}}
</style>