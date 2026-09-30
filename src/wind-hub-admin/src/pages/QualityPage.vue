<script setup lang="ts">
import * as echarts from 'echarts'
import { computed, nextTick, onBeforeUnmount, ref, watch } from 'vue'
import { useViewport } from '../composables/useViewport'
import { protocolOfDevice, store } from '../mock/data'

type ChannelState='Healthy'|'Degraded'|'Interrupted'|'Disabled'
type DataWindow='1 h'|'24 h'|'7 d'
type DrawerKind='event'|'metric'|'dimension'

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
  tasks:Array<Record<string,string|number>>
  devices:Array<Record<string,string|number>>
  errors:Array<Record<string,string|number>>
  distribution:Array<{name:string;value:number}>
  problems:ProblemObject[]
}

const activeTab=ref<'channel'|'data'>('channel')
const dataWindow=ref<DataWindow>('24 h')
const { isMobile, isTablet }=useViewport()
const drawerOpen=ref(false)
const drawer=ref<QualityDrawer|null>(null)
const chartEl=ref<HTMLElement|null>(null)
let chart:echarts.ECharts|null=null

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
    last:interrupted?'No recent success':(80+(index%8)*45)+' ms ago',
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

const channelSummary=computed(()=>{
  const all=[...acquisitionChannels.value,...deliveryChannels.value]
  return {
    interrupted:all.filter(x=>x.state==='Interrupted').length,
    degraded:all.filter(x=>x.state==='Degraded').length,
    timeouts:all.reduce((sum,x)=>sum+x.timeouts,0),
    reconnects:all.reduce((sum,x)=>sum+x.reconnects,0),
  }
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

const windowFactor=computed(()=>dataWindow.value==='1 h'?0.2:dataWindow.value==='7 d'?4.5:1)
const dataMetrics=computed(()=>[
  {key:'stale',label:'Stale Tasks',value:1,hint:'current',tone:'danger'},
  {key:'missing',label:'Missing Cycles',value:Math.max(1,Math.round(13*windowFactor.value)),hint:dataWindow.value,tone:'warning'},
  {key:'reads',label:'Point Read Failures',value:Math.max(1,Math.round(6*windowFactor.value)),hint:dataWindow.value,tone:'warning'},
  {key:'dropped',label:'Dropped Points',value:0,hint:dataWindow.value,tone:'normal'},
] as const)

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

function openEvent(row:CommunicationEvent){
  drawer.value={
    kind:'event',
    title:row.event,
    subtitle:row.object+' · '+row.protocol,
    tasks:[],
    devices:[{Object:row.object,Target:row.target,State:row.state,Duration:row.duration}],
    errors:[{Time:row.time,Object:row.object,Error:row.error}],
    distribution:[],
    problems:[],
  }
  drawerOpen.value=true
}

function metricDetail(key:string){
  if(key==='stale')return {
    tasks:[{Task:'turbine-ads-all',Expected:'1 s',LastSample:'12m 18s ago',State:'Stale'}],
    devices:[{Device:'wtg-041',Protocol:'ADS',LastSuccess:'12m 18s ago',State:'Fault'}],
    errors:[{Time:'18:36:14',Object:'wtg-041',Error:'ADS session unavailable'},{Time:'18:36:12',Object:'turbine-ads-all',Error:'No fresh samples'}],
  }
  if(key==='missing')return {
    tasks:[{Task:'turbine-modbus-all',Expected:'1 s',Missing:8,State:'Warning'},{Task:'pcs-fast',Expected:'1 s',Missing:5,State:'Warning'}],
    devices:[{Device:'wtg-003',Missing:5,Error:'Read timeout'},{Device:'pcs-03',Missing:3,Error:'TCP retry'},{Device:'wtg-017',Missing:2,Error:'High latency'}],
    errors:[{Time:'18:40:31',Object:'wtg-003',Error:'Modbus read timeout'},{Time:'18:39:08',Object:'pcs-03',Error:'TCP reconnect'}],
  }
  if(key==='reads')return {
    tasks:[{Task:'turbine-ads-all',Failures:4,State:'Warning'},{Task:'turbine-modbus-all',Failures:2,State:'Warning'}],
    devices:[{Device:'wtg-041',Point:'.wind_speed',Error:'ADS read failed'},{Device:'wtg-003',Point:'grid_active_power',Error:'Modbus timeout'}],
    errors:[{Time:'18:42:11',Object:'wtg-041/.wind_speed',Error:'ADSERR_DEVICE_SYMBOLNOTFOUND'},{Time:'18:41:52',Object:'wtg-003/grid_active_power',Error:'Read timeout'}],
  }
  return {tasks:[],devices:[],errors:[]}
}

function openMetric(metric:(typeof dataMetrics.value)[number]){
  const detail=metricDetail(metric.key)
  drawer.value={
    kind:'metric',
    title:metric.label,
    subtitle:`${metric.value} · ${metric.hint}`,
    tasks:detail.tasks,
    devices:detail.devices,
    errors:detail.errors,
    distribution:[],
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
    distribution:[{name:'Normal',value:49},{name:'Warning',value:6},{name:'Fault',value:1}],
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
    subtitle:row.metric+' · '+row.detail,
    tasks:[],
    devices:[],
    errors:detail.problems.map(p=>({Object:p.object,Error:p.error})),
    distribution:detail.distribution,
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
    tooltip:{trigger:'item'},
    grid:{left:42,right:18,top:20,bottom:42},
    xAxis:{type:'category',data:drawer.value.distribution.map(x=>x.name)},
    yAxis:{type:'value',minInterval:1},
    series:[{type:'bar',barMaxWidth:54,data:drawer.value.distribution.map(x=>x.value)}],
  })
}
watch(drawerOpen,open=>{if(open&&drawer.value?.kind==='dimension')nextTick(renderChart);if(!open){chart?.dispose();chart=null}})
onBeforeUnmount(()=>chart?.dispose())

function stateType(state:string){
  if(state==='Healthy'||state==='Normal'||state==='Recovered')return 'success'
  if(state==='Degraded'||state==='Warning')return 'warning'
  if(state==='Interrupted'||state==='Fault'||state==='Active')return 'danger'
  return 'info'
}
</script>

<template>
  <div class="quality-page">
    <div class="head">
      <div><h1>Quality</h1><p>采集链路质量、数据质量与异常定位</p></div>
    </div>

    <el-tabs v-model="activeTab">
      <el-tab-pane label="Channel Quality" name="channel">
        <div class="metric-grid">
          <el-card shadow="never"><span>Interrupted</span><b>{{channelSummary.interrupted}}</b></el-card>
          <el-card shadow="never"><span>Degraded</span><b>{{channelSummary.degraded}}</b></el-card>
          <el-card shadow="never"><span>Timeouts</span><b>{{channelSummary.timeouts}}</b></el-card>
          <el-card shadow="never"><span>Reconnects</span><b>{{channelSummary.reconnects}}</b></el-card>
        </div>

        <section class="quality-section">
          <div class="section-head"><div><h2>Acquisition Channels</h2><p>设备采集通道当前状态；详情通过 Communication Events 查看。</p></div></div>
          <el-card shadow="never">
            <el-table :data="acquisitionChannels" height="360">
              <el-table-column prop="object" label="Device" min-width="130"/>
              <el-table-column v-if="!isMobile" prop="protocol" label="Protocol" width="100"/>
              <el-table-column label="State" width="110"><template #default="{row}"><el-tag :type="stateType(row.state)">{{row.state}}</el-tag></template></el-table-column>
              <el-table-column v-if="!isTablet" prop="target" label="Target" min-width="150"/>
              <el-table-column v-if="!isMobile" prop="last" label="Last Success" min-width="130"/>
              <el-table-column prop="latency" label="Latency" width="90"/>
              <el-table-column v-if="!isTablet" prop="timeouts" label="Timeouts" width="90"/>
              <el-table-column v-if="!isTablet" prop="reconnects" label="Reconnects" width="100"/>
              <el-table-column prop="issue" label="Current Issue" min-width="180" show-overflow-tooltip/>
            </el-table>
          </el-card>
        </section>

        <section class="quality-section">
          <div class="section-head"><div><h2>Delivery Channels</h2><p>Sink 交付通道当前状态；不再使用详情 Drawer。</p></div></div>
          <el-card shadow="never">
            <el-table :data="deliveryChannels">
              <el-table-column prop="object" label="Sink" min-width="130"/>
              <el-table-column prop="protocol" label="Type" width="110"/>
              <el-table-column label="State" width="110"><template #default="{row}"><el-tag :type="stateType(row.state)">{{row.state}}</el-tag></template></el-table-column>
              <el-table-column v-if="!isMobile" prop="target" label="Target" min-width="220" show-overflow-tooltip/>
              <el-table-column v-if="!isTablet" prop="last" label="Last Write" min-width="150"/>
              <el-table-column prop="latency" label="Latency" width="90"/>
              <el-table-column prop="issue" label="Current Issue" min-width="160" show-overflow-tooltip/>
            </el-table>
          </el-card>
        </section>

        <section class="quality-section">
          <div class="section-head"><div><h2>Communication Events</h2><p>连接中断、恢复、超时和退化事件。点击事件查看时间线与错误信息。</p></div></div>
          <el-card shadow="never">
            <el-table :data="communicationEvents" @row-click="openEvent" class="clickable-table">
              <el-table-column prop="time" label="Time" min-width="150"/>
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
        <div class="data-toolbar">
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
              <div><b>{{row.dimension}}</b><el-tag :type="stateType(row.status)" size="small">{{row.status}}</el-tag></div>
              <strong>{{row.metric}}</strong>
              <span>{{row.detail}}</span>
            </el-card>
          </div>
        </section>

        <section class="quality-section">
          <div class="section-head"><div><h2>Active Data Issues</h2><p>当前未恢复的问题直接展示，不打开 Drawer。</p></div></div>
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

    <el-drawer v-model="drawerOpen" :title="drawer?.title||'Quality Detail'" :size="isMobile?'100%':'min(840px, 86vw)'" append-to-body>
      <template v-if="drawer">
        <p class="drawer-subtitle">{{drawer.subtitle}}</p>

        <template v-if="drawer.kind==='dimension'">
          <section class="drawer-section">
            <h3>Distribution</h3>
            <div ref="chartEl" class="distribution-chart"></div>
          </section>
          <section class="drawer-section">
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
        </template>

        <template v-else>
          <section v-if="drawer.tasks.length" class="drawer-section">
            <h3>Affected Tasks</h3>
            <el-table :data="drawer.tasks"><el-table-column v-for="key in Object.keys(drawer.tasks[0]||{})" :key="key" :prop="key" :label="key" min-width="120"/></el-table>
          </section>
          <section v-if="drawer.devices.length" class="drawer-section">
            <h3>Affected Devices / Objects</h3>
            <el-table :data="drawer.devices"><el-table-column v-for="key in Object.keys(drawer.devices[0]||{})" :key="key" :prop="key" :label="key" min-width="120"/></el-table>
          </section>
        </template>

        <section v-if="drawer.errors.length" class="drawer-section">
          <h3>Errors / Evidence</h3>
          <el-table :data="drawer.errors"><el-table-column v-for="key in Object.keys(drawer.errors[0]||{})" :key="key" :prop="key" :label="key" min-width="140"/></el-table>
        </section>
      </template>
    </el-drawer>
  </div>
</template>

<style scoped>
.metric-grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:var(--app-space-3);margin-bottom:var(--app-space-6)}
.metric-grid :deep(.el-card__body){display:grid;gap:var(--app-space-1)}
.metric-grid span{color:var(--app-text-secondary);font-size:var(--app-font-label)}
.metric-grid b{font-size:var(--app-font-metric);font-weight:var(--app-font-weight-semibold)}
.metric-grid small{color:var(--app-text-muted)}
.clickable-metrics :deep(.el-card),.dimension-card,.clickable-table :deep(.el-table__row){cursor:pointer}
.quality-section{margin-top:var(--app-space-6)}
.section-head,.data-toolbar{display:flex;align-items:flex-start;justify-content:space-between;gap:var(--app-space-4);margin-bottom:var(--app-space-3)}
.section-head h2,.data-toolbar h2,.drawer-section h3{margin:0;font-size:var(--app-font-section-title)}
.section-head p,.data-toolbar p,.drawer-subtitle{margin:var(--app-space-1) 0 0;color:var(--app-text-muted);font-size:var(--app-font-body)}
.dimension-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:var(--app-space-3)}
.dimension-card :deep(.el-card__body){display:grid;gap:var(--app-space-2)}
.dimension-card :deep(.el-card__body)>div{display:flex;align-items:center;justify-content:space-between;gap:var(--app-space-2)}
.dimension-card strong{font-size:var(--app-font-panel-title)}
.dimension-card span{color:var(--app-text-secondary);font-size:var(--app-font-label)}
.drawer-section{margin-top:var(--app-space-6)}
.distribution-chart{height:260px}
@media(max-width:1199px){.metric-grid{grid-template-columns:repeat(2,minmax(0,1fr))}.dimension-grid{grid-template-columns:repeat(2,minmax(0,1fr))}}
@media(max-width:767px){.metric-grid,.dimension-grid{grid-template-columns:1fr}.data-toolbar{flex-direction:column}.distribution-chart{height:220px}}
</style>
