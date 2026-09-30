<script setup lang="ts">
import * as echarts from 'echarts'
import { InfoFilled } from '@element-plus/icons-vue'
import { computed, nextTick, onBeforeUnmount, ref, watch } from 'vue'
import { useViewport } from '../composables/useViewport'
import { protocolOfDevice, store } from '../mock/data'

type ChannelState='Healthy'|'Degraded'|'Interrupted'|'Disabled'
type WindowRange='1 h'|'24 h'|'7 d'
type DrawerKind='channel-metric'|'event'|'data-metric'|'dimension'

interface ChannelRow {
  object:string
  source:'Acquisition'|'Delivery'
  protocol:string
  state:ChannelState
  target:string
  last:string
  latency:string
  timeouts:number
  reconnects:number
  issue:string
}
interface CommunicationEvent {
  id:number
  time:string
  object:string
  protocol:string
  event:string
  state:'Active'|'Recovered'
  error:string
  duration:string
  target:string
}
interface ProblemObject {
  object:string
  kind:'Task'|'Device'|'Point'|'Sink'
  metric:string
  expected:string
  state:'Warning'|'Fault'
  error:string
}
interface QualityDrawer {
  kind:DrawerKind
  title:string
  subtitle:string
  distribution:Array<{name:string;value:number}>
  tasks:Array<Record<string,string|number>>
  devices:Array<Record<string,string|number>>
  errors:Array<Record<string,string|number>>
  problems:ProblemObject[]
}

const activeTab=ref<'channel'|'data'>('channel')
const channelWindow=ref<WindowRange>('24 h')
const dataWindow=ref<WindowRange>('24 h')
const { isMobile, isTablet }=useViewport()

const checking=ref(false)
const autoCheck=ref(false)
const checkInterval=ref(30)
const lastChecked=ref('Never')
let autoTimer:number|undefined

const drawerOpen=ref(false)
const drawer=ref<QualityDrawer|null>(null)
const chartEl=ref<HTMLElement|null>(null)
let chart:echarts.ECharts|null=null

function nowText(){
  const d=new Date()
  const pad=(n:number)=>String(n).padStart(2,'0')
  return `${d.getFullYear()}-${pad(d.getMonth()+1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`
}
async function runCheck(){
  if(checking.value)return
  checking.value=true
  try{
    await new Promise(resolve=>setTimeout(resolve,700))
    lastChecked.value=nowText()
  }finally{checking.value=false}
}
function syncAutoCheck(){
  if(autoTimer)window.clearInterval(autoTimer)
  autoTimer=undefined
  if(autoCheck.value){
    autoTimer=window.setInterval(()=>void runCheck(),checkInterval.value*1000)
  }
}
watch([autoCheck,checkInterval],syncAutoCheck)

const acquisitionChannels=computed<ChannelRow[]>(()=>store.devices.filter(d=>d.enabled).map((d,index)=>{
  const interrupted=!d.online
  const degraded=!interrupted&&(index%13===4||index%17===9)
  const state:ChannelState=interrupted?'Interrupted':degraded?'Degraded':'Healthy'
  const latency=interrupted?0:12+(index*11)%210
  return {
    object:d.device_id,
    source:'Acquisition',
    protocol:protocolOfDevice(d).toUpperCase(),
    state,
    target:d.host,
    last:interrupted?'2026-09-30 18:36:14':`2026-09-30 18:${String(47-index%10).padStart(2,'0')}:${String((index*7)%60).padStart(2,'0')}`,
    latency:interrupted?'—':latency+' ms',
    timeouts:interrupted?8:degraded?2:0,
    reconnects:interrupted?3:degraded?1:0,
    issue:interrupted?'Protocol session unavailable':degraded?'Latency / reconnect degradation':'—',
  }
}))

const deliveryChannels=computed<ChannelRow[]>(()=>store.sinks.map(s=>{
  const state:ChannelState=!s.enabled?'Disabled':s.runtime_state==='failed'?'Interrupted':s.runtime_state==='healthy'?'Healthy':'Degraded'
  return {
    object:s.name,
    source:'Delivery',
    protocol:s.type==='db'?'POSTGRESQL':s.type.toUpperCase(),
    state,
    target:String(s.params.bootstrap_servers||s.params.dsn||s.params.path||'configured'),
    last:s.last_write_at||'Never',
    latency:s.enabled?(s.latency_ms?s.latency_ms+' ms':'—'):'—',
    timeouts:s.runtime_state==='failed'?3:0,
    reconnects:s.runtime_state==='warning'?1:0,
    issue:s.error||(!s.enabled?'Disabled':'—'),
  }
}))

const rangeFactor=(range:WindowRange)=>range==='1 h'?0.25:range==='7 d'?5:1
const channelFactor=computed(()=>rangeFactor(channelWindow.value))
const dataFactor=computed(()=>rangeFactor(dataWindow.value))

const channelSummary=computed(()=>{
  const all=[...acquisitionChannels.value,...deliveryChannels.value]
  return [
    {key:'interrupted',label:'Interrupted',value:all.filter(x=>x.state==='Interrupted').length,tone:'danger'},
    {key:'degraded',label:'Degraded',value:all.filter(x=>x.state==='Degraded').length,tone:'warning'},
    {key:'timeouts',label:'Timeouts',value:Math.round(all.reduce((sum,x)=>sum+x.timeouts,0)*channelFactor.value),tone:'warning'},
    {key:'reconnects',label:'Reconnects',value:Math.round(all.reduce((sum,x)=>sum+x.reconnects,0)*channelFactor.value),tone:'warning'},
  ]
})

const communicationEvents=computed<CommunicationEvent[]>(()=>{
  const rows:CommunicationEvent[]=[]
  acquisitionChannels.value.forEach((row,index)=>{
    if(row.state==='Healthy')return
    rows.push({
      id:index+1,
      time:`2026-09-30 18:${String(42-index%30).padStart(2,'0')}:${String((index*7)%60).padStart(2,'0')}`,
      object:row.object,
      protocol:row.protocol,
      event:row.state==='Interrupted'?'Connection lost':'Channel degraded',
      state:row.state==='Interrupted'?'Active':'Recovered',
      error:row.state==='Interrupted'?'Session timeout / no response':row.issue,
      duration:row.state==='Interrupted'?'12m 18s':'43s',
      target:row.target,
    })
  })
  rows.push({
    id:1001,time:'2026-09-30 18:31:22',object:'file_archive',protocol:'FILE',
    event:'Write retry',state:'Recovered',error:'Transient filesystem latency',duration:'2.1s',target:'/var/tmp/wind-hub/archive.jsonl',
  })
  return rows
})

function channelMetricDetail(key:string){
  const base=[
    {name:'00–04',value:0},
    {name:'04–08',value:1},
    {name:'08–12',value:key==='timeouts'?4:1},
    {name:'12–16',value:key==='reconnects'?3:1},
    {name:'16–20',value:key==='interrupted'?2:5},
    {name:'20–24',value:1},
  ]
  const devices=[
    {Device:'wtg-041',Protocol:'ADS',Events:key==='timeouts'?8:3,Duration:'12m 18s',LastEvent:'2026-09-30 18:36:14',Error:'ADS session timeout'},
    {Device:'wtg-017',Protocol:'ADS',Events:key==='reconnects'?2:1,Duration:'48s',LastEvent:'2026-09-30 15:22:08',Error:'Repeated reconnect / high latency'},
  ]
  const errors=[
    {Time:'2026-09-30 18:36:14',Device:'wtg-041',Event:'Disconnected',Error:'ADS session timeout'},
    {Time:'2026-09-30 15:22:08',Device:'wtg-017',Event:'Channel degraded',Error:'Read latency above threshold'},
  ]
  return {distribution:base,devices,errors}
}
function openChannelMetric(metric:(typeof channelSummary.value)[number]){
  const detail=channelMetricDetail(metric.key)
  drawer.value={
    kind:'channel-metric',
    title:metric.label,
    subtitle:`${channelWindow.value} · ${metric.value}`,
    distribution:detail.distribution,
    tasks:[],
    devices:detail.devices,
    errors:detail.errors,
    problems:[],
  }
  drawerOpen.value=true
  nextTick(renderChart)
}
function openEvent(row:CommunicationEvent){
  drawer.value={
    kind:'event',
    title:row.event,
    subtitle:row.object+' · '+row.protocol,
    distribution:[],
    tasks:[],
    devices:[{Object:row.object,Target:row.target,State:row.state,Duration:row.duration}],
    errors:[{Time:row.time,Object:row.object,Error:row.error}],
    problems:[],
  }
  drawerOpen.value=true
}

const dataMetrics=computed(()=>[
  {key:'stale',label:'Stale Tasks',value:1,hint:'current',tone:'danger'},
  {key:'missing',label:'Missing Cycles',value:Math.max(1,Math.round(13*dataFactor.value)),hint:dataWindow.value,tone:'warning'},
  {key:'reads',label:'Point Read Failures',value:Math.max(1,Math.round(6*dataFactor.value)),hint:dataWindow.value,tone:'warning'},
  {key:'dropped',label:'Dropped Points',value:0,hint:dataWindow.value,tone:'normal'},
] as const)

const dimensionInfo:Record<string,{definition:string;method:string;threshold:string}>={
  continuity:{
    definition:'判断采集序列是否连续，重点识别中断和连续缺失周期。',
    method:'按 Task 的计划采样周期与实际样本时间序列计算 gap。',
    threshold:'Normal：无连续缺失；Warning：出现短时 gap；Fault：连续缺失超过 3 个采样周期。',
  },
  timeliness:{
    definition:'判断采集数据是否按 Task 周期及时到达。',
    method:'以数据年龄 / Task 采样周期作为 freshness ratio。',
    threshold:'Normal ≤ 1.5× period；Warning ≤ 3× period；Fault > 3× period。',
  },
  completeness:{
    definition:'判断计划采样中实际收到的数据是否完整。',
    method:'统计 missing cycles 与 point read failures。',
    threshold:'Normal ≥ 99.9%；Warning 99%–99.9%；Fault < 99%。',
  },
  validity:{
    definition:'判断接收值是否能够按 Point 定义正确解码和解释。',
    method:'统计 decode、payload、timestamp 与 ordering 异常。',
    threshold:'Normal：无异常；Warning：少量可恢复异常；Fault：持续无法解码。',
  },
  delivery:{
    definition:'判断采集结果向 Sink 交付时是否丢失或积压。',
    method:'统计 dropped points、queue backlog 与 write failures。',
    threshold:'Normal：无丢弃且 backlog 正常；Warning：持续积压；Fault：发生丢弃或持续写失败。',
  },
}

const dimensionRows=computed(()=>[
  {key:'continuity',dimension:'Continuity',status:'Fault',metric:'1 stale task',detail:'Longest gap 12m 18s'},
  {key:'timeliness',dimension:'Timeliness',status:'Warning',metric:'P95 freshness 1.9 × period',detail:'7 devices degraded'},
  {key:'completeness',dimension:'Completeness',status:'Warning',metric:dataMetrics.value[1].value+' missing cycles',detail:'6 point reads failed'},
  {key:'validity',dimension:'Validity',status:'Warning',metric:'2 decode errors',detail:'No timestamp/order error'},
  {key:'delivery',dimension:'Delivery Integrity',status:'Normal',metric:'0 dropped points',detail:'File backlog 2'},
])

const activeIssues=computed(()=>[
  {level:'Fault',object:'turbine-ads-all',kind:'Task',dimension:'Continuity',issue:'No fresh samples',duration:'12m 18s',error:'WTG-041 ADS session unavailable'},
  {level:'Warning',object:'wtg-017',kind:'Device',dimension:'Timeliness',issue:'Freshness above threshold',duration:'4m 05s',error:'Repeated high read latency'},
  {level:'Warning',object:'wtg-003',kind:'Device',dimension:'Completeness',issue:'Missing acquisition cycles',duration:'2m 41s',error:'Modbus read timeout'},
])

function metricDetail(key:string){
  if(key==='stale')return {
    tasks:[{Task:'turbine-ads-all',Expected:'1 s',LastSample:'2026-09-30 18:36:14',State:'Stale'}],
    devices:[{Device:'wtg-041',Protocol:'ADS',LastSuccess:'2026-09-30 18:36:14',State:'Fault'}],
    errors:[{Time:'2026-09-30 18:36:14',Object:'wtg-041',Error:'ADS session unavailable'},{Time:'2026-09-30 18:36:12',Object:'turbine-ads-all',Error:'No fresh samples'}],
  }
  if(key==='missing')return {
    tasks:[{Task:'turbine-modbus-all',Expected:'1 s',LastSample:'2026-09-30 18:44:58',Missing:8,State:'Warning'},{Task:'pcs-fast',Expected:'1 s',LastSample:'2026-09-30 18:45:02',Missing:5,State:'Warning'}],
    devices:[{Device:'wtg-003',LastSuccess:'2026-09-30 18:40:29',Missing:5,Error:'Read timeout'},{Device:'pcs-03',LastSuccess:'2026-09-30 18:39:06',Missing:3,Error:'TCP retry'},{Device:'wtg-017',LastSuccess:'2026-09-30 18:42:55',Missing:2,Error:'High latency'}],
    errors:[{Time:'2026-09-30 18:40:31',Object:'wtg-003',Error:'Modbus read timeout'},{Time:'2026-09-30 18:39:08',Object:'pcs-03',Error:'TCP reconnect'}],
  }
  if(key==='reads')return {
    tasks:[{Task:'turbine-ads-all',LastSample:'2026-09-30 18:42:10',Failures:4,State:'Warning'},{Task:'turbine-modbus-all',LastSample:'2026-09-30 18:41:50',Failures:2,State:'Warning'}],
    devices:[{Device:'wtg-041',Point:'.wind_speed',LastSuccess:'2026-09-30 18:36:14',Error:'ADS read failed'},{Device:'wtg-003',Point:'grid_active_power',LastSuccess:'2026-09-30 18:41:49',Error:'Modbus timeout'}],
    errors:[{Time:'2026-09-30 18:42:11',Object:'wtg-041/.wind_speed',Error:'ADSERR_DEVICE_SYMBOLNOTFOUND'},{Time:'2026-09-30 18:41:52',Object:'wtg-003/grid_active_power',Error:'Read timeout'}],
  }
  return {tasks:[],devices:[],errors:[]}
}
function openMetric(metric:(typeof dataMetrics.value)[number]){
  const detail=metricDetail(metric.key)
  drawer.value={
    kind:'data-metric',
    title:metric.label,
    subtitle:`${metric.value} · ${metric.hint}`,
    distribution:[],
    tasks:detail.tasks,
    devices:detail.devices,
    errors:detail.errors,
    problems:[],
  }
  drawerOpen.value=true
}

function dimensionDetail(key:string){
  if(key==='continuity')return {
    distribution:[{name:'Normal',value:54},{name:'Warning',value:1},{name:'Fault',value:1}],
    problems:[{object:'turbine-ads-all',kind:'Task' as const,metric:'Gap 12m 18s',expected:'≤ 3 s',state:'Fault' as const,error:'No fresh samples'}],
  }
  if(key==='timeliness')return {
    distribution:[{name:'≤1.0×',value:42},{name:'1.0–1.5×',value:7},{name:'1.5–3.0×',value:6},{name:'>3.0×',value:1}],
    problems:[
      {object:'wtg-041',kind:'Device' as const,metric:'No fresh data',expected:'≤ 1.5 × period',state:'Fault' as const,error:'ADS session unavailable'},
      {object:'wtg-017',kind:'Device' as const,metric:'1.9 × period',expected:'≤ 1.5 × period',state:'Warning' as const,error:'High read latency'},
      {object:'wtg-029',kind:'Device' as const,metric:'1.7 × period',expected:'≤ 1.5 × period',state:'Warning' as const,error:'Jitter burst'},
    ],
  }
  if(key==='completeness')return {
    distribution:[{name:'≥99.9%',value:50},{name:'99–99.9%',value:4},{name:'<99%',value:2}],
    problems:[
      {object:'wtg-003',kind:'Device' as const,metric:'5 missing cycles',expected:'0',state:'Warning' as const,error:'Modbus timeout'},
      {object:'pcs-03',kind:'Device' as const,metric:'3 missing cycles',expected:'0',state:'Warning' as const,error:'TCP reconnect'},
    ],
  }
  if(key==='validity')return {
    distribution:[{name:'Valid',value:54},{name:'Warning',value:2},{name:'Fault',value:0}],
    problems:[
      {object:'wtg-041/.wind_speed',kind:'Point' as const,metric:'Decode failed',expected:'Valid float32',state:'Warning' as const,error:'Invalid payload length'},
      {object:'wtg-028/grid_active_power',kind:'Point' as const,metric:'Decode failed',expected:'Valid float32',state:'Warning' as const,error:'NaN payload'},
    ],
  }
  return {distribution:[{name:'Normal',value:3},{name:'Warning',value:0},{name:'Fault',value:0}],problems:[]}
}
function openDimension(row:(typeof dimensionRows.value)[number]){
  const detail=dimensionDetail(row.key)
  drawer.value={
    kind:'dimension',
    title:row.dimension,
    subtitle:`${dataWindow.value} · ${row.metric} · ${row.detail}`,
    distribution:detail.distribution,
    tasks:[],
    devices:[],
    errors:detail.problems.map(p=>({Object:p.object,Error:p.error})),
    problems:detail.problems,
  }
  drawerOpen.value=true
  nextTick(renderChart)
}

function renderChart(){
  chart?.dispose()
  if(!chartEl.value||!drawer.value?.distribution.length)return
  chart=echarts.init(chartEl.value)
  chart.setOption({
    animation:false,
    tooltip:{trigger:'axis'},
    grid:{left:44,right:18,top:20,bottom:42},
    xAxis:{type:'category',data:drawer.value.distribution.map(x=>x.name)},
    yAxis:{type:'value',minInterval:1},
    series:[{type:'bar',barMaxWidth:54,data:drawer.value.distribution.map(x=>x.value)}],
  })
}
watch(drawerOpen,open=>{
  if(open&&drawer.value?.distribution.length)nextTick(renderChart)
  if(!open){chart?.dispose();chart=null}
})
function stateType(state:string){
  if(state==='Healthy'||state==='Normal'||state==='Recovered')return 'success'
  if(state==='Degraded'||state==='Warning')return 'warning'
  if(state==='Interrupted'||state==='Fault'||state==='Active')return 'danger'
  return 'info'
}
onBeforeUnmount(()=>{if(autoTimer)window.clearInterval(autoTimer);chart?.dispose()})
</script>

<template>
  <div class="quality-page">
    <div class="head">
      <div><h1>Quality</h1><p>采集链路质量、数据质量与异常定位</p></div>
    </div>

    <el-tabs v-model="activeTab">
      <el-tab-pane label="Channel Quality" name="channel">
        <div class="quality-toolbar">
          <div class="toolbar-left">
            <el-segmented v-model="channelWindow" :options="['1 h','24 h','7 d']"/>
            <span class="last-check">Last check: {{lastChecked}}</span>
          </div>
          <div class="toolbar-actions">
            <span>Auto Check</span>
            <el-switch v-model="autoCheck"/>
            <el-select v-model="checkInterval" :disabled="!autoCheck" class="check-interval">
              <el-option :value="10" label="10 s"/><el-option :value="30" label="30 s"/><el-option :value="60" label="1 min"/>
            </el-select>
            <el-button type="primary" :loading="checking" @click="runCheck">Check</el-button>
          </div>
        </div>

        <div class="metric-grid clickable-metrics">
          <el-card v-for="metric in channelSummary" :key="metric.key" shadow="never" @click="openChannelMetric(metric)">
            <span>{{metric.label}}</span><b>{{metric.value}}</b><small>{{channelWindow}}</small>
          </el-card>
        </div>

        <section class="quality-section">
          <div class="section-head"><div><h2>Acquisition Channels</h2><p>设备采集通道当前状态；详情通过 Communication Events 查看。</p></div></div>
          <el-card shadow="never">
            <el-table :data="acquisitionChannels" height="360">
              <el-table-column prop="object" label="Device" min-width="130"/>
              <el-table-column v-if="!isMobile" prop="protocol" label="Protocol" width="100"/>
              <el-table-column label="State" width="110"><template #default="{row}"><el-tag :type="stateType(row.state)">{{row.state}}</el-tag></template></el-table-column>
              <el-table-column v-if="!isTablet" prop="target" label="Target" min-width="150"/>
              <el-table-column v-if="!isMobile" prop="last" label="Last Success" min-width="165"/>
              <el-table-column prop="latency" label="Latency" width="90"/>
              <el-table-column v-if="!isTablet" prop="timeouts" label="Timeouts" width="90"/>
              <el-table-column v-if="!isTablet" prop="reconnects" label="Reconnects" width="100"/>
              <el-table-column prop="issue" label="Current Issue" min-width="180" show-overflow-tooltip/>
            </el-table>
          </el-card>
        </section>

        <section class="quality-section">
          <div class="section-head"><div><h2>Delivery Channels</h2><p>Sink 交付通道当前状态。</p></div></div>
          <el-card shadow="never">
            <el-table :data="deliveryChannels">
              <el-table-column prop="object" label="Sink" min-width="130"/>
              <el-table-column prop="protocol" label="Type" width="110"/>
              <el-table-column label="State" width="110"><template #default="{row}"><el-tag :type="stateType(row.state)">{{row.state}}</el-tag></template></el-table-column>
              <el-table-column v-if="!isMobile" prop="target" label="Target" min-width="220" show-overflow-tooltip/>
              <el-table-column v-if="!isTablet" prop="last" label="Last Write" min-width="165"/>
              <el-table-column prop="latency" label="Latency" width="90"/>
              <el-table-column prop="issue" label="Current Issue" min-width="160" show-overflow-tooltip/>
            </el-table>
          </el-card>
        </section>

        <section class="quality-section">
          <div class="section-head"><div><h2>Communication Events</h2><p>连接中断、恢复、超时和退化事件。点击事件查看时间与错误信息。</p></div></div>
          <el-card shadow="never">
            <el-table :data="communicationEvents" @row-click="openEvent" class="clickable-table">
              <el-table-column prop="time" label="Time" min-width="165"/>
              <el-table-column prop="object" label="Object" min-width="130"/>
              <el-table-column v-if="!isMobile" prop="protocol" label="Protocol" width="100"/>
              <el-table-column prop="event" label="Event" min-width="150"/>
              <el-table-column label="State" width="105"><template #default="{row}"><el-tag :type="stateType(row.state)">{{row.state}}</el-tag></template></el-table-column>
              <el-table-column prop="error" label="Error / Evidence" min-width="240" show-overflow-tooltip/>
              <el-table-column v-if="!isTablet" prop="duration" label="Duration" width="100"/>
            </el-table>
          </el-card>
        </section>
      </el-tab-pane>

      <el-tab-pane label="Data Quality" name="data">
        <div class="quality-toolbar">
          <div><h2>Data Quality</h2><p>点击指标查看受影响的 Task / Device / Point 以及错误信息。</p></div>
          <el-segmented v-model="dataWindow" :options="['1 h','24 h','7 d']"/>
        </div>

        <div class="metric-grid clickable-metrics">
          <el-card v-for="metric in dataMetrics" :key="metric.key" shadow="never" @click="openMetric(metric)">
            <span>{{metric.label}}</span><b>{{metric.value}}</b><small>{{metric.hint}}</small>
          </el-card>
        </div>

        <section class="quality-section">
          <div class="section-head"><div><h2>Quality Dimensions</h2><p>点击维度查看分布以及质量较差的对象。</p></div></div>
          <div class="dimension-grid">
            <el-card v-for="row in dimensionRows" :key="row.key" shadow="never" class="dimension-card" @click="openDimension(row)">
              <div class="dimension-head">
                <span class="dimension-title">
                  <b>{{row.dimension}}</b>
                  <el-tooltip placement="top" :show-after="150">
                    <template #content>
                      <div class="dimension-help">
                        <div><b>Definition</b>：{{dimensionInfo[row.key].definition}}</div>
                        <div><b>Method</b>：{{dimensionInfo[row.key].method}}</div>
                        <div><b>Threshold</b>：{{dimensionInfo[row.key].threshold}}</div>
                      </div>
                    </template>
                    <el-icon class="info-icon" @click.stop><InfoFilled/></el-icon>
                  </el-tooltip>
                </span>
                <el-tag :type="stateType(row.status)" size="small">{{row.status}}</el-tag>
              </div>
              <strong>{{row.metric}}</strong>
              <span>{{row.detail}}</span>
            </el-card>
          </div>
        </section>

        <section class="quality-section">
          <div class="section-head"><div><h2>Active Data Issues</h2><p>当前未恢复的问题直接展示。</p></div></div>
          <el-card shadow="never">
            <el-table :data="activeIssues">
              <el-table-column label="Level" width="90"><template #default="{row}"><el-tag :type="stateType(row.level)">{{row.level}}</el-tag></template></el-table-column>
              <el-table-column prop="object" label="Object" min-width="140"/>
              <el-table-column v-if="!isMobile" prop="kind" label="Type" width="90"/>
              <el-table-column prop="dimension" label="Dimension" min-width="120"/>
              <el-table-column prop="issue" label="Issue" min-width="180"/>
              <el-table-column v-if="!isTablet" prop="duration" label="Duration" width="100"/>
              <el-table-column prop="error" label="Last Error" min-width="220" show-overflow-tooltip/>
            </el-table>
          </el-card>
        </section>
      </el-tab-pane>
    </el-tabs>

    <el-drawer v-model="drawerOpen" :title="drawer?.title||'Quality Detail'" :size="isMobile?'100%':'min(860px, 86vw)'" append-to-body>
      <template v-if="drawer">
        <p class="drawer-subtitle">{{drawer.subtitle}}</p>

        <section v-if="drawer.distribution.length" class="drawer-section">
          <h3>Distribution</h3>
          <div ref="chartEl" class="distribution-chart"></div>
        </section>

        <section v-if="drawer.kind==='dimension'" class="drawer-section">
          <h3>Problem Objects</h3>
          <el-table :data="drawer.problems" empty-text="No degraded objects">
            <el-table-column prop="object" label="Object" min-width="160"/>
            <el-table-column prop="kind" label="Type" width="90"/>
            <el-table-column prop="metric" label="Metric" min-width="130"/>
            <el-table-column v-if="!isMobile" prop="expected" label="Expected" min-width="130"/>
            <el-table-column label="State" width="100"><template #default="{row}"><el-tag :type="stateType(row.state)">{{row.state}}</el-tag></template></el-table-column>
            <el-table-column prop="error" label="Error" min-width="180" show-overflow-tooltip/>
          </el-table>
        </section>

        <section v-if="drawer.tasks.length" class="drawer-section">
          <h3>Affected Tasks</h3>
          <el-table :data="drawer.tasks"><el-table-column v-for="key in Object.keys(drawer.tasks[0]||{})" :key="key" :prop="key" :label="key" min-width="130"/></el-table>
        </section>
        <section v-if="drawer.devices.length" class="drawer-section">
          <h3>Affected Devices / Objects</h3>
          <el-table :data="drawer.devices"><el-table-column v-for="key in Object.keys(drawer.devices[0]||{})" :key="key" :prop="key" :label="key" min-width="130"/></el-table>
        </section>
        <section v-if="drawer.errors.length" class="drawer-section">
          <h3>Errors / Evidence</h3>
          <el-table :data="drawer.errors"><el-table-column v-for="key in Object.keys(drawer.errors[0]||{})" :key="key" :prop="key" :label="key" min-width="150"/></el-table>
        </section>
      </template>
    </el-drawer>
  </div>
</template>

<style scoped>
.quality-toolbar{display:flex;align-items:center;justify-content:space-between;gap:var(--app-space-4);margin-bottom:var(--app-space-4)}
.quality-toolbar h2{margin:0;font-size:var(--app-font-section-title)}.quality-toolbar p{margin:var(--app-space-1) 0 0;color:var(--app-text-muted);font-size:var(--app-font-body)}
.toolbar-left,.toolbar-actions{display:flex;align-items:center;gap:var(--app-space-2);flex-wrap:wrap}.last-check{color:var(--app-text-muted);font-size:var(--app-font-label)}.check-interval{width:92px}
.metric-grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:var(--app-space-3);margin-bottom:var(--app-space-6)}
.metric-grid :deep(.el-card__body){display:grid;gap:var(--app-space-1)}
.metric-grid span{color:var(--app-text-secondary);font-size:var(--app-font-label)}.metric-grid b{font-size:var(--app-font-metric);font-weight:var(--app-font-weight-semibold)}.metric-grid small{color:var(--app-text-muted)}
.clickable-metrics :deep(.el-card),.dimension-card,.clickable-table :deep(.el-table__row){cursor:pointer}
.quality-section{margin-top:var(--app-space-6)}.section-head{display:flex;align-items:flex-start;justify-content:space-between;gap:var(--app-space-4);margin-bottom:var(--app-space-3)}
.section-head h2,.drawer-section h3{margin:0;font-size:var(--app-font-section-title)}.section-head p,.drawer-subtitle{margin:var(--app-space-1) 0 0;color:var(--app-text-muted);font-size:var(--app-font-body)}
.dimension-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:var(--app-space-3)}.dimension-card :deep(.el-card__body){display:grid;gap:var(--app-space-2)}
.dimension-head{display:flex;align-items:center;justify-content:space-between;gap:var(--app-space-2)}.dimension-title{display:flex;align-items:center;gap:var(--app-space-1)}.info-icon{color:var(--app-text-muted);cursor:help}
.dimension-card strong{font-size:var(--app-font-panel-title)}.dimension-card>span{color:var(--app-text-secondary);font-size:var(--app-font-label)}.dimension-help{max-width:360px;display:grid;gap:6px;line-height:1.5}
.drawer-section{margin-top:var(--app-space-6)}.distribution-chart{height:260px}
@media(max-width:1199px){.metric-grid{grid-template-columns:repeat(2,minmax(0,1fr))}.dimension-grid{grid-template-columns:repeat(2,minmax(0,1fr))}}
@media(max-width:767px){.quality-toolbar{align-items:flex-start;flex-direction:column}.toolbar-actions{width:100%}.metric-grid,.dimension-grid{grid-template-columns:1fr}.distribution-chart{height:220px}}
</style>
