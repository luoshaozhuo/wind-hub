<script setup lang="ts">
import { computed, reactive, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { useViewport } from '../composables/useViewport'
import { effectiveConnection, pointsOfTable, protocolOfDevice, store, tableOfDevice, unitSymbol } from '../mock/data'
import { DATA_TYPES, MODBUS_REGISTER_TYPES, PROTOCOLS } from '../mock/types'
import type { DeviceInst, PointDef, Protocol } from '../mock/types'

type NetworkMode='single'|'subnet'
type NetworkTool='ping'|'ports'|'scan'
type ProtocolSource='device'|'group'|'table'|'manual'
type Operation='read'|'write'

const activeTab=ref<'network'|'protocol'>('network')
const { isMobile }=useViewport()
const running=ref(false)
const sleep=(ms:number)=>new Promise(resolve=>setTimeout(resolve,ms))

// Network diagnostics
const networkMode=ref<NetworkMode>('single')
const networkTool=ref<NetworkTool>('ping')
const singleHost=ref('192.168.151.1')
const subnet=reactive({ip:'192.168.151.1',mask:'255.255.255.0'})
const portProfile=ref<'common'|'custom'>('common')
const customPorts=ref('502, 2404, 48898')
const networkResults=ref<Array<Record<string,string|number>>>([])

watch(networkMode,mode=>{
  networkTool.value=mode==='single'?'ping':'scan'
  networkResults.value=[]
})

function maskToPrefix(mask:string){
  const octets=mask.split('.').map(Number)
  if(octets.length!==4||octets.some(x=>Number.isNaN(x)||x<0||x>255))return null
  const bits=octets.map(x=>x.toString(2).padStart(8,'0')).join('')
  if(!/^1*0*$/.test(bits))return null
  return bits.indexOf('0')===-1?32:bits.indexOf('0')
}
const subnetSummary=computed(()=>{
  const prefix=maskToPrefix(subnet.mask)
  const parts=subnet.ip.split('.').map(Number)
  if(prefix===null||parts.length!==4||parts.some(x=>Number.isNaN(x)||x<0||x>255))return null
  if(prefix!==24)return {network:`${subnet.ip}/${prefix}`,range:'Mock scan currently visualizes configured hosts matching the entered network.'}
  return {network:`${parts[0]}.${parts[1]}.${parts[2]}.0/24`,range:`${parts[0]}.${parts[1]}.${parts[2]}.1 – ${parts[0]}.${parts[1]}.${parts[2]}.254`}
})
function commonPorts(){return [{port:48898,service:'ADS'},{port:502,service:'Modbus TCP'},{port:2404,service:'IEC 104'}]}
function customPortList(){return customPorts.value.split(',').map(x=>Number(x.trim())).filter(x=>Number.isInteger(x)&&x>0&&x<=65535).map(port=>({port,service:'Custom'}))}
async function runNetwork(){
  if(running.value)return
  if(networkMode.value==='single'&&!singleHost.value.trim()){ElMessage.warning('Enter an IP address');return}
  running.value=true
  try{
    await sleep(350)
    if(networkMode.value==='single'&&networkTool.value==='ping'){
      networkResults.value=[{IP:singleHost.value,Reachable:'Yes',RTT:'8 ms',Loss:'0%',Time:new Date().toLocaleTimeString()}]
    }else if(networkMode.value==='single'&&networkTool.value==='ports'){
      const list=portProfile.value==='common'?commonPorts():customPortList()
      networkResults.value=list.map((item,index)=>({IP:singleHost.value,Port:item.port,Service:item.service,State:index===0?'Open':'Closed',Latency:index===0?'6 ms':'—'}))
    }else{
      const prefix=subnet.ip.split('.').slice(0,3).join('.')+'.'
      const rows=store.devices.filter(d=>d.host.startsWith(prefix)).map(d=>({
        IP:d.host,
        Ping:d.online?'Yes':'No',
        ADS:protocolOfDevice(d)==='ads'&&d.online?'Open':'—',
        Modbus:protocolOfDevice(d)==='modbus'&&d.online?'Open':'—',
        IEC104:protocolOfDevice(d)==='iec104'&&d.online?'Open':'—',
        Object:d.device_id,
      }))
      networkResults.value=rows.length?rows:[{IP:prefix+'1',Ping:'No hosts found',ADS:'—',Modbus:'—',IEC104:'—',Object:'—'}]
    }
  }finally{running.value=false}
}

// Protocol diagnostics
const protocol=ref<Protocol>('ads')
const source=ref<ProtocolSource>('device')
const operation=ref<Operation>('read')
const deviceId=ref('')
const groupId=ref('')
const groupDeviceId=ref('')
const tableId=ref('')
const tableDeviceId=ref('')
const pointId=ref('')
const writeValue=ref('')
const protocolResults=ref<Array<Record<string,string|number>>>([])
const manual=reactive({
  host:'192.168.151.1',
  port:48898,
  amsNetId:'192.168.151.1.1.1',
  adsPort:801,
  symbol:'.wind_speed',
  indexGroup:'',
  indexOffset:'',
  unitId:1,
  registerType:'holding',
  address:0,
  commonAddress:1,
  ioa:1,
  asduType:'',
  dataType:'float32',
})

const protocolDevices=computed(()=>store.devices.filter(d=>protocolOfDevice(d)===protocol.value))
const protocolGroups=computed(()=>store.deviceGroups.filter(g=>store.devices.some(d=>d.device_group===g.id&&protocolOfDevice(d)===protocol.value)))
const groupDevices=computed(()=>store.devices.filter(d=>d.device_group===groupId.value&&protocolOfDevice(d)===protocol.value))
const protocolTables=computed(()=>store.pointTables.filter(t=>!t.system&&t.protocol===protocol.value))
const tableDevices=computed(()=>store.devices.filter(d=>tableOfDevice(d)===tableId.value&&protocolOfDevice(d)===protocol.value))

const activeDevice=computed<DeviceInst|undefined>(()=>{
  if(source.value==='device')return store.devices.find(d=>d.device_id===deviceId.value)
  if(source.value==='group')return store.devices.find(d=>d.device_id===groupDeviceId.value)
  if(source.value==='table')return store.devices.find(d=>d.device_id===tableDeviceId.value)
  return undefined
})
const activeTableId=computed(()=>{
  if(source.value==='table')return tableId.value
  return activeDevice.value?tableOfDevice(activeDevice.value):''
})
const availablePoints=computed(()=>activeTableId.value?pointsOfTable(activeTableId.value):[])
const selectedPoint=computed(()=>availablePoints.value.find(p=>p.point_id===pointId.value))
const groupReadDevices=computed(()=>source.value==='group'?groupDevices.value.filter(d=>d.enabled):[])

function pointAddress(point:PointDef|undefined){
  if(!point)return '—'
  if(protocol.value==='ads')return point.address.symbol||[point.address.index_group,point.address.index_offset].filter(Boolean).join(' / ')||'—'
  if(protocol.value==='modbus')return `${point.address.type||'holding'} ${point.address.address??'—'}`
  return `IOA ${point.address.ioa??'—'}`
}
function connectionLabel(device:DeviceInst|undefined){
  if(!device)return '—'
  const conn=effectiveConnection(device)
  if(protocol.value==='ads')return `${device.host} · AMS ${String(device.extensions.target_net_id||device.host+'.1.1')} · ${String(conn.target_port||801)}`
  return `${device.host}:${String(conn.port||device.port|| (protocol.value==='modbus'?502:2404))}`
}

function resetProtocolSelections(){
  deviceId.value=protocolDevices.value[0]?.device_id||''
  groupId.value=protocolGroups.value[0]?.id||''
  groupDeviceId.value=groupDevices.value[0]?.device_id||''
  tableId.value=protocolTables.value[0]?.id||''
  tableDeviceId.value=tableDevices.value[0]?.device_id||''
  pointId.value=availablePoints.value[0]?.point_id||''
  manual.port=protocol.value==='ads'?48898:protocol.value==='modbus'?502:2404
  protocolResults.value=[]
}
watch(protocol,resetProtocolSelections,{immediate:true})
watch(source,()=>{pointId.value=availablePoints.value[0]?.point_id||'';protocolResults.value=[]})
watch(groupId,()=>{groupDeviceId.value=groupDevices.value[0]?.device_id||'';pointId.value=availablePoints.value[0]?.point_id||''})
watch(tableId,()=>{tableDeviceId.value=tableDevices.value[0]?.device_id||'';pointId.value=availablePoints.value[0]?.point_id||''})
watch([deviceId,groupDeviceId,tableDeviceId],()=>{pointId.value=availablePoints.value[0]?.point_id||''})

function manualAddress(){
  if(protocol.value==='ads')return manual.symbol.trim()||`${manual.indexGroup||'—'} / ${manual.indexOffset||'—'}`
  if(protocol.value==='modbus')return `${manual.registerType} ${manual.address}`
  return `IOA ${manual.ioa}`
}
function targetDevicesForRead(){
  if(source.value==='group')return groupReadDevices.value
  return activeDevice.value?[activeDevice.value]:[]
}
async function runRead(){
  if(running.value)return
  if(source.value!=='manual'&&!selectedPoint.value){ElMessage.warning('Select a point');return}
  if(source.value==='manual'&&!manual.host.trim()){ElMessage.warning('Enter target host');return}
  running.value=true
  try{
    await sleep(350)
    if(source.value==='manual'){
      protocolResults.value=[{
        Target:manual.host,
        Address:manualAddress(),
        Type:manual.dataType,
        Raw:'42 C8 00 00',
        Value:'100',
        Unit:'—',
        Result:'Success',
        Latency:'14 ms',
      }]
    }else{
      const targets=targetDevicesForRead()
      protocolResults.value=targets.map((d,index)=>({
        Target:d.device_id,
        Host:d.host,
        Point:selectedPoint.value?.point_id||'—',
        Address:pointAddress(selectedPoint.value),
        Raw:'42 C8 00 00',
        Value:String(8.4+index/10),
        Unit:selectedPoint.value?unitSymbol(selectedPoint.value.unit):'',
        Result:d.online?'Success':'Failed',
        Error:d.online?'—':'Connection unavailable',
        Latency:d.online?(12+index%9)+' ms':'—',
      }))
    }
  }finally{running.value=false}
}

async function runWrite(){
  if(running.value)return
  if(!writeValue.value.trim()){ElMessage.warning('Enter a write value');return}
  if(source.value==='group'&&!groupDeviceId.value){ElMessage.warning('Select one device in the group for write');return}
  if(source.value!=='manual'&&!selectedPoint.value){ElMessage.warning('Select a point');return}
  await ElMessageBox.confirm(
    'Send one diagnostic write and perform readback? Group writes always target only the selected device.',
    'Diagnostic Write',
    {type:'warning',confirmButtonText:'Write Once'},
  )
  running.value=true
  try{
    await sleep(380)
    protocolResults.value=[{
      Target:source.value==='manual'?manual.host:(activeDevice.value?.device_id||'—'),
      Address:source.value==='manual'?manualAddress():pointAddress(selectedPoint.value),
      Write:writeValue.value,
      Result:'Success',
      Readback:writeValue.value,
      Latency:'21 ms',
    }]
  }finally{running.value=false}
}
</script>

<template>
  <div class="diagnostics-page">
    <div class="head">
      <div><h1>Diagnostics</h1><p>网络连通性、IP 扫描以及协议级 Read / Write 调试。</p></div>
    </div>

    <el-tabs v-model="activeTab">
      <el-tab-pane label="Network" name="network">
        <div class="workspace-grid">
          <el-card shadow="never">
            <div class="panel-heading"><h2>Network Target</h2><p>单 IP 用于 Ping / Port Probe；IP + Mask 用于网段扫描。</p></div>
            <el-segmented v-model="networkMode" :options="[{label:'Single Host',value:'single'},{label:'Subnet',value:'subnet'}]" class="mode-switch"/>
            <el-form label-position="top">
              <template v-if="networkMode==='single'">
                <el-form-item label="IP Address"><el-input v-model="singleHost"/></el-form-item>
                <el-form-item label="Tool">
                  <el-radio-group v-model="networkTool">
                    <el-radio-button value="ping">Ping</el-radio-button>
                    <el-radio-button value="ports">Port Probe</el-radio-button>
                  </el-radio-group>
                </el-form-item>
                <template v-if="networkTool==='ports'">
                  <el-form-item label="Ports">
                    <el-radio-group v-model="portProfile">
                      <el-radio-button value="common">Common</el-radio-button>
                      <el-radio-button value="custom">Custom</el-radio-button>
                    </el-radio-group>
                  </el-form-item>
                  <el-form-item v-if="portProfile==='custom'" label="Custom Ports"><el-input v-model="customPorts" placeholder="502, 2404, 48898"/></el-form-item>
                </template>
              </template>
              <template v-else>
                <div class="two-col">
                  <el-form-item label="IP Address"><el-input v-model="subnet.ip"/></el-form-item>
                  <el-form-item label="Subnet Mask"><el-input v-model="subnet.mask"/></el-form-item>
                </div>
                <el-descriptions v-if="subnetSummary" :column="1" border size="small">
                  <el-descriptions-item label="Network">{{subnetSummary.network}}</el-descriptions-item>
                  <el-descriptions-item label="Host Range">{{subnetSummary.range}}</el-descriptions-item>
                </el-descriptions>
              </template>
              <div class="actions"><el-button type="primary" :loading="running" @click="runNetwork">{{networkMode==='subnet'?'Scan':'Run'}}</el-button></div>
            </el-form>
          </el-card>

          <el-card shadow="never">
            <div class="panel-heading"><h2>Results</h2><p>{{networkResults.length}} result rows</p></div>
            <el-table :data="networkResults" empty-text="Run a network diagnostic">
              <el-table-column v-for="key in Object.keys(networkResults[0]||{})" :key="key" :prop="key" :label="key" min-width="110"/>
            </el-table>
          </el-card>
        </div>
      </el-tab-pane>

      <el-tab-pane label="Protocol" name="protocol">
        <el-card shadow="never">
          <div class="protocol-toolbar">
            <div>
              <span>Protocol</span>
              <el-segmented v-model="protocol" :options="PROTOCOLS.map(value=>({label:value.toUpperCase(),value}))"/>
            </div>
            <div>
              <span>Source</span>
              <el-segmented v-model="source" :options="[
                {label:'Device',value:'device'},
                {label:'Device Group',value:'group'},
                {label:'Point Table',value:'table'},
                {label:'Manual',value:'manual'},
              ]"/>
            </div>
            <div>
              <span>Operation</span>
              <el-segmented v-model="operation" :options="[{label:'Read',value:'read'},{label:'Write',value:'write'}]"/>
            </div>
          </div>
        </el-card>

        <div class="protocol-layout">
          <el-card shadow="never">
            <div class="panel-heading"><h2>Target & Address</h2><p>优先复用已配置对象；Manual 模式可完全自由输入。</p></div>
            <el-form label-position="top">
              <template v-if="source==='device'">
                <el-form-item label="Device">
                  <el-select v-model="deviceId" filterable class="app-full-width">
                    <el-option v-for="d in protocolDevices" :key="d.device_id" :value="d.device_id" :label="d.device_id+' · '+d.host"/>
                  </el-select>
                </el-form-item>
              </template>

              <template v-else-if="source==='group'">
                <div class="two-col">
                  <el-form-item label="Device Group">
                    <el-select v-model="groupId" class="app-full-width"><el-option v-for="g in protocolGroups" :key="g.id" :value="g.id" :label="g.id"/></el-select>
                  </el-form-item>
                  <el-form-item label="Write Target Device">
                    <el-select v-model="groupDeviceId" filterable class="app-full-width">
                      <el-option v-for="d in groupDevices" :key="d.device_id" :value="d.device_id" :label="d.device_id+' · '+d.host"/>
                    </el-select>
                  </el-form-item>
                </div>
                <el-alert type="info" :closable="false" title="Read tests all enabled devices in the group; Write targets only the selected device."/>
              </template>

              <template v-else-if="source==='table'">
                <div class="two-col">
                  <el-form-item label="Point Table">
                    <el-select v-model="tableId" class="app-full-width"><el-option v-for="t in protocolTables" :key="t.id" :value="t.id" :label="t.id"/></el-select>
                  </el-form-item>
                  <el-form-item label="Compatible Device">
                    <el-select v-model="tableDeviceId" filterable class="app-full-width"><el-option v-for="d in tableDevices" :key="d.device_id" :value="d.device_id" :label="d.device_id+' · '+d.host"/></el-select>
                  </el-form-item>
                </div>
              </template>

              <template v-if="source!=='manual'">
                <el-form-item label="Point">
                  <el-select v-model="pointId" filterable class="app-full-width">
                    <el-option v-for="p in availablePoints" :key="p.point_id" :value="p.point_id" :label="p.point_id+' · '+pointAddress(p)"/>
                  </el-select>
                </el-form-item>
                <el-descriptions :column="1" border size="small">
                  <el-descriptions-item label="Target">{{source==='group'&&operation==='read'?groupDevices.length+' devices':connectionLabel(activeDevice)}}</el-descriptions-item>
                  <el-descriptions-item label="Address">{{pointAddress(selectedPoint)}}</el-descriptions-item>
                  <el-descriptions-item label="Data Type">{{selectedPoint?.data_type||'—'}}</el-descriptions-item>
                  <el-descriptions-item label="Scale / Offset">{{selectedPoint ? selectedPoint.scale+' / '+selectedPoint.offset : '—'}}</el-descriptions-item>
                  <el-descriptions-item label="Unit">{{selectedPoint ? unitSymbol(selectedPoint.unit)||selectedPoint.unit : '—'}}</el-descriptions-item>
                </el-descriptions>
              </template>

              <template v-else>
                <div class="two-col">
                  <el-form-item label="Host / IP"><el-input v-model="manual.host"/></el-form-item>
                  <el-form-item label="TCP Port"><el-input-number v-model="manual.port" :min="1" :max="65535" class="app-full-width"/></el-form-item>
                </div>
                <template v-if="protocol==='ads'">
                  <div class="two-col">
                    <el-form-item label="AMS Net ID"><el-input v-model="manual.amsNetId"/></el-form-item>
                    <el-form-item label="ADS Port"><el-input-number v-model="manual.adsPort" :min="1" class="app-full-width"/></el-form-item>
                  </div>
                  <el-form-item label="Symbol"><el-input v-model="manual.symbol" placeholder=".wind_speed"/></el-form-item>
                  <div class="two-col">
                    <el-form-item label="Index Group"><el-input v-model="manual.indexGroup"/></el-form-item>
                    <el-form-item label="Index Offset"><el-input v-model="manual.indexOffset"/></el-form-item>
                  </div>
                </template>
                <template v-else-if="protocol==='modbus'">
                  <div class="two-col">
                    <el-form-item label="Unit ID"><el-input-number v-model="manual.unitId" :min="0" :max="255" class="app-full-width"/></el-form-item>
                    <el-form-item label="Register Type"><el-select v-model="manual.registerType" class="app-full-width"><el-option v-for="item in MODBUS_REGISTER_TYPES" :key="item" :value="item" :label="item"/></el-select></el-form-item>
                    <el-form-item label="0-based Address"><el-input-number v-model="manual.address" :min="0" class="app-full-width"/></el-form-item>
                  </div>
                </template>
                <template v-else>
                  <div class="two-col">
                    <el-form-item label="Common Address"><el-input-number v-model="manual.commonAddress" :min="1" class="app-full-width"/></el-form-item>
                    <el-form-item label="IOA"><el-input-number v-model="manual.ioa" :min="0" class="app-full-width"/></el-form-item>
                    <el-form-item label="ASDU Type"><el-input v-model="manual.asduType"/></el-form-item>
                  </div>
                </template>
                <el-form-item label="Data Type"><el-select v-model="manual.dataType" class="app-full-width"><el-option v-for="item in DATA_TYPES" :key="item" :value="item" :label="item"/></el-select></el-form-item>
              </template>

              <el-form-item v-if="operation==='write'" label="Write Value"><el-input v-model="writeValue"/></el-form-item>
              <div class="actions">
                <el-button v-if="operation==='read'" type="primary" :loading="running" @click="runRead">Read Once</el-button>
                <el-button v-else type="danger" :loading="running" @click="runWrite">Write Once & Readback</el-button>
              </div>
            </el-form>
          </el-card>

          <el-card shadow="never">
            <div class="panel-heading"><h2>Results</h2><p>{{protocolResults.length}} result rows</p></div>
            <el-table :data="protocolResults" empty-text="Run a protocol test">
              <el-table-column v-for="key in Object.keys(protocolResults[0]||{})" :key="key" :prop="key" :label="key" min-width="110"/>
            </el-table>
          </el-card>
        </div>
      </el-tab-pane>
    </el-tabs>
  </div>
</template>

<style scoped>
.workspace-grid,.protocol-layout{display:grid;grid-template-columns:minmax(320px,.7fr) minmax(0,1.3fr);gap:var(--app-space-4);align-items:start}
.panel-heading{margin-bottom:var(--app-space-4)}.panel-heading h2{margin:0;font-size:var(--app-font-section-title)}.panel-heading p{margin:var(--app-space-1) 0 0;color:var(--app-text-muted);font-size:var(--app-font-body)}
.mode-switch{margin-bottom:var(--app-space-4)}.two-col{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:0 var(--app-space-3)}
.actions{display:flex;justify-content:flex-end;margin-top:var(--app-space-3)}
.protocol-toolbar{display:flex;align-items:flex-end;gap:var(--app-space-6);flex-wrap:wrap}
.protocol-toolbar>div{display:grid;gap:var(--app-space-2)}.protocol-toolbar span{color:var(--app-text-secondary);font-size:var(--app-font-label)}
.protocol-layout{margin-top:var(--app-space-4)}
@media(max-width:1199px){.workspace-grid,.protocol-layout{grid-template-columns:1fr}}
@media(max-width:767px){.two-col{grid-template-columns:1fr}.protocol-toolbar{align-items:stretch;flex-direction:column}.actions{justify-content:flex-start}}
</style>
