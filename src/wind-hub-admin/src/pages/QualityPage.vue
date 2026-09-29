<script setup lang="ts">
import * as echarts from 'echarts'
import { InfoFilled } from '@element-plus/icons-vue'
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { protocolOfDevice, store } from '../mock/data'

const activeTab = ref<'channel'|'data'>('channel')
const viewportWidth = ref(window.innerWidth)
const isMobile = computed(()=>viewportWidth.value<768)
const isTablet = computed(()=>viewportWidth.value<1200)
function updateViewport(){viewportWidth.value=window.innerWidth}
window.addEventListener('resize',updateViewport)

type Tone='normal'|'warning'|'danger'
type ChannelState='Healthy'|'Degraded'|'Interrupted'

interface AcquisitionChannel{
  object:string
  protocol:string
  state:ChannelState
  last:string
  latency:string
  latencyMs:number|null
  timeouts:number
  reconnects:number
  issue:string
}

const checking=ref(false)
const autoCheck=ref(false)
const checkInterval=ref(30)
const lastChecked=ref('Never')
const checkIntervalOptions=[
  {label:'5 s',value:5},
  {label:'10 s',value:10},
  {label:'30 s',value:30},
  {label:'1 min',value:60},
  {label:'5 min',value:300},
]
let autoCheckTimer:number|undefined
let checkCompletionTimer:number|undefined

function performCheck(){
  if(checking.value)return
  checking.value=true
  if(checkCompletionTimer)window.clearTimeout(checkCompletionTimer)
  checkCompletionTimer=window.setTimeout(()=>{
    checking.value=false
    lastChecked.value=new Date().toLocaleTimeString([],{hour:'2-digit',minute:'2-digit',second:'2-digit'})
    checkCompletionTimer=undefined
  },700)
}
function restartAutoCheck(){
  if(autoCheckTimer)window.clearInterval(autoCheckTimer)
  autoCheckTimer=undefined
  if(!autoCheck.value)return
  performCheck()
  autoCheckTimer=window.setInterval(performCheck,checkInterval.value*1000)
}
watch([autoCheck,checkInterval],restartAutoCheck)

const channelSummary=[
  {label:'Active interruptions',value:2,hint:'acquisition + delivery',tone:'danger' as Tone},
  {label:'Timeouts',value:13,hint:'last 24 h',tone:'warning' as Tone},
  {label:'Reconnects',value:7,hint:'last 24 h',tone:'warning' as Tone},
  {label:'Degraded endpoints',value:3,hint:'need attention',tone:'warning' as Tone},
]

const latencyPattern=[18,24,31,42,56,68,84,97,126,158,230,420]
const acquisitionChannels=computed<AcquisitionChannel[]>(()=>store.devices
  .filter(d=>d.enabled)
  .map((d,index)=>{
    const latencyMs=index===0?null:latencyPattern[index%latencyPattern.length]+Math.floor(index/latencyPattern.length)*3
    const state:ChannelState=index===0?'Interrupted':((latencyMs??0)>=120||index%7===0)?'Degraded':'Healthy'
    const timeouts=state==='Interrupted'?8:state==='Degraded'?(index%4)+1:0
    const reconnects=state==='Interrupted'?3:state==='Degraded'?(index%3):0
    return {
      object:d.device_id,
      protocol:protocolOfDevice(d),
      state,
      last:state==='Interrupted'?'12m 18s ago':String(Math.max(20,Math.round((latencyMs??0)*4)))+' ms ago',
      latency:latencyMs===null?'—':String(latencyMs)+' ms',
      latencyMs,
      timeouts,
      reconnects,
      issue:state==='Interrupted'?'Protocol session unavailable':state==='Degraded'?((latencyMs??0)>=120?'High acquisition latency':'Reconnect burst'):'—',
    }
  }))

const deliveryChannels=computed(()=>store.sinks
  .filter(s=>s.type!=='file')
  .map(s=>({
    object:s.name,
    protocol:s.type==='db'?'PostgreSQL':'Kafka',
    state:s.enabled?(s.runtime_state==='failed'?'Interrupted':s.runtime_state==='healthy'?'Healthy':'Degraded'):'Disabled',
    last:s.verification.checked_at||'Never',
    latency:s.latency_ms?String(s.latency_ms)+' ms':'—',
    timeouts:s.name==='kafka_main'?2:0,
    reconnects:s.name==='kafka_main'?1:0,
    issue:s.enabled?(s.error||'—'):'Sink disabled',
  })))

const acquisitionFilter=ref<'all'|'abnormal'>('all')
const acquisitionPage=ref(1)
const acquisitionPageSize=ref(20)
const filteredAcquisitionChannels=computed(()=>acquisitionFilter.value==='all'
  ? acquisitionChannels.value
  : acquisitionChannels.value.filter(c=>c.state!=='Healthy'))
const pagedAcquisitionChannels=computed(()=>{
  const start=(acquisitionPage.value-1)*acquisitionPageSize.value
  return filteredAcquisitionChannels.value.slice(start,start+acquisitionPageSize.value)
})
watch([acquisitionFilter,acquisitionPageSize],()=>{acquisitionPage.value=1})

interface LatencyBucket{
  label:string
  min:number
  max:number
  devices:AcquisitionChannel[]
}

const latencyBuckets=computed<LatencyBucket[]>(()=>{
  const defs=[
    {label:'0–20 ms',min:0,max:20},
    {label:'20–50 ms',min:20,max:50},
    {label:'50–100 ms',min:50,max:100},
    {label:'100–200 ms',min:100,max:200},
    {label:'200–500 ms',min:200,max:500},
    {label:'500+ ms',min:500,max:Number.POSITIVE_INFINITY},
  ]
  return defs.map(def=>({
    ...def,
    devices:acquisitionChannels.value.filter(c=>c.latencyMs!==null&&c.latencyMs>=def.min&&c.latencyMs<def.max),
  }))
})

function percentile(values:number[],ratio:number){
  if(!values.length)return 0
  const sorted=[...values].sort((a,b)=>a-b)
  const position=(sorted.length-1)*ratio
  const lower=Math.floor(position)
  const upper=Math.ceil(position)
  if(lower===upper)return sorted[lower]
  return Math.round(sorted[lower]+(sorted[upper]-sorted[lower])*(position-lower))
}
const latencyStats=computed(()=>{
  const values=acquisitionChannels.value.flatMap(c=>c.latencyMs===null?[]:[c.latencyMs])
  return {
    p50:percentile(values,.5),
    p95:percentile(values,.95),
    p99:percentile(values,.99),
  }
})

const latencyChartEl=ref<HTMLElement|null>(null)
let latencyChart:echarts.ECharts|null=null
let latencyResizeObserver:ResizeObserver|null=null

function css(name:string,fallback:string){
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim()||fallback
}
function escapeHtml(value:string){
  const map:Record<string,string>={'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}
  return value.replace(/[&<>"']/g,char=>map[char])
}
function renderLatencyChart(){
  if(!latencyChartEl.value)return
  latencyChart?.dispose()
  latencyResizeObserver?.disconnect()
  latencyChart=echarts.init(latencyChartEl.value)
  latencyChart.setOption({
    animation:false,
    grid:{left:42,right:18,top:18,bottom:38},
    tooltip:{
      trigger:'item',
      confine:true,
      formatter:(params:any)=>{
        const bucket=latencyBuckets.value[params.dataIndex]
        if(!bucket)return ''
        const devices=[...bucket.devices].sort((a,b)=>(b.latencyMs??0)-(a.latencyMs??0))
        const visible=devices.slice(0,10)
        const rows=visible.map(d=>'<div style="display:flex;justify-content:space-between;gap:24px"><span>'+escapeHtml(d.object)+'</span><b>'+String(d.latencyMs)+' ms</b></div>').join('')
        const more=devices.length>10?'<div style="margin-top:5px;color:#98a2b3">+ '+String(devices.length-10)+' more</div>':''
        return '<div style="min-width:180px"><b>'+escapeHtml(bucket.label)+'</b><div style="margin:3px 0 7px;color:#98a2b3">'+String(devices.length)+' devices</div>'+rows+more+'</div>'
      },
    },
    xAxis:{
      type:'category',
      data:latencyBuckets.value.map(b=>b.label),
      axisLabel:{color:css('--app-text-muted','#98a2b3'),fontSize:10,interval:0},
      axisLine:{lineStyle:{color:css('--app-border','#e5e9ef')}},
    },
    yAxis:{
      type:'value',
      minInterval:1,
      name:'Devices',
      nameTextStyle:{fontSize:10,color:css('--app-text-muted','#98a2b3')},
      axisLabel:{color:css('--app-text-muted','#98a2b3'),fontSize:10},
      splitLine:{lineStyle:{color:css('--app-border-soft','#eef0f3')}},
    },
    series:[{
      type:'bar',
      barMaxWidth:56,
      data:latencyBuckets.value.map(b=>b.devices.length),
    }],
  })
  latencyResizeObserver=new ResizeObserver(()=>latencyChart?.resize())
  latencyResizeObserver.observe(latencyChartEl.value)
}
watch(activeTab,tab=>{if(tab==='channel')nextTick(renderLatencyChart)})
watch(acquisitionChannels,()=>nextTick(renderLatencyChart),{deep:true})

const dataSummary=[
  {label:'Stale tasks',value:1,hint:'current',tone:'danger' as Tone},
  {label:'Missing cycles',value:13,hint:'last 24 h',tone:'warning' as Tone},
  {label:'Point read failures',value:6,hint:'last 24 h',tone:'warning' as Tone},
  {label:'Dropped points',value:0,hint:'last 24 h',tone:'normal' as Tone},
]

const dataDimensions=[
  {
    dimension:'Continuity',
    status:'Failed',
    metric:'1 stale task',
    detail:'Longest active gap 12m 18s',
    description:'判断期望采集周期是否出现连续中断、长时间无新数据或采集任务停滞。',
    tone:'danger' as Tone,
  },
  {
    dimension:'Timeliness',
    status:'Degraded',
    metric:'Jitter P95 87 ms',
    detail:'Worst task 8.7 × expected age',
    description:'判断数据是否在期望时间内到达，包括 freshness、采集延迟和 jitter。',
    tone:'warning' as Tone,
  },
  {
    dimension:'Completeness',
    status:'Degraded',
    metric:'13 missing cycles',
    detail:'6 point reads failed',
    description:'判断期望采集的数据是否完整，包括 missing cycles 和 point read failures。',
    tone:'warning' as Tone,
  },
  {
    dimension:'Validity',
    status:'Degraded',
    metric:'2 decode errors',
    detail:'No timestamp/order errors',
    description:'判断采集数据能否被正确解析和使用，包括 decode、timestamp、ordering 等技术有效性。',
    tone:'warning' as Tone,
  },
  {
    dimension:'Delivery Integrity',
    status:'Normal',
    metric:'0 dropped points',
    detail:'File sink backlog 2',
    description:'判断 wind-hub 已采集的数据是否完整交付到目标 Sink，包括 drop、queue backlog 和 write failure。',
    tone:'normal' as Tone,
  },
]

const dataIssues=computed(()=>[
  {object:'turbine-ads-all',level:'Fault',dimension:'Continuity',symptom:'No fresh samples',impact:'24 devices · 120 points',last:'12m 18s ago'},
  {object:'turbine-modbus-all',level:'Warning',dimension:'Timeliness',symptom:'Jitter above baseline',impact:'3 missing cycles',last:'80 ms ago'},
  {object:store.devices.filter(d=>d.enabled)[2]?.device_id||'wtg-025',level:'Warning',dimension:'Completeness',symptom:'3 point reads missing',impact:'3 points',last:'1.8 s ago'},
  {object:'file_archive',level:'Warning',dimension:'Delivery Integrity',symptom:'Queue depth increasing',impact:'2 queued batches',last:'4 ms ago'},
])

function tagType(tone:Tone){return tone==='danger'?'danger':tone==='warning'?'warning':'success'}
function channelTagType(state:string){return state==='Interrupted'?'danger':state==='Degraded'?'warning':state==='Healthy'?'success':'info'}

onMounted(()=>nextTick(renderLatencyChart))
onBeforeUnmount(()=>{
  window.removeEventListener('resize',updateViewport)
  if(autoCheckTimer)window.clearInterval(autoCheckTimer)
  if(checkCompletionTimer)window.clearTimeout(checkCompletionTimer)
  latencyResizeObserver?.disconnect()
  latencyChart?.dispose()
})
</script>

<template>
  <div class="standard-page quality-page">
    <div class="head quality-head">
      <div>
        <h1>Quality</h1>
        <p>区分通信链路质量与最终数据质量，回答“发生了什么、影响多大”；根因定位由 Diagnostics 负责。</p>
      </div>
      <div class="quality-check-controls">
        <el-button type="primary" :disabled="checking" @click="performCheck">Check</el-button>
        <label class="auto-check-control"><span>Auto Check</span><el-switch v-model="autoCheck"/></label>
        <el-select v-model="checkInterval" class="check-interval" aria-label="Check interval">
          <el-option v-for="option in checkIntervalOptions" :key="option.value" :label="option.label" :value="option.value"/>
        </el-select>
        <span class="last-check">{{checking?'Checking…':'Last checked '+lastChecked}}</span>
      </div>
    </div>

    <el-tabs v-model="activeTab" class="quality-tabs">
      <el-tab-pane label="Channel Quality" name="channel">
        <div class="quality-definition">
          <div><b>Channel Quality</b><span>wind-hub 与外部端点之间的通信链路质量，包括网络 Sink 交付侧和设备采集侧。</span></div>
        </div>

        <div class="quality-metric-grid">
          <el-card v-for="m in channelSummary" :key="m.label" shadow="never">
            <span>{{m.label}}</span>
            <div><b>{{m.value}}</b><el-tag :type="tagType(m.tone)" size="small">{{m.tone==='danger'?'Fault':m.tone==='warning'?'Attention':'Normal'}}</el-tag></div>
            <small>{{m.hint}}</small>
          </el-card>
        </div>

        <section class="quality-section">
          <div class="section-title"><div><h2>Delivery Channels</h2><p>wind-hub → Kafka / PostgreSQL / Redis 等网络 Sink；File 不计入网络信道。</p></div></div>
          <el-card shadow="never">
            <el-table :data="deliveryChannels" empty-text="No network Sinks configured">
              <el-table-column prop="object" label="Sink" min-width="145"/>
              <el-table-column prop="protocol" label="Protocol" width="110"/>
              <el-table-column label="State" width="105"><template #default="{row}"><el-tag :type="channelTagType(row.state)" size="small">{{row.state}}</el-tag></template></el-table-column>
              <el-table-column v-if="!isMobile" prop="last" label="Last Check" min-width="145"/>
              <el-table-column v-if="!isTablet" prop="latency" label="Latency" width="90"/>
              <el-table-column v-if="!isTablet" prop="timeouts" label="Timeout" width="78" align="right"/>
              <el-table-column v-if="!isTablet" prop="reconnects" label="Reconnect" width="88" align="right"/>
              <el-table-column prop="issue" label="Current Issue" min-width="190"/>
            </el-table>
          </el-card>
        </section>

        <section class="quality-section acquisition-section">
          <div class="section-title"><div><h2>Acquisition Channels</h2><p>Device / PLC / EMS → wind-hub。Latency 分布统计全部有效采集信道，不受表格分页影响。</p></div></div>

          <el-card shadow="never" class="latency-card">
            <div class="latency-card-head">
              <div><b>Latency Distribution</b><span>Hover a bar to inspect devices and latency.</span></div>
              <div class="latency-stats">
                <div><span>P50</span><b>{{latencyStats.p50}} ms</b></div>
                <div><span>P95</span><b>{{latencyStats.p95}} ms</b></div>
                <div><span>P99</span><b>{{latencyStats.p99}} ms</b></div>
              </div>
            </div>
            <div ref="latencyChartEl" class="latency-chart"/>
          </el-card>

          <div class="acquisition-toolbar">
            <el-segmented v-model="acquisitionFilter" :options="[{label:'All',value:'all'},{label:'Abnormal',value:'abnormal'}]"/>
            <span>{{filteredAcquisitionChannels.length}} channels</span>
          </div>

          <el-card shadow="never">
            <el-table :data="pagedAcquisitionChannels">
              <el-table-column prop="object" label="Endpoint" min-width="145"/>
              <el-table-column prop="protocol" label="Protocol" width="100"/>
              <el-table-column label="State" width="105"><template #default="{row}"><el-tag :type="channelTagType(row.state)" size="small">{{row.state}}</el-tag></template></el-table-column>
              <el-table-column v-if="!isMobile" prop="last" label="Last Success" min-width="120"/>
              <el-table-column v-if="!isTablet" prop="latency" label="Latency" width="90"/>
              <el-table-column v-if="!isTablet" prop="timeouts" label="Timeout" width="78" align="right"/>
              <el-table-column v-if="!isTablet" prop="reconnects" label="Reconnect" width="88" align="right"/>
              <el-table-column prop="issue" label="Current Issue" min-width="190"/>
            </el-table>
            <div class="acquisition-pagination">
              <el-pagination
                v-model:current-page="acquisitionPage"
                v-model:page-size="acquisitionPageSize"
                :page-sizes="[20,50,100]"
                :total="filteredAcquisitionChannels.length"
                :layout="isMobile?'prev, pager, next':'total, sizes, prev, pager, next'"
              />
            </div>
          </el-card>
        </section>
      </el-tab-pane>

      <el-tab-pane label="Data Quality" name="data">
        <div class="quality-definition">
          <div><b>Data Quality</b><span>衡量期望数据流与实际可用数据流之间的偏差，不评价风速、功率等业务值是否合理。</span></div>
        </div>

        <div class="quality-metric-grid">
          <el-card v-for="m in dataSummary" :key="m.label" shadow="never">
            <span>{{m.label}}</span>
            <div><b>{{m.value}}</b><el-tag :type="tagType(m.tone)" size="small">{{m.tone==='danger'?'Fault':m.tone==='warning'?'Attention':'Normal'}}</el-tag></div>
            <small>{{m.hint}}</small>
          </el-card>
        </div>

        <section class="quality-section">
          <div class="section-title"><div><h2>Quality Dimensions</h2><p>端到端数据质量的五个独立观察维度。</p></div></div>
          <div class="dimension-grid">
            <el-card v-for="d in dataDimensions" :key="d.dimension" shadow="never" class="dimension-card">
              <div class="dimension-card-head">
                <span class="dimension-name">
                  <b>{{d.dimension}}</b>
                  <el-tooltip placement="top" effect="dark" :show-after="200" :teleported="true">
                    <template #content><div class="dimension-tooltip"><b>{{d.dimension}}</b><span>{{d.description}}</span></div></template>
                    <el-icon class="info-icon" :aria-label="d.dimension+' definition'"><InfoFilled/></el-icon>
                  </el-tooltip>
                </span>
                <el-tag :type="tagType(d.tone)" size="small">{{d.status}}</el-tag>
              </div>
              <strong>{{d.metric}}</strong>
              <span class="dimension-detail">{{d.detail}}</span>
            </el-card>
          </div>
        </section>

        <section class="quality-section">
          <div class="section-title"><div><h2>Active Data Issues</h2><p>只展示会影响最终可用数据的异常。</p></div></div>
          <el-card shadow="never">
            <el-table :data="dataIssues">
              <el-table-column prop="object" label="Object" min-width="155"/>
              <el-table-column label="Level" width="95"><template #default="{row}"><el-tag :type="row.level==='Fault'?'danger':'warning'" size="small">{{row.level}}</el-tag></template></el-table-column>
              <el-table-column prop="dimension" label="Dimension" min-width="130"/>
              <el-table-column prop="symptom" label="Symptom" min-width="180"/>
              <el-table-column v-if="!isMobile" prop="impact" label="Impact" min-width="160"/>
              <el-table-column prop="last" label="Last Good" min-width="115"/>
            </el-table>
          </el-card>
        </section>
      </el-tab-pane>
    </el-tabs>
  </div>
</template>

<style scoped>
.quality-head{align-items:flex-end}.quality-check-controls{display:flex;align-items:center;justify-content:flex-end;gap:var(--app-space-3);flex-wrap:wrap}.auto-check-control{display:inline-flex;align-items:center;gap:var(--app-space-2);color:var(--app-text-secondary);font-size:var(--app-font-label);white-space:nowrap}.check-interval{width:92px}.last-check{min-width:124px;color:var(--app-text-muted);font-size:var(--app-font-caption);font-variant-numeric:tabular-nums;white-space:nowrap}
.quality-tabs{margin-top:var(--app-space-4)}.quality-definition{display:flex;align-items:center;justify-content:space-between;gap:var(--app-space-4);margin-bottom:var(--app-space-4);padding:var(--app-space-3) 0}.quality-definition>div{display:grid;gap:4px}.quality-definition b{font-size:var(--app-font-panel-title);font-weight:var(--app-font-weight-semibold)}.quality-definition span{color:var(--app-text-muted);font-size:var(--app-font-caption)}
.quality-metric-grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:var(--app-space-3);margin-bottom:var(--app-space-6)}.quality-metric-grid .el-card span,.quality-metric-grid .el-card small{color:var(--app-text-muted);font-size:var(--app-font-caption)}.quality-metric-grid .el-card>div>div{display:flex;align-items:center;justify-content:space-between;gap:var(--app-space-2);margin:var(--app-space-2) 0}.quality-metric-grid b{color:var(--app-text-primary);font-size:var(--app-font-metric);font-weight:var(--app-font-weight-semibold)}
.quality-section{margin-bottom:var(--app-space-6)}.section-title{margin-bottom:var(--app-space-3)}.section-title h2{margin:0;font-size:var(--app-font-section-title);font-weight:var(--app-font-weight-semibold)}.section-title p{margin:4px 0 0;color:var(--app-text-muted);font-size:var(--app-font-caption)}
.latency-card{margin-bottom:var(--app-space-3)}.latency-card-head{display:flex;align-items:flex-start;justify-content:space-between;gap:var(--app-space-4)}.latency-card-head>div:first-child{display:grid;gap:4px}.latency-card-head>div:first-child>b{font-size:var(--app-font-panel-title)}.latency-card-head>div:first-child>span{color:var(--app-text-muted);font-size:var(--app-font-caption)}.latency-stats{display:flex;align-items:center;gap:var(--app-space-5)}.latency-stats>div{display:grid;gap:2px;text-align:right}.latency-stats span{color:var(--app-text-muted);font-size:var(--app-font-caption)}.latency-stats b{font-size:var(--app-font-body);font-weight:var(--app-font-weight-semibold);font-variant-numeric:tabular-nums}.latency-chart{height:230px;margin-top:var(--app-space-2)}
.acquisition-toolbar{display:flex;align-items:center;justify-content:space-between;gap:var(--app-space-3);margin:var(--app-space-3) 0}.acquisition-toolbar>span{color:var(--app-text-muted);font-size:var(--app-font-caption)}.acquisition-pagination{display:flex;justify-content:flex-end;padding-top:var(--app-space-3)}
.dimension-grid{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:var(--app-space-3)}.dimension-card{min-width:0}.dimension-card-head{display:flex;align-items:center;justify-content:space-between;gap:var(--app-space-2)}.dimension-name{display:inline-flex;align-items:center;gap:5px;min-width:0}.dimension-name b{font-size:var(--app-font-panel-title);font-weight:var(--app-font-weight-semibold)}.info-icon{flex:0 0 auto;color:var(--app-text-muted);font-size:var(--app-font-panel-title);cursor:help;transition:color .15s ease}.info-icon:hover{color:var(--app-text-secondary)}.dimension-card strong{display:block;margin-top:var(--app-space-4);font-size:var(--app-font-panel-title);font-weight:var(--app-font-weight-semibold)}.dimension-detail{display:block;margin-top:4px;color:var(--app-text-muted);font-size:var(--app-font-caption);line-height:var(--app-line-height-body)}.dimension-tooltip{display:grid;gap:5px;max-width:300px;line-height:1.5}.dimension-tooltip b{font-weight:var(--app-font-weight-semibold)}
@media(max-width:1399px){.dimension-grid{grid-template-columns:repeat(3,minmax(0,1fr))}}
@media(max-width:1199px){.quality-metric-grid{grid-template-columns:repeat(2,minmax(0,1fr))}.quality-head{align-items:flex-start;flex-direction:column;gap:var(--app-space-4)}.quality-check-controls{justify-content:flex-start}.dimension-grid{grid-template-columns:repeat(2,minmax(0,1fr))}}
@media(max-width:767px){.quality-definition{align-items:flex-start;flex-direction:column}.quality-metric-grid,.dimension-grid{grid-template-columns:1fr}.quality-check-controls{width:100%;justify-content:flex-start}.last-check{width:100%}.latency-card-head{flex-direction:column}.latency-stats{width:100%;justify-content:space-between;gap:var(--app-space-3)}.latency-stats>div{text-align:left}.latency-chart{height:220px}.acquisition-pagination{justify-content:center;overflow-x:auto}}
</style>
