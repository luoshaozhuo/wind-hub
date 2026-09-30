<script setup lang="ts">
import { computed, reactive, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { useViewport } from '../composables/useViewport'
import { effectiveConnection, pointsOfTable, protocolOfDevice, store, tableOfDevice, unitSymbol } from '../mock/data'
import { DATA_TYPES, MODBUS_REGISTER_TYPES } from '../mock/types'

type TargetMode='defined'|'manual'
type DefinedKind='device'|'sink'|'point'
type ExplorerTab='network'|'read'|'write'
type NetworkTool='reachability'|'ports'|'protocol'|'discovery'

const targetMode=ref<TargetMode>('defined')
const definedKind=ref<DefinedKind>('device')
const selectedDeviceId=ref(store.devices[0]?.device_id||'')
const selectedSinkName=ref(store.sinks[0]?.name||'')
const selectedPointId=ref('')
const explorerTab=ref<ExplorerTab>('network')
const networkTool=ref<NetworkTool>('reachability')
const running=ref(false)

const { isMobile } = useViewport()
const manualTarget=reactive({host:'192.168.151.25',protocol:'ads'})
const discovery=reactive({cidr:'192.168.151.0/24',knownOnly:false})
const portProbe=reactive({profile:'configured',customPorts:'48898, 502, 2404'})

const selectedDevice=computed(()=>store.devices.find(d=>d.device_id===selectedDeviceId.value))
const selectedSink=computed(()=>store.sinks.find(s=>s.name===selectedSinkName.value))
const devicePoints=computed(()=>selectedDevice.value?pointsOfTable(tableOfDevice(selectedDevice.value)):[])
watch(selectedDeviceId,()=>{selectedPointId.value=devicePoints.value[0]?.point_id||''},{immediate:true})
const selectedPoint=computed(()=>devicePoints.value.find(p=>p.point_id===selectedPointId.value))

function sinkTarget(){
  const sink=selectedSink.value
  if(!sink)return {host:'',port:0}
  const raw=String(sink.params.bootstrap_servers||sink.params.dsn||sink.params.path||'')
  if(raw.includes('://')){
    const body=raw.split('://')[1]||''
    const host=body.split(/[/:]/)[0]||body
    const port=Number(body.match(/:(\d+)/)?.[1]||0)
    return {host,port}
  }
  const parts=raw.split(':')
  return {host:parts[0]||raw,port:Number(parts[1]||0)}
}

const resolvedTarget=computed(()=>{
  if(targetMode.value==='manual'){
    return {name:'Manual target',host:manualTarget.host,port:0,protocol:manualTarget.protocol,group:'—',pointTable:'—'}
  }
  if(definedKind.value==='sink'){
    const target=sinkTarget()
    return {name:selectedSink.value?.name||'Sink',host:target.host,port:target.port,protocol:selectedSink.value?.type||'',group:'—',pointTable:'—'}
  }
  const device=selectedDevice.value
  const conn=device?effectiveConnection(device):{}
  return {
    name:device?.device_id||'Device',
    host:device?.host||'',
    port:Number(conn.port||0),
    protocol:device?protocolOfDevice(device):'',
    group:device?.device_group||'—',
    pointTable:device?tableOfDevice(device):'—',
  }
})

function pointAddress(point:any){
  if(point?.address?.symbol)return point.address.symbol
  if(point?.address?.address!==undefined)return String(point.address.type||'')+' '+point.address.address
  if(point?.address?.ioa!==undefined)return 'IOA '+point.address.ioa
  return '—'
}
function sleep(ms:number){return new Promise(resolve=>setTimeout(resolve,ms))}

const networkResults=ref<Array<Record<string,string|number>>>([])
const resultPage=ref(1)
const resultPageSize=ref(20)
const pagedResults=computed(()=>{
  const start=(resultPage.value-1)*resultPageSize.value
  return networkResults.value.slice(start,start+resultPageSize.value)
})

function commonPorts(){
  return [
    {Port:48898,Service:'ADS Router'},
    {Port:502,Service:'Modbus TCP'},
    {Port:2404,Service:'IEC 104'},
    {Port:5432,Service:'PostgreSQL'},
    {Port:6379,Service:'Redis'},
    {Port:9092,Service:'Kafka'},
  ]
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
function customPorts(){
  return portProbe.customPorts.split(',')
    .map(value=>Number(value.trim()))
    .filter(value=>Number.isInteger(value)&&value>0&&value<=65535)
    .map(Port=>({Port,Service:'Custom'}))
}
async function runNetwork(){
  running.value=true
  await sleep(350)

  if(networkTool.value==='reachability'){
    networkResults.value=[{
      Host:resolvedTarget.value.host,
      Reachable:resolvedTarget.value.host?'Yes':'No',
      RTT:'8 ms',
      KnownObject:resolvedTarget.value.name,
      Time:new Date().toLocaleTimeString(),
    }]
  }else if(networkTool.value==='ports'){
    const ports=portProbe.profile==='configured'?configuredPorts():portProbe.profile==='common'?commonPorts():customPorts()
    networkResults.value=ports.map((item,index)=>({
      Host:resolvedTarget.value.host,
      Port:item.Port,
      Service:item.Service,
      State:index===0?'Open':'Closed',
      Latency:(6+index*3)+' ms',
    }))
  }else if(networkTool.value==='protocol'){
    const protocol=resolvedTarget.value.protocol.toUpperCase()||'UNSPECIFIED'
    const stages=protocol==='ADS'
      ?['TCP 48898','ADS Session','Read State']
      :protocol==='MODBUS'
        ?['TCP '+(resolvedTarget.value.port||502),'Modbus Session']
        :protocol==='IEC104'
          ?['TCP '+(resolvedTarget.value.port||2404),'STARTDT / Session']
          :['TCP Session','Application Session']
    networkResults.value=stages.map((stage,index)=>({
      Stage:stage,
      Result:'Passed',
      Target:resolvedTarget.value.host,
      Latency:(10+index*5)+' ms',
      Evidence:index===0?'Transport connection established':'Protocol response received',
    }))
  }else{
    const prefix=discovery.cidr.split('/')[0].split('.').slice(0,3).join('.')
    const rows=store.devices.filter(d=>!prefix||d.host.startsWith(prefix+'.')).map(d=>({
      IP:d.host,
      Reachable:d.online?'Yes':'No',
      RTT:d.online?8+(Number(d.device_id.replace(/\D/g,''))%13)+' ms':'—',
      KnownObject:d.device_id,
      Protocol:protocolOfDevice(d).toUpperCase(),
    }))
    networkResults.value=discovery.knownOnly?rows.filter(row=>row.KnownObject):rows
  }

  resultPage.value=1
  running.value=false
}

const readMode=ref<'defined'|'manual'>('defined')
const manualRead=reactive({symbol:'',registerType:'holding',address:0,ioa:1,dataType:'float32'})
const readResults=ref<Array<Record<string,string|number>>>([])
async function readOnce(){
  running.value=true
  await sleep(320)
  const point=selectedPoint.value
  const address=readMode.value==='defined'
    ?pointAddress(point)
    :resolvedTarget.value.protocol==='ads'
      ?manualRead.symbol
      :resolvedTarget.value.protocol==='modbus'
        ?manualRead.registerType+' '+manualRead.address
        :'IOA '+manualRead.ioa
  readResults.value=[{
    Target:resolvedTarget.value.name,
    Address:address||'—',
    Type:readMode.value==='defined'?(point?.data_type||'—'):manualRead.dataType,
    Raw:'42 C8 00 00',
    Decoded:'100',
    Engineering:'100 '+(point?unitSymbol(point.unit):''),
    Timestamp:new Date().toLocaleTimeString(),
    Latency:'14 ms',
  }]
  running.value=false
}

const writeMode=ref<'defined'|'manual'>('defined')
const writePointId=ref('')
const writeValue=ref('')
const writablePoints=computed(()=>devicePoints.value.filter(point=>point.point_groups.includes('control')))
const writeResults=ref<Array<Record<string,string|number>>>([])
async function writeOnce(){
  if(!writeValue.value.trim()){
    ElMessage.warning('Enter a write value')
    return
  }
  const point=writeMode.value==='defined'?writablePoints.value.find(item=>item.point_id===writePointId.value):null
  await ElMessageBox.confirm(
    'Send one diagnostic write to '+resolvedTarget.value.name+'? Production backend must audit this operation and perform readback.',
    'Diagnostic Write',
    {type:'warning',confirmButtonText:'Write Once'},
  )
  running.value=true
  await sleep(380)
  writeResults.value=[{
    Target:resolvedTarget.value.name,
    Point:point?.point_id||'Manual address',
    Value:writeValue.value,
    Result:'Passed',
    Readback:writeValue.value,
    Latency:'21 ms',
  }]
  running.value=false
}

const canReadWrite=computed(()=>targetMode.value==='manual'||definedKind.value!=='sink')
</script>

<template>
  <div class="diagnostics-page">
    <div class="head">
      <div>
        <h1 class="app-page-title">Diagnostics</h1>
        <p class="app-body">工程探索工作台：左侧确定 Target，右侧执行网络、读取和写入探索。</p>
      </div>
    </div>

    <div class="diagnostics-layout">
      <el-card shadow="never" class="target-panel">
        <div class="panel-heading">
          <h2 class="app-section-title">Target</h2>
          <p class="app-caption">Explorer 的所有操作共享当前目标上下文。</p>
        </div>

        <el-segmented
          v-model="targetMode"
          :options="[{label:'Defined Object',value:'defined'},{label:'Manual Target',value:'manual'}]"
          class="target-mode"
        />

        <el-form v-if="targetMode==='defined'" label-position="top" class="target-form">
          <el-form-item label="Object Type">
            <el-radio-group v-model="definedKind">
              <el-radio-button value="device">Device</el-radio-button>
              <el-radio-button value="sink">Sink</el-radio-button>
              <el-radio-button value="point">Point</el-radio-button>
            </el-radio-group>
          </el-form-item>

          <el-form-item v-if="definedKind!=='sink'" label="Device">
            <el-select v-model="selectedDeviceId" filterable class="app-full-width">
              <el-option
                v-for="device in store.devices"
                :key="device.device_id"
                :label="device.device_id+' · '+protocolOfDevice(device).toUpperCase()+' · '+device.host"
                :value="device.device_id"
              />
            </el-select>
          </el-form-item>

          <el-form-item v-if="definedKind==='sink'" label="Sink">
            <el-select v-model="selectedSinkName" filterable class="app-full-width">
              <el-option v-for="sink in store.sinks" :key="sink.name" :label="sink.name+' · '+sink.type" :value="sink.name"/>
            </el-select>
          </el-form-item>

          <el-form-item v-if="definedKind==='point'" label="Point">
            <el-select v-model="selectedPointId" filterable class="app-full-width">
              <el-option v-for="point in devicePoints" :key="point.point_id" :label="point.point_id+' · '+pointAddress(point)" :value="point.point_id"/>
            </el-select>
          </el-form-item>
        </el-form>

        <el-form v-else label-position="top" class="target-form">
          <el-form-item label="Host / IP"><el-input v-model="manualTarget.host"/></el-form-item>
          <el-form-item label="Protocol">
            <el-select v-model="manualTarget.protocol" class="app-full-width">
              <el-option label="ADS" value="ads"/>
              <el-option label="Modbus TCP" value="modbus"/>
              <el-option label="IEC 104" value="iec104"/>
              <el-option label="Other" value="other"/>
            </el-select>
          </el-form-item>
        </el-form>

        <el-divider/>

        <div class="target-summary">
          <span class="app-caption">Resolved Target</span>
          <b class="app-panel-title">{{resolvedTarget.name}}</b>
          <el-descriptions :column="1" size="small">
            <el-descriptions-item label="Protocol">{{resolvedTarget.protocol.toUpperCase()||'UNSPECIFIED'}}</el-descriptions-item>
            <el-descriptions-item label="Host">{{resolvedTarget.host||'—'}}<template v-if="resolvedTarget.port">:{{resolvedTarget.port}}</template></el-descriptions-item>
            <el-descriptions-item v-if="targetMode==='defined'&&definedKind!=='sink'" label="Device Group">{{resolvedTarget.group}}</el-descriptions-item>
            <el-descriptions-item v-if="targetMode==='defined'&&definedKind!=='sink'" label="Point Table">{{resolvedTarget.pointTable}}</el-descriptions-item>
            <el-descriptions-item v-if="definedKind==='point'&&selectedPoint" label="Point">{{selectedPoint.point_id}}</el-descriptions-item>
          </el-descriptions>
        </div>
      </el-card>

      <section class="explorer-panel">
        <el-tabs v-model="explorerTab" class="explorer-tabs">
          <el-tab-pane label="Network" name="network">
            <div class="panel-heading">
              <h2 class="app-section-title">Network Explorer</h2>
              <p class="app-caption">按 IP → TCP Port → Protocol Session 的顺序验证通信链路。</p>
            </div>

            <el-segmented
              v-model="networkTool"
              :options="[
                {label:'Reachability',value:'reachability'},
                {label:'Port Probe',value:'ports'},
                {label:'Protocol Check',value:'protocol'},
                {label:'Host Discovery',value:'discovery'},
              ]"
              class="tool-switch"
            />

            <el-card shadow="never" class="tool-card">
              <el-form label-position="top">
                <div class="tool-fields">
                  <template v-if="networkTool==='reachability'">
                    <el-form-item label="Host / IP"><el-input :model-value="resolvedTarget.host" readonly/></el-form-item>
                    <el-form-item label="Timeout"><el-input model-value="1 s" readonly/></el-form-item>
                  </template>

                  <template v-else-if="networkTool==='ports'">
                    <el-form-item label="Host"><el-input :model-value="resolvedTarget.host" readonly/></el-form-item>
                    <el-form-item label="Port Profile">
                      <el-select v-model="portProbe.profile" class="app-full-width">
                        <el-option label="Configured" value="configured"/>
                        <el-option label="Wind Hub Common" value="common"/>
                        <el-option label="Custom" value="custom"/>
                      </el-select>
                    </el-form-item>
                    <el-form-item v-if="portProbe.profile==='custom'" label="Custom Ports">
                      <el-input v-model="portProbe.customPorts" placeholder="502, 2404, 48898"/>
                    </el-form-item>
                  </template>

                  <template v-else-if="networkTool==='protocol'">
                    <el-form-item label="Protocol"><el-input :model-value="resolvedTarget.protocol.toUpperCase()" readonly/></el-form-item>
                    <el-form-item label="Target"><el-input :model-value="resolvedTarget.host" readonly/></el-form-item>
                    <el-form-item>
                      <template #label>Purpose</template>
                      <span class="field-note">验证协议会话能否建立；不读取业务点，不写入数据。</span>
                    </el-form-item>
                  </template>

                  <template v-else>
                    <el-form-item label="Subnet / CIDR"><el-input v-model="discovery.cidr"/></el-form-item>
                    <el-form-item label="Known Objects Only"><el-switch v-model="discovery.knownOnly"/></el-form-item>
                  </template>
                </div>

                <div class="tool-actions"><el-button type="primary" :loading="running" @click="runNetwork">Run</el-button></div>
              </el-form>
            </el-card>

            <div class="result-heading">
              <h3 class="app-panel-title">Results</h3>
              <span class="app-caption">{{networkResults.length}} rows</span>
            </div>
            <el-card shadow="never">
              <el-table :data="pagedResults" empty-text="Run the selected network tool">
                <el-table-column v-for="key in Object.keys(networkResults[0]||{})" :key="key" :prop="key" :label="key" min-width="120"/>
              </el-table>
              <div v-if="networkResults.length>resultPageSize" class="pagination">
                <el-pagination
                  v-model:current-page="resultPage"
                  v-model:page-size="resultPageSize"
                  :page-sizes="[20,50,100]"
                  :total="networkResults.length"
                  :layout="isMobile?'prev, pager, next':'total, sizes, prev, pager, next'"
                />
              </div>
            </el-card>
          </el-tab-pane>

          <el-tab-pane label="Read" name="read">
            <div class="panel-heading">
              <h2 class="app-section-title">Read Explorer</h2>
              <p class="app-caption">读取已定义 Point，或临时填写协议地址进行现场探索。</p>
            </div>

            <el-alert v-if="!canReadWrite" type="info" :closable="false" title="Read Explorer requires a Device, Point or Manual Target."/>

            <template v-else>
              <el-segmented v-model="readMode" :options="[{label:'Defined Point',value:'defined'},{label:'Manual Address',value:'manual'}]" class="tool-switch"/>
              <el-card shadow="never" class="tool-card">
                <el-form label-position="top">
                  <div class="tool-fields">
                    <el-form-item v-if="readMode==='defined'" label="Point">
                      <el-select v-model="selectedPointId" filterable class="app-full-width">
                        <el-option v-for="point in devicePoints" :key="point.point_id" :label="point.point_id+' · '+pointAddress(point)" :value="point.point_id"/>
                      </el-select>
                    </el-form-item>

                    <template v-else>
                      <el-form-item v-if="resolvedTarget.protocol==='ads'" label="Symbol"><el-input v-model="manualRead.symbol"/></el-form-item>
                      <el-form-item v-else-if="resolvedTarget.protocol==='modbus'" label="Register Type">
                        <el-select v-model="manualRead.registerType" class="app-full-width">
                          <el-option v-for="item in MODBUS_REGISTER_TYPES" :key="item" :label="item" :value="item"/>
                        </el-select>
                      </el-form-item>
                      <el-form-item v-if="resolvedTarget.protocol==='modbus'" label="0-based Address"><el-input-number v-model="manualRead.address" :min="0" class="app-full-width" /></el-form-item>
                      <el-form-item v-if="resolvedTarget.protocol==='iec104'" label="IOA"><el-input-number v-model="manualRead.ioa" :min="0" class="app-full-width" /></el-form-item>
                      <el-form-item label="Data Type">
                        <el-select v-model="manualRead.dataType" class="app-full-width">
                          <el-option v-for="item in DATA_TYPES" :key="item" :label="item" :value="item"/>
                        </el-select>
                      </el-form-item>
                    </template>
                  </div>
                  <div class="tool-actions"><el-button type="primary" :loading="running" @click="readOnce">Read Once</el-button></div>
                </el-form>
              </el-card>

              <el-card shadow="never">
                <el-table :data="readResults" empty-text="Run a read">
                  <el-table-column prop="Target" label="Target" min-width="130"/>
                  <el-table-column prop="Address" label="Address" min-width="180"/>
                  <el-table-column prop="Type" label="Type" width="100"/>
                  <el-table-column prop="Raw" label="Raw" min-width="110"/>
                  <el-table-column prop="Decoded" label="Decoded" width="100"/>
                  <el-table-column prop="Engineering" label="Engineering" min-width="130"/>
                  <el-table-column prop="Timestamp" label="Timestamp" width="110"/>
                  <el-table-column prop="Latency" label="Latency" width="90"/>
                </el-table>
              </el-card>
            </template>
          </el-tab-pane>

          <el-tab-pane label="Write" name="write">
            <div class="panel-heading">
              <h2 class="app-section-title">Write Explorer</h2>
              <p class="app-caption">写入与读取分离，始终单次执行、二次确认并 readback。</p>
            </div>

            <el-alert type="warning" :closable="false" title="Write tests can change real equipment state."/>

            <el-alert v-if="!canReadWrite" type="info" :closable="false" title="Write Explorer requires a Device, Point or Manual Target." class="tool-card"/>

            <template v-else>
              <el-segmented v-model="writeMode" :options="[{label:'Defined Point',value:'defined'},{label:'Manual Address',value:'manual'}]" class="tool-switch"/>
              <el-card shadow="never" class="tool-card">
                <el-form label-position="top">
                  <div class="tool-fields">
                    <el-form-item v-if="writeMode==='defined'" label="Writable Point">
                      <el-select v-model="writePointId" filterable class="app-full-width">
                        <el-option v-for="point in writablePoints" :key="point.point_id" :label="point.point_id+' · '+pointAddress(point)" :value="point.point_id"/>
                      </el-select>
                    </el-form-item>
                    <el-form-item v-else label="Manual Address / Symbol"><el-input v-model="manualRead.symbol"/></el-form-item>
                    <el-form-item label="Write Value"><el-input v-model="writeValue"/></el-form-item>
                  </div>
                  <div class="tool-actions"><el-button type="danger" :loading="running" @click="writeOnce">Write Once & Readback</el-button></div>
                </el-form>
              </el-card>

              <el-card shadow="never">
                <el-table :data="writeResults" empty-text="No write executed">
                  <el-table-column prop="Target" label="Target"/>
                  <el-table-column prop="Point" label="Point"/>
                  <el-table-column prop="Value" label="Write"/>
                  <el-table-column prop="Result" label="Result"/>
                  <el-table-column prop="Readback" label="Readback"/>
                  <el-table-column prop="Latency" label="Latency"/>
                </el-table>
              </el-card>
            </template>
          </el-tab-pane>
        </el-tabs>
      </section>
    </div>
  </div>
</template>

<style scoped>
.diagnostics-layout{display:grid;grid-template-columns:minmax(18rem,20rem) minmax(0,1fr);gap:var(--app-space-4);align-items:start}.target-panel{position:sticky;top:var(--app-space-4)}.panel-heading p{margin:var(--app-space-1) 0 0}.target-mode,.tool-switch{margin-top:var(--app-space-4)}.target-form{margin-top:var(--app-space-4)}.target-form :deep(.el-form-item){margin-bottom:var(--app-space-3)}.target-summary{display:grid;gap:var(--app-space-2)}.target-summary>span{display:block}.target-summary>b{display:block}.explorer-panel{min-width:0}.explorer-tabs{margin-top:calc(var(--app-space-2) * -1)}.tool-card{margin:var(--app-space-3) 0}.tool-fields{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:0 var(--app-space-3);align-items:start}.tool-fields :deep(.el-form-item){margin-bottom:var(--app-space-3)}.tool-actions{display:flex;justify-content:flex-end}.field-note{display:flex;align-items:center;min-height:var(--app-control-height);color:var(--app-text-secondary)}.result-heading{display:flex;align-items:center;justify-content:space-between;gap:var(--app-space-3);margin:var(--app-space-4) 0 var(--app-space-2)}.pagination{display:flex;justify-content:flex-end;padding-top:var(--app-space-3)}
@media(max-width:1199px){.diagnostics-layout{grid-template-columns:1fr}.target-panel{position:static}.tool-fields{grid-template-columns:repeat(2,minmax(0,1fr))}}
@media(max-width:767px){.tool-fields{grid-template-columns:1fr}.tool-actions{justify-content:flex-start}.pagination{justify-content:center;overflow-x:auto}}
</style>