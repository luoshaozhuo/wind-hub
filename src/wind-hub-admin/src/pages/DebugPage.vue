<script setup lang="ts">
import { computed, reactive, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { useViewport } from '../composables/useViewport'
import { effectiveConnection, pointsOfTable, protocolOfDevice, store, tableOfDevice, unitSymbol } from '../api/data'
import {
  manualProtocolReadRow,
  manualProtocolWriteRow,
  pingHost,
  probePorts,
  runProtocolRead,
  runProtocolWrite,
  scanSubnet,
} from '../api/service'
import { DATA_TYPES, MODBUS_REGISTER_TYPES, PROTOCOLS } from '../api/types'
import type { DeviceInst, PointDef, Protocol } from '../api/types'

type NetworkMode='single'|'subnet'
type NetworkTool='ping'|'ports'
type ProtocolSource='device'|'group'|'table'|'manual'
type Operation='read'|'write'

const activeTab=ref<'network'|'protocol'>('network')
const { isMobile }=useViewport()
const running=ref(false)

// Keep the admin aligned with wind_hub default diagnostic port mapping.
const DEFAULT_PORT_MAPPING:Record<number,string>={
  502:'modbus',
  2404:'iec104',
  48898:'ads',
  4840:'opc-ua',
  44818:'ethernet-ip',
  80:'http',
  443:'https',
  22:'ssh',
  23:'telnet',
}
const DEFAULT_PORTS=Object.keys(DEFAULT_PORT_MAPPING).map(Number).join(', ')

// Network diagnostics
const networkMode=ref<NetworkMode>('single')
const networkTool=ref<NetworkTool>('ping')
const singleHost=ref('192.168.151.1')
const subnet=reactive({ip:'192.168.151.1',mask:'255.255.255.0'})
const portsInput=ref(DEFAULT_PORTS)
const networkResults=ref<Array<Record<string,string|number>>>([])
const networkPage=ref(1)
const networkPageSize=ref(50)
const scanProgress=ref(0)
const scanTotal=ref(0)
const pagedNetworkResults=computed(()=>{
  const start=(networkPage.value-1)*networkPageSize.value
  return networkResults.value.slice(start,start+networkPageSize.value)
})
watch([networkResults,networkPageSize],()=>{
  const maxPage=Math.max(1,Math.ceil(networkResults.value.length/networkPageSize.value))
  if(networkPage.value>maxPage)networkPage.value=maxPage
})

watch(networkMode,mode=>{
  networkTool.value='ping'
  networkResults.value=[]
  scanProgress.value=0
  scanTotal.value=0
  if(mode==='subnet')networkTool.value='ping'
})

function resetDefaultPorts(){
  portsInput.value=DEFAULT_PORTS
}
function parsePorts(){
  const values=[...new Set(portsInput.value.split(',').map(x=>Number(x.trim())).filter(x=>Number.isInteger(x)&&x>=1&&x<=65535))]
  return values
}
function ipv4ToInt(ip:string){
  const parts=ip.trim().split('.').map(Number)
  if(parts.length!==4||parts.some(x=>!Number.isInteger(x)||x<0||x>255))return null
  return ((((parts[0]<<24)>>>0)+((parts[1]<<16)>>>0)+((parts[2]<<8)>>>0)+parts[3])>>>0)
}
function intToIpv4(value:number){
  const v=value>>>0
  return [v>>>24,(v>>>16)&255,(v>>>8)&255,v&255].join('.')
}
function maskToPrefix(mask:string){
  const value=ipv4ToInt(mask)
  if(value===null)return null
  const bits=(value>>>0).toString(2).padStart(32,'0')
  if(!/^1*0*$/.test(bits))return null
  return bits.indexOf('0')===-1?32:bits.indexOf('0')
}
const subnetInfo=computed(()=>{
  const ip=ipv4ToInt(subnet.ip)
  const prefix=maskToPrefix(subnet.mask)
  if(ip===null||prefix===null)return null
  const mask=prefix===0?0:((0xffffffff<<(32-prefix))>>>0)
  const network=(ip&mask)>>>0
  const broadcast=(network|(~mask>>>0))>>>0
  let first=network
  let last=broadcast
  if(prefix<=30){first=(network+1)>>>0;last=(broadcast-1)>>>0}
  const total=last>=first?last-first+1:0
  return {
    prefix,
    network:`${intToIpv4(network)}/${prefix}`,
    first,
    last,
    total,
    range:total?`${intToIpv4(first)} – ${intToIpv4(last)}`:'—',
  }
})
const scanPercent=computed(()=>scanTotal.value?Math.round(scanProgress.value/scanTotal.value*100):0)

async function runNetwork(){
  if(running.value)return
  networkResults.value=[]
  networkPage.value=1

  if(networkMode.value==='single'){
    if(ipv4ToInt(singleHost.value)===null){ElMessage.warning('Enter a valid IPv4 address');return}
    if(networkTool.value==='ports'&&!parsePorts().length){ElMessage.warning('Enter at least one valid port');return}
    running.value=true
    try{
      // Ping / Port Probe 结果与 Devices Verify 一致（§17.1/§17.2）：由 backend service 派生
      networkResults.value=networkTool.value==='ping'
        ? [await pingHost(singleHost.value)]
        : await probePorts(singleHost.value,parsePorts())
    }finally{running.value=false}
    return
  }

  const info=subnetInfo.value
  if(!info){ElMessage.warning('Enter a valid IP address and subnet mask');return}
  if(info.total>4096){ElMessage.warning('Subnet is too large for this diagnostic scan; use a mask with 4096 hosts or fewer');return}

  running.value=true
  scanProgress.value=0
  scanTotal.value=info.total
  try{
    networkResults.value=await scanSubnet(
      info.network,
      parsePorts().length?parsePorts():[502,2404,48898],
      (completed,total)=>{scanProgress.value=completed;scanTotal.value=total},
    )
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
  return `${device.host}:${String(conn.port||device.port||(protocol.value==='modbus'?502:2404))}`
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
// Protocol Read：来自与 Device Data 相同的 mock 数据源（§17.3），失败路径与场景一致。
async function runRead(){
  if(running.value)return
  if(source.value!=='manual'&&!selectedPoint.value){ElMessage.warning('Select a point');return}
  if(source.value==='manual'&&!manual.host.trim()){ElMessage.warning('Enter target host');return}
  running.value=true
  try{
    if(source.value==='manual'){
      protocolResults.value=[await manualProtocolReadRow(manual.host.trim(),manualAddress(),manual.dataType)]
    }else{
      protocolResults.value=await runProtocolRead(
        targetDevicesForRead(),
        selectedPoint.value as PointDef,
        pointAddress(selectedPoint.value),
      )
    }
  }finally{running.value=false}
}
// Protocol Write：与 Device Command 共享底层写操作（§17.4），成功后 Data / Trend / Logs 联动。
async function runWrite(){
  if(running.value)return
  if(!writeValue.value.trim()){ElMessage.warning('Enter a write value');return}
  if(source.value==='group'&&!groupDeviceId.value){ElMessage.warning('Select one device in the group for write');return}
  if(source.value!=='manual'&&!selectedPoint.value){ElMessage.warning('Select a point');return}
  if(source.value!=='manual'&&!activeDevice.value){ElMessage.warning('Select a device');return}
  try{
    await ElMessageBox.confirm(
      'Send one diagnostic write and perform readback? Group writes always target only the selected device.',
      'Diagnostic Write',
      {type:'warning',confirmButtonText:'Write Once'},
    )
  }catch{return}
  running.value=true
  try{
    if(source.value==='manual'){
      protocolResults.value=[await manualProtocolWriteRow(manual.host.trim(),manualAddress(),writeValue.value.trim())]
    }else{
      protocolResults.value=[await runProtocolWrite(
        activeDevice.value as DeviceInst,
        selectedPoint.value as PointDef,
        writeValue.value.trim(),
        pointAddress(selectedPoint.value),
      )]
    }
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

            <el-form label-position="top">
              <el-form-item label="Target Scope">
                <el-radio-group v-model="networkMode">
                  <el-radio-button value="single">Single Host</el-radio-button>
                  <el-radio-button value="subnet">Subnet</el-radio-button>
                </el-radio-group>
              </el-form-item>

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
                    <div class="ports-row">
                      <el-input v-model="portsInput" placeholder="502, 2404, 48898"/>
                      <el-button @click="resetDefaultPorts">Reset Defaults</el-button>
                    </div>
                    <div class="field-note">默认端口与 Wind Hub ports 配置映射保持一致，可直接临时修改。</div>
                  </el-form-item>
                </template>
              </template>

              <template v-else>
                <div class="two-col">
                  <el-form-item label="IP Address"><el-input v-model="subnet.ip"/></el-form-item>
                  <el-form-item label="Subnet Mask"><el-input v-model="subnet.mask"/></el-form-item>
                </div>
                <el-descriptions v-if="subnetInfo" :column="1" border size="small">
                  <el-descriptions-item label="Network">{{subnetInfo.network}}</el-descriptions-item>
                  <el-descriptions-item label="Host Range">{{subnetInfo.range}}</el-descriptions-item>
                  <el-descriptions-item label="Hosts">{{subnetInfo.total}}</el-descriptions-item>
                </el-descriptions>
                <div v-if="running&&scanTotal" class="scan-progress">
                  <el-progress :percentage="scanPercent"/>
                  <span>{{scanProgress}} / {{scanTotal}} hosts</span>
                </div>
              </template>

              <div class="actions">
                <el-button type="primary" :loading="running" @click="runNetwork">
                  {{networkMode==='subnet'?'Scan':networkTool==='ports'?'Probe':'Ping'}}
                </el-button>
              </div>
            </el-form>
          </el-card>

          <el-card shadow="never">
            <div class="panel-heading"><h2>Results</h2><p>{{networkResults.length}} result rows</p></div>
            <el-table :data="pagedNetworkResults" empty-text="Run a network diagnostic" height="var(--app-table-viewport-height)">
              <el-table-column v-for="key in Object.keys(networkResults[0]||{})" :key="key" :prop="key" :label="key" min-width="110"/>
            </el-table>
            <div v-if="networkResults.length>networkPageSize" class="pagination">
              <el-pagination
                v-model:current-page="networkPage"
                v-model:page-size="networkPageSize"
                :page-sizes="[50,100,200]"
                :total="networkResults.length"
                :layout="isMobile ? 'prev, pager, next' : 'total, sizes, prev, pager, next'"
              />
            </div>
          </el-card>
        </div>
      </el-tab-pane>

      <el-tab-pane label="Protocol" name="protocol">
        <el-card shadow="never">
          <div class="protocol-toolbar">
            <div><span>Protocol</span><el-segmented v-model="protocol" :options="PROTOCOLS.map(value=>({label:value.toUpperCase(),value}))"/></div>
            <div><span>Source</span><el-segmented v-model="source" :options="[
              {label:'Device',value:'device'},{label:'Device Group',value:'group'},{label:'Point Table',value:'table'},{label:'Manual',value:'manual'},
            ]"/></div>
            <div><span>Operation</span><el-segmented v-model="operation" :options="[{label:'Read',value:'read'},{label:'Write',value:'write'}]"/></div>
          </div>
        </el-card>

        <div class="protocol-layout">
          <el-card shadow="never">
            <div class="panel-heading"><h2>Target & Address</h2><p>优先复用已配置对象；Manual 模式可完全自由输入。</p></div>
            <el-form label-position="top">
              <template v-if="source==='device'">
                <el-form-item label="Device"><el-select v-model="deviceId" filterable class="app-full-width"><el-option v-for="d in protocolDevices" :key="d.device_id" :value="d.device_id" :label="d.device_id+' · '+d.host"/></el-select></el-form-item>
              </template>

              <template v-else-if="source==='group'">
                <div class="two-col">
                  <el-form-item label="Device Group"><el-select v-model="groupId" class="app-full-width"><el-option v-for="g in protocolGroups" :key="g.id" :value="g.id" :label="g.id"/></el-select></el-form-item>
                  <el-form-item label="Write Target Device"><el-select v-model="groupDeviceId" filterable class="app-full-width"><el-option v-for="d in groupDevices" :key="d.device_id" :value="d.device_id" :label="d.device_id+' · '+d.host"/></el-select></el-form-item>
                </div>
                <el-alert type="info" :closable="false" title="Read tests all enabled devices in the group; Write targets only the selected device."/>
              </template>

              <template v-else-if="source==='table'">
                <div class="two-col">
                  <el-form-item label="Point Table"><el-select v-model="tableId" class="app-full-width"><el-option v-for="t in protocolTables" :key="t.id" :value="t.id" :label="t.id"/></el-select></el-form-item>
                  <el-form-item label="Compatible Device"><el-select v-model="tableDeviceId" filterable class="app-full-width"><el-option v-for="d in tableDevices" :key="d.device_id" :value="d.device_id" :label="d.device_id+' · '+d.host"/></el-select></el-form-item>
                </div>
              </template>

              <template v-if="source!=='manual'">
                <el-form-item label="Point"><el-select v-model="pointId" filterable class="app-full-width"><el-option v-for="p in availablePoints" :key="p.point_id" :value="p.point_id" :label="p.point_id+' · '+pointAddress(p)"/></el-select></el-form-item>
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
.workspace-grid,.protocol-layout{display:grid;grid-template-columns:minmax(var(--app-layout-pane-min-width),.7fr) minmax(0,1.3fr);gap:var(--app-space-4);align-items:start}
.panel-heading{margin-bottom:var(--app-space-4)}.panel-heading h2{margin:0;font-size:var(--app-font-section-title)}.panel-heading p{margin:var(--app-space-1) 0 0;color:var(--app-text-muted);font-size:var(--app-font-body)}
.two-col{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:0 var(--app-space-3)}.ports-row{display:flex;align-items:center;gap:var(--app-space-2)}.field-note{margin-top:var(--app-space-1);color:var(--app-text-muted);font-size:var(--app-font-label)}
.scan-progress{display:grid;gap:var(--app-space-1);margin-top:var(--app-space-3)}.scan-progress span{color:var(--app-text-muted);font-size:var(--app-font-label);text-align:right}
.actions{display:flex;justify-content:flex-end;margin-top:var(--app-space-3)}
.protocol-toolbar{display:flex;align-items:flex-end;gap:var(--app-space-6);flex-wrap:wrap}.protocol-toolbar>div{display:grid;gap:var(--app-space-2)}.protocol-toolbar span{color:var(--app-text-secondary);font-size:var(--app-font-label)}
.protocol-layout{margin-top:var(--app-space-4)}
@media(max-width:1199px){.workspace-grid,.protocol-layout{grid-template-columns:1fr}}
@media(max-width:767px){.two-col{grid-template-columns:1fr}.ports-row{align-items:stretch;flex-direction:column}.protocol-toolbar{align-items:stretch;flex-direction:column}.actions{justify-content:flex-start}}
</style>
