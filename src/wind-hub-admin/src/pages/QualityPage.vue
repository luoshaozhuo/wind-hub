<script setup lang="ts">
import * as echarts from 'echarts'
import { InfoFilled } from '@element-plus/icons-vue'
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { protocolOfDevice, store } from '../mock/data'

const activeTab=ref<'channel'|'data'>('channel')
const viewportWidth=ref(window.innerWidth)
const isMobile=computed(()=>viewportWidth.value<768)
const isTablet=computed(()=>viewportWidth.value<1200)
function updateViewport(){viewportWidth.value=window.innerWidth}
window.addEventListener('resize',updateViewport)

type Tone='normal'|'warning'|'danger'
type ChannelState='Healthy'|'Degraded'|'Interrupted'
type DataWindow='1 h'|'24 h'|'7 d'

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
const nextCheckIn=ref<number|null>(null)
const checkGeneration=ref(0)
const checkIntervalOptions=[
  {label:'5 s',value:5},
  {label:'10 s',value:10},
  {label:'30 s',value:30},
  {label:'1 min',value:60},
  {label:'5 min',value:300},
]
let autoCheckTimer:number|undefined
let countdownTimer:number|undefined
let checkCompletionTimer:number|undefined

function performCheck(){
  if(checking.value)return
  checking.value=true
  if(checkCompletionTimer)window.clearTimeout(checkCompletionTimer)
  checkCompletionTimer=window.setTimeout(()=>{
    checkGeneration.value+=1
    lastChecked.value=new Date().toLocaleTimeString([],{hour:'2-digit',minute:'2-digit',second:'2-digit'})
    checking.value=false
    if(autoCheck.value)nextCheckIn.value=checkInterval.value
    checkCompletionTimer=undefined
  },700)
}
function restartAutoCheck(){
  if(autoCheckTimer)window.clearInterval(autoCheckTimer)
  if(countdownTimer)window.clearInterval(countdownTimer)
  autoCheckTimer=undefined
  countdownTimer=undefined
  nextCheckIn.value=null
  if(!autoCheck.value)return

  nextCheckIn.value=checkInterval.value
  autoCheckTimer=window.setInterval(performCheck,checkInterval.value*1000)
  countdownTimer=window.setInterval(()=>{
    if(nextCheckIn.value===null)return
    nextCheckIn.value=nextCheckIn.value<=1?checkInterval.value:nextCheckIn.value-1
  },1000)
}
watch([autoCheck,checkInterval],restartAutoCheck)

const latencyPattern=[12,18,24,31,38,46,57,69,82,96,118,146,188,245,360,540]
const acquisitionChannels=computed<AcquisitionChannel[]>(()=>store.devices
  .filter(d=>d.enabled)
  .map((d,index)=>{
    const wave=(checkGeneration.value*7+index*3)%19-9
    const base=latencyPattern[index%latencyPattern.length]+Math.floor(index/latencyPattern.length)*4
    const latencyMs=index===0&&checkGeneration.value%3===0?null:Math.max(4,base+wave)
    const state:ChannelState=latencyMs===null?'Interrupted':latencyMs>=180||((index+checkGeneration.value)%11===0)?'Degraded':'Healthy'
    const timeouts=state==='Interrupted'?8:state==='Degraded'?((index+checkGeneration.value)%4)+1:0
    const reconnects=state==='Interrupted'?3:state==='Degraded'?((index+checkGeneration.value)%3):0
    return {
      object:d.device_id,
      protocol:protocolOfDevice(d),
      state,
      last:state==='Interrupted'?'No recent success':String(Math.max(15,Math.round((latencyMs??0)*2.5)))+' ms ago',
      latency:latencyMs===null?'—':String(latencyMs)+' ms',
      latencyMs,
      timeouts,
      reconnects,
      issue:state==='Interrupted'?'Protocol session unavailable':state==='Degraded'?(latencyMs!==null&&latencyMs>=180?'High acquisition latency':'Reconnect burst'):'—',
    }
  }))

const deliveryChannels=computed(()=>store.sinks
  .filter(s=>s.type!=='file')
  .map((s,index)=>{
    const baseLatency=s.latency_ms??18
    const latency=Math.max(3,baseLatency+((checkGeneration.value+index*2)%7)-3)
    const runtimeState=s.enabled?(s.runtime_state==='failed'?'Interrupted':s.runtime_state==='healthy'?'Healthy':'Degraded'):'Disabled'
    return {
      object:s.name,
      protocol:s.type==='db'?'PostgreSQL':s.type==='redis'?'Redis':'Kafka',
      state:runtimeState,
      last:lastChecked.value==='Never'?(s.verification.checked_at||'Never'):lastChecked.value,
      latency:s.enabled?String(latency)+' ms':'—',
      timeouts:s.name==='kafka_main'?(checkGeneration.value%3):0,
      reconnects:s.name==='kafka_main'?(checkGeneration.value%2):0,
      issue:s.enabled?(s.error||'—'):'Sink disabled',
    }
  }))

const channelSummary=computed(()=>{
  const acquisition=acquisitionChannels.value
  const delivery=deliveryChannels.value
  const interrupted=acquisition.filter(c=>c.state==='Interrupted').length+delivery.filter(c=>c.state==='Interrupted').length
  const timeouts=acquisition.reduce((sum,c)=>sum+c.timeouts,0)+delivery.reduce((sum,c)=>sum+c.timeouts,0)
  const reconnects=acquisition.reduce((sum,c)=>sum+c.reconnects,0)+delivery.reduce((sum,c)=>sum+c.reconnects,0)
  const degraded=acquisition.filter(c=>c.state==='Degraded').length+delivery.filter(c=>c.state==='Degraded').length
  return [
    {label:'Active interruptions',value:interrupted,hint:'acquisition + delivery',tone:(interrupted?'danger':'normal') as Tone},
    {label:'Timeouts',value:timeouts,hint:'current check snapshot',tone:(timeouts?'warning':'normal') as Tone},
    {label:'Reconnects',value:reconnects,hint:'current check snapshot',tone:(reconnects?'warning':'normal') as Tone},
    {
      label:'Degraded Channels',
      value:degraded,
      hint:'current check snapshot',
      tone:(degraded?'warning':'normal') as Tone,
      description:'Healthy：通信正常。Degraded：信道仍可用，但已出现高延迟、偶发超时、频繁重连或部分请求失败。Interrupted：信道不可用或持续无法完成有效通信。状态阈值最终由后端按协议和配置判定。',
    },
  ]
})

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
  count:number
}

const latencyBuckets=computed<LatencyBucket[]>(()=>{
  const defs=[
    {label:'0–10',min:0,max:10},
    {label:'10–20',min:10,max:20},
    {label:'20–30',min:20,max:30},
    {label:'30–50',min:30,max:50},
    {label:'50–75',min:50,max:75},
    {label:'75–100',min:75,max:100},
    {label:'100–150',min:100,max:150},
    {label:'150–200',min:150,max:200},
    {label:'200–300',min:200,max:300},
    {label:'300–500',min:300,max:500},
    {label:'500+',min:500,max:Number.POSITIVE_INFINITY},
  ]
  return defs.map(def=>({
    ...def,
    count:acquisitionChannels.value.filter(c=>c.latencyMs!==null&&c.latencyMs>=def.min&&c.latencyMs<def.max).length,
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
  return {p50:percentile(values,.5),p95:percentile(values,.95),p99:percentile(values,.99)}
})
const channelStateStats=computed(()=>[
  {name:'Healthy',value:acquisitionChannels.value.filter(c=>c.state==='Healthy').length},
  {name:'Degraded',value:acquisitionChannels.value.filter(c=>c.state==='Degraded').length},
  {name:'Interrupted',value:acquisitionChannels.value.filter(c=>c.state==='Interrupted').length},
])

const latencyChartEl=ref<HTMLElement|null>(null)
const stateChartEl=ref<HTMLElement|null>(null)
let latencyChart:echarts.ECharts|null=null
let stateChart:echarts.ECharts|null=null
let chartResizeObserver:ResizeObserver|null=null

function css(name:string,fallback:string){
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim()||fallback
}
function renderChannelCharts(){
  if(activeTab.value!=='channel')return
  latencyChart?.dispose()
  stateChart?.dispose()
  chartResizeObserver?.disconnect()
  chartResizeObserver=new ResizeObserver(()=>{
    latencyChart?.resize()
    stateChart?.resize()
  })

  if(latencyChartEl.value){
    latencyChart=echarts.init(latencyChartEl.value)
    latencyChart.setOption({
      animation:false,
      grid:{left:42,right:16,top:18,bottom:42},
      tooltip:{
        trigger:'item',
        formatter:(params:any)=>{
          const bucket=latencyBuckets.value[params.dataIndex]
          return bucket?bucket.label+' ms<br/><b>'+bucket.count+' channels</b>':''
        },
      },
      xAxis:{
        type:'category',
        data:latencyBuckets.value.map(b=>b.label),
        name:'Latency (ms)',
        nameLocation:'middle',
        nameGap:28,
        nameTextStyle:{fontSize:10,color:css('--app-text-muted','#98a2b3')},
        axisLabel:{color:css('--app-text-muted','#98a2b3'),fontSize:10,interval:0},
        axisLine:{lineStyle:{color:css('--app-border','#e5e9ef')}},
      },
      yAxis:{
        type:'value',
        minInterval:1,
        name:'Channels',
        nameTextStyle:{fontSize:10,color:css('--app-text-muted','#98a2b3')},
        axisLabel:{color:css('--app-text-muted','#98a2b3'),fontSize:10},
        splitLine:{lineStyle:{color:css('--app-border-soft','#eef0f3')}},
      },
      series:[{type:'bar',barMaxWidth:46,data:latencyBuckets.value.map(b=>b.count)}],
    })
    chartResizeObserver.observe(latencyChartEl.value)
  }

  if(stateChartEl.value){
    stateChart=echarts.init(stateChartEl.value)
    stateChart.setOption({
      animation:false,
      tooltip:{trigger:'item',formatter:'{b}: {c} ({d}%)'},
      legend:{bottom:0,left:'center',textStyle:{fontSize:10,color:css('--app-text-secondary','#77808f')}},
      series:[{
        type:'pie',
        radius:['42%','68%'],
        center:['50%','43%'],
        label:{show:true,formatter:'{c}',fontSize:11},
        data:channelStateStats.value,
      }],
    })
    chartResizeObserver.observe(stateChartEl.value)
  }
}
watch(activeTab,tab=>{if(tab==='channel')nextTick(renderChannelCharts)})
watch(checkGeneration,()=>nextTick(renderChannelCharts))

const dataWindow=ref<DataWindow>('24 h')
const dataWindowFactor=computed(()=>dataWindow.value==='1 h'?0.18:dataWindow.value==='7 d'?4.5:1)

const dataSummary=computed(()=>[
  {label:'Stale tasks',value:1,hint:'current',tone:'danger' as Tone},
  {label:'Missing cycles',value:Math.max(1,Math.round(13*dataWindowFactor.value)),hint:dataWindow.value,tone:'warning' as Tone},
  {label:'Point read failures',value:Math.max(1,Math.round(6*dataWindowFactor.value)),hint:dataWindow.value,tone:'warning' as Tone},
  {label:'Dropped points',value:0,hint:dataWindow.value,tone:'normal' as Tone},
])

const dataDimensions=computed(()=>[
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
    metric:String(Math.max(1,Math.round(13*dataWindowFactor.value)))+' missing cycles',
    detail:String(Math.max(1,Math.round(6*dataWindowFactor.value)))+' point reads failed',
    description:'判断期望采集的数据是否完整，包括 missing cycles 和 point read failures。',
    tone:'warning' as Tone,
  },
  {
    dimension:'Validity',
    status:'Degraded',
    metric:String(Math.max(1,Math.round(2*dataWindowFactor.value)))+' decode errors',
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
])

const nowTick=ref(Date.now())
const issueBase=Date.now()
let issueTimer:number|undefined

type IssueDefinition={
  object:string
  kind:'Task'|'Device'|'Sink'
  level:'Fault'|'Warning'
  dimension:string
  symptom:string
  impact:string
  startedAt:number
  evidence:Array<[string,string]>
  related:Array<[string,string]>
  checks:string[]
}

const issueDefinitions:IssueDefinition[]=[
  {
    object:'turbine-ads-all',
    kind:'Task',
    level:'Fault',
    dimension:'Continuity',
    symptom:'No fresh samples',
    impact:'24 devices · 120 points',
    startedAt:issueBase-12*60*1000-18*1000,
    evidence:[['Expected interval','1 s'],['Observed gap','12m 18s'],['Missing cycles','738'],['Affected devices','24']],
    related:[['Task','turbine-ads-all'],['Protocol','ADS'],['Point Group','realtime'],['Devices','24 affected']],
    checks:['Check whether the task is still RUNNING.','Check Acquisition Channel state for the affected devices.','Check ADS session / reconnect errors.','Read one representative configured point to separate channel failure from point mapping failure.'],
  },
  {
    object:'turbine-modbus-all',
    kind:'Task',
    level:'Warning',
    dimension:'Timeliness',
    symptom:'Jitter above baseline',
    impact:'3 missing cycles',
    startedAt:issueBase-8*60*1000,
    evidence:[['Expected interval','1 s'],['Jitter P95','87 ms'],['Worst age ratio','8.7 ×'],['Missing cycles','3']],
    related:[['Task','turbine-modbus-all'],['Protocol','Modbus TCP'],['Point Group','realtime'],['Scope','group task']],
    checks:['Compare actual interval with configured interval.','Check Acquisition latency distribution for outliers.','Check task overrun / missed tick counters.','If all devices degrade together, check System Health for CPU or event-loop pressure.'],
  },
  {
    object:store.devices.filter(d=>d.enabled)[2]?.device_id||'wtg-025',
    kind:'Device',
    level:'Warning',
    dimension:'Completeness',
    symptom:'3 point reads missing',
    impact:'3 points',
    startedAt:issueBase-3*60*1000-40*1000,
    evidence:[['Expected points','120'],['Read success','117'],['Read failed','3'],['Channel state','Healthy']],
    related:[['Device',store.devices.filter(d=>d.enabled)[2]?.device_id||'wtg-025'],['Point Table','resolved device table'],['Protocol',store.devices.filter(d=>d.enabled)[2]?protocolOfDevice(store.devices.filter(d=>d.enabled)[2]):'unknown'],['Failed points','3']],
    checks:['Confirm the device channel is healthy.','Inspect failed Point IDs and their configured address / symbol.','Run configured Point Read Test for one failed point.','Compare raw data type with the Point definition if decode fails.'],
  },
  {
    object:'file_archive',
    kind:'Sink',
    level:'Warning',
    dimension:'Delivery Integrity',
    symptom:'Queue depth increasing',
    impact:'2 queued batches',
    startedAt:issueBase-2*60*1000,
    evidence:[['Dropped points','0'],['Queued batches','2'],['Recent write errors','1'],['Growth','increasing']],
    related:[['Sink','file_archive'],['Type','File'],['Queue','2 batches'],['Data loss','None observed']],
    checks:['Check whether the sink worker is consuming the queue.','Check target filesystem capacity and write errors.','Compare enqueue rate with sink write throughput.','If storage is constrained, continue in System Health to inspect mount capacity.'],
  },
]
function formatDuration(ms:number){
  const totalSeconds=Math.max(0,Math.floor(ms/1000))
  const hours=Math.floor(totalSeconds/3600)
  const minutes=Math.floor((totalSeconds%3600)/60)
  const seconds=totalSeconds%60
  if(hours)return hours+'h '+minutes+'m'
  if(minutes)return minutes+'m '+seconds+'s'
  return seconds+'s'
}
const dataIssues=computed(()=>issueDefinitions.map(issue=>({
  ...issue,
  duration:formatDuration(nowTick.value-issue.startedAt),
})))

const issueDrawerOpen=ref(false)
const selectedIssue=ref<(IssueDefinition&{duration:string})|null>(null)
function openIssue(issue:IssueDefinition&{duration:string}){
  selectedIssue.value=issue
  issueDrawerOpen.value=true
}

function tagType(tone:Tone){return tone==='danger'?'danger':tone==='warning'?'warning':'success'}
function channelTagType(state:string){return state==='Interrupted'?'danger':state==='Degraded'?'warning':state==='Healthy'?'success':'info'}

onMounted(()=>{
  nextTick(renderChannelCharts)
  issueTimer=window.setInterval(()=>{nowTick.value=Date.now()},1000)
})
onBeforeUnmount(()=>{
  window.removeEventListener('resize',updateViewport)
  if(autoCheckTimer)window.clearInterval(autoCheckTimer)
  if(countdownTimer)window.clearInterval(countdownTimer)
  if(checkCompletionTimer)window.clearTimeout(checkCompletionTimer)
  if(issueTimer)window.clearInterval(issueTimer)
  chartResizeObserver?.disconnect()
  latencyChart?.dispose()
  stateChart?.dispose()
})
</script>

<template>
  <div class="standard-page quality-page">
    <div class="head">
      <div>
        <h1>Quality</h1>
        <p>区分通信链路质量与最终数据质量，回答“发生了什么、影响多大”；根因定位由 Diagnostics 负责。</p>
      </div>
    </div>

    <el-tabs v-model="activeTab" class="quality-tabs">
      <el-tab-pane label="Channel Quality" name="channel">
        <div class="quality-definition channel-definition">
          <div>
            <b>Channel Quality</b>
            <span>wind-hub 与外部端点之间的通信链路质量，包括网络 Sink 交付侧和设备采集侧。</span>
          </div>
          <div class="quality-check-controls">
            <el-button type="primary" :disabled="checking" @click="performCheck">Check</el-button>
            <label class="auto-check-control"><span>Auto Check</span><el-switch v-model="autoCheck"/></label>
            <el-select v-model="checkInterval" class="check-interval" aria-label="Check interval">
              <el-option v-for="option in checkIntervalOptions" :key="option.value" :label="option.label" :value="option.value"/>
            </el-select>
            <div class="check-status">
              <span>{{checking?'Checking…':'Last checked '+lastChecked}}</span>
              <small v-if="autoCheck&&nextCheckIn!==null">Next check in {{nextCheckIn}} s</small>
            </div>
          </div>
        </div>

        <div class="quality-metric-grid">
          <el-card v-for="m in channelSummary" :key="m.label" shadow="never">
            <span class="metric-label">
              {{m.label}}
              <el-tooltip v-if="'description' in m && m.description" placement="top" effect="dark" :show-after="200" :teleported="true">
                <template #content><div class="metric-info-tooltip">{{m.description}}</div></template>
                <el-icon class="info-icon" :aria-label="m.label+' definition'"><InfoFilled/></el-icon>
              </el-tooltip>
            </span>
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
          <div class="section-title"><div><h2>Acquisition Channels</h2><p>Device / PLC / EMS → wind-hub。图表统计全部有效采集信道，不受表格分页影响。</p></div></div>

          <div class="channel-chart-grid">
            <el-card shadow="never" class="latency-card">
              <div class="chart-card-head">
                <div><b>Latency Distribution</b><span>采集通信 latency 的整体分布。</span></div>
                <div class="latency-stats">
                  <div><span>P50</span><b>{{latencyStats.p50}} ms</b></div>
                  <div><span>P95</span><b>{{latencyStats.p95}} ms</b></div>
                  <div><span>P99</span><b>{{latencyStats.p99}} ms</b></div>
                </div>
              </div>
              <div ref="latencyChartEl" class="channel-chart"/>
            </el-card>

            <el-card shadow="never" class="state-card">
              <div class="chart-card-head">
                <div><b>Channel State</b><span>当前采集信道状态构成。</span></div>
              </div>
              <div ref="stateChartEl" class="channel-chart"/>
            </el-card>
          </div>

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
        <div class="quality-definition data-definition">
          <div><b>Data Quality</b><span>衡量期望数据流与实际可用数据流之间的偏差，不评价风速、功率等业务值是否合理。</span></div>
          <el-segmented v-model="dataWindow" :options="['1 h','24 h','7 d']"/>
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
              <span class="dimension-window">Window · {{dataWindow}}</span>
            </el-card>
          </div>
        </section>

        <section class="quality-section">
          <div class="section-title"><div><h2>Active Data Issues</h2><p>只展示会影响最终可用数据的当前异常；持续时间自动更新。</p></div></div>
          <el-card shadow="never">
            <el-table :data="dataIssues">
              <el-table-column label="Object" min-width="155">
                <template #default="{row}">
                  <el-button link type="primary" class="issue-object-link" @click="openIssue(row)">{{row.object}}</el-button>
                </template>
              </el-table-column>
              <el-table-column label="Level" width="95"><template #default="{row}"><el-tag :type="row.level==='Fault'?'danger':'warning'" size="small">{{row.level}}</el-tag></template></el-table-column>
              <el-table-column prop="dimension" label="Dimension" min-width="130"/>
              <el-table-column prop="symptom" label="Symptom" min-width="180"/>
              <el-table-column v-if="!isMobile" prop="impact" label="Impact" min-width="160"/>
              <el-table-column prop="duration" label="Duration" min-width="100"/>
            </el-table>
          </el-card>
        </section>
      </el-tab-pane>
    </el-tabs>

    <el-drawer v-model="issueDrawerOpen" title="Issue Detail" :size="isMobile?'100%':'72%'">
      <template v-if="selectedIssue">
        <div class="issue-drawer-head">
          <div>
            <div class="issue-title-row">
              <b>{{selectedIssue.object}}</b>
              <el-tag :type="selectedIssue.level==='Fault'?'danger':'warning'" size="small">{{selectedIssue.level}}</el-tag>
            </div>
            <p>{{selectedIssue.symptom}}</p>
          </div>
          <span class="issue-duration">{{selectedIssue.duration}}</span>
        </div>

        <div class="issue-overview-grid">
          <div><span>Object Type</span><b>{{selectedIssue.kind}}</b></div>
          <div><span>Dimension</span><b>{{selectedIssue.dimension}}</b></div>
          <div><span>Impact</span><b>{{selectedIssue.impact}}</b></div>
          <div><span>Duration</span><b>{{selectedIssue.duration}}</b></div>
        </div>

        <el-divider content-position="left">Evidence</el-divider>
        <el-descriptions :column="isMobile?1:2" border size="small">
          <el-descriptions-item v-for="item in selectedIssue.evidence" :key="item[0]" :label="item[0]">{{item[1]}}</el-descriptions-item>
        </el-descriptions>

        <el-divider content-position="left">Related Objects</el-divider>
        <el-descriptions :column="isMobile?1:2" border size="small">
          <el-descriptions-item v-for="item in selectedIssue.related" :key="item[0]" :label="item[0]">{{item[1]}}</el-descriptions-item>
        </el-descriptions>

        <el-divider content-position="left">Suggested Checks</el-divider>
        <el-steps direction="vertical" :active="-1" class="issue-check-steps">
          <el-step v-for="(check,index) in selectedIssue.checks" :key="check" :title="String(index+1)+'. '+check"/>
        </el-steps>
        <p class="issue-guidance">这些步骤用于缩小问题范围；真正的协议连通、点读取、原始数据等操作仍在 Diagnostics 中执行。</p>
      </template>
    </el-drawer>
  </div>
</template>

<style scoped>
.quality-tabs{margin-top:var(--app-space-4)}.quality-definition{display:flex;align-items:center;justify-content:space-between;gap:var(--app-space-4);margin-bottom:var(--app-space-4);padding:var(--app-space-3) 0}.quality-definition>div:first-child{display:grid;gap:4px}.quality-definition b{font-size:var(--app-font-panel-title);font-weight:var(--app-font-weight-semibold)}.quality-definition span{color:var(--app-text-muted);font-size:var(--app-font-caption)}
.quality-check-controls{display:flex;align-items:center;justify-content:flex-end;gap:var(--app-space-3);flex-wrap:wrap}.auto-check-control{display:inline-flex;align-items:center;gap:var(--app-space-2);color:var(--app-text-secondary);font-size:var(--app-font-label);white-space:nowrap}.check-interval{width:92px}.check-status{display:grid;gap:2px;min-width:128px}.check-status span,.check-status small{color:var(--app-text-muted);font-size:var(--app-font-caption);font-variant-numeric:tabular-nums;white-space:nowrap}
.quality-metric-grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:var(--app-space-3);margin-bottom:var(--app-space-6)}.quality-metric-grid .el-card span,.quality-metric-grid .el-card small{color:var(--app-text-muted);font-size:var(--app-font-caption)}.quality-metric-grid .el-card>div>div{display:flex;align-items:center;justify-content:space-between;gap:var(--app-space-2);margin:var(--app-space-2) 0}.quality-metric-grid b{color:var(--app-text-primary);font-size:var(--app-font-metric);font-weight:var(--app-font-weight-semibold)}.metric-label{display:inline-flex;align-items:center;gap:5px}.metric-info-tooltip{max-width:360px;line-height:1.55}
.quality-section{margin-bottom:var(--app-space-6)}.section-title{margin-bottom:var(--app-space-3)}.section-title h2{margin:0;font-size:var(--app-font-section-title);font-weight:var(--app-font-weight-semibold)}.section-title p{margin:4px 0 0;color:var(--app-text-muted);font-size:var(--app-font-caption)}
.channel-chart-grid{display:grid;grid-template-columns:minmax(0,2fr) minmax(280px,1fr);gap:var(--app-space-4);margin-bottom:var(--app-space-3)}.chart-card-head{display:flex;align-items:flex-start;justify-content:space-between;gap:var(--app-space-4)}.chart-card-head>div:first-child{display:grid;gap:4px}.chart-card-head>div:first-child>b{font-size:var(--app-font-panel-title)}.chart-card-head>div:first-child>span{color:var(--app-text-muted);font-size:var(--app-font-caption)}.latency-stats{display:flex;align-items:center;gap:var(--app-space-5)}.latency-stats>div{display:grid;gap:2px;text-align:right}.latency-stats span{color:var(--app-text-muted);font-size:var(--app-font-caption)}.latency-stats b{font-size:var(--app-font-body);font-weight:var(--app-font-weight-semibold);font-variant-numeric:tabular-nums}.channel-chart{height:250px;margin-top:var(--app-space-2)}
.acquisition-toolbar{display:flex;align-items:center;justify-content:space-between;gap:var(--app-space-3);margin:var(--app-space-3) 0}.acquisition-toolbar>span{color:var(--app-text-muted);font-size:var(--app-font-caption)}.acquisition-pagination{display:flex;justify-content:flex-end;padding-top:var(--app-space-3)}
.dimension-grid{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:var(--app-space-3)}.dimension-card{min-width:0}.dimension-card-head{display:flex;align-items:center;justify-content:space-between;gap:var(--app-space-2)}.dimension-name{display:inline-flex;align-items:center;gap:5px;min-width:0}.dimension-name b{font-size:var(--app-font-panel-title);font-weight:var(--app-font-weight-semibold)}.info-icon{flex:0 0 auto;color:var(--app-text-muted);font-size:var(--app-font-panel-title);cursor:help;transition:color .15s ease}.info-icon:hover{color:var(--app-text-secondary)}.dimension-card strong{display:block;margin-top:var(--app-space-4);font-size:var(--app-font-panel-title);font-weight:var(--app-font-weight-semibold)}.dimension-detail{display:block;margin-top:4px;color:var(--app-text-muted);font-size:var(--app-font-caption);line-height:var(--app-line-height-body)}.dimension-window{display:block;margin-top:var(--app-space-3);padding-top:var(--app-space-2);border-top:1px solid var(--app-border-soft);color:var(--app-text-muted);font-size:var(--app-font-caption)}.dimension-tooltip{display:grid;gap:5px;max-width:300px;line-height:1.5}.dimension-tooltip b{font-weight:var(--app-font-weight-semibold)}
.issue-object-link{padding:0;font-size:var(--app-font-body);font-weight:var(--app-font-weight-medium)}.issue-drawer-head{display:flex;align-items:flex-start;justify-content:space-between;gap:var(--app-space-4);margin-bottom:var(--app-space-4)}.issue-title-row{display:flex;align-items:center;gap:var(--app-space-2)}.issue-title-row b{font-size:var(--app-font-section-title)}.issue-drawer-head p{margin:5px 0 0;color:var(--app-text-secondary);font-size:var(--app-font-body)}.issue-duration{color:var(--app-text-muted);font-size:var(--app-font-caption);font-variant-numeric:tabular-nums;white-space:nowrap}.issue-overview-grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:var(--app-space-3)}.issue-overview-grid>div{padding:var(--app-space-3);border:1px solid var(--app-border-soft);border-radius:var(--app-panel-radius);min-width:0}.issue-overview-grid span{display:block;margin-bottom:5px;color:var(--app-text-muted);font-size:var(--app-font-caption)}.issue-overview-grid b{display:block;color:var(--app-text-primary);font-size:var(--app-font-body);font-weight:var(--app-font-weight-semibold);overflow-wrap:anywhere}.issue-check-steps{margin-top:var(--app-space-2)}.issue-guidance{margin:var(--app-space-3) 0 0;color:var(--app-text-muted);font-size:var(--app-font-caption);line-height:var(--app-line-height-body)}
@media(max-width:1399px){.dimension-grid{grid-template-columns:repeat(3,minmax(0,1fr))}}
@media(max-width:1199px){.quality-metric-grid{grid-template-columns:repeat(2,minmax(0,1fr))}.channel-definition{align-items:flex-start;flex-direction:column}.quality-check-controls{justify-content:flex-start}.channel-chart-grid{grid-template-columns:1fr}.dimension-grid{grid-template-columns:repeat(2,minmax(0,1fr))}.issue-overview-grid{grid-template-columns:repeat(2,minmax(0,1fr))}}
@media(max-width:767px){.quality-definition{align-items:flex-start;flex-direction:column}.quality-metric-grid,.dimension-grid{grid-template-columns:1fr}.quality-check-controls{width:100%;justify-content:flex-start}.check-status{width:100%}.chart-card-head{flex-direction:column}.latency-stats{width:100%;justify-content:space-between;gap:var(--app-space-3)}.latency-stats>div{text-align:left}.channel-chart{height:220px}.acquisition-pagination{justify-content:center;overflow-x:auto}.issue-overview-grid{grid-template-columns:1fr}.issue-drawer-head{flex-direction:column}.issue-duration{white-space:normal}}
</style>
