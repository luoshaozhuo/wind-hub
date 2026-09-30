<script setup lang="ts">
import * as echarts from 'echarts'
import { InfoFilled } from '@element-plus/icons-vue'
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useViewport } from '../composables/useViewport'
import { pointsOfTable, protocolOfDevice, store, tableOfDevice } from '../mock/data'

type Tone='normal'|'warning'|'danger'
type ChannelState='Healthy'|'Degraded'|'Interrupted'
type DataWindow='1 h'|'24 h'|'7 d'
type DetailLevel='Normal'|'Warning'|'Fault'

interface ChannelRow {
  object:string
  source:'Acquisition'|'Delivery'
  protocol:string
  state:string
  target:string
  last:string
  latency:string
  latencyMs:number|null
  timeouts:number
  reconnects:number
  issue:string
}

interface DetailModel {
  kind:'channel'|'data'
  title:string
  subtitle:string
  level:DetailLevel
  dimension:string
  impact:string
  duration:string
  location:Array<[string,string]>
  evidence:Array<[string,string]>
  affected:Array<Record<string,string|number>>
  checks:string[]
}

const activeTab=ref<'channel'|'data'>('channel')
const detail=ref<DetailModel|null>(null)
const { isMobile, isTablet }=useViewport()

const checking=ref(false)
const autoCheck=ref(false)
const checkInterval=ref(30)
const checkGeneration=ref(0)
const lastChecked=ref('Never')
const nextCheckIn=ref<number|null>(null)
const checkIntervalOptions=[
  {label:'5 s',value:5},{label:'10 s',value:10},{label:'30 s',value:30},
  {label:'1 min',value:60},{label:'5 min',value:300},
]
let autoTimer:number|undefined
let countdownTimer:number|undefined
let checkTimer:number|undefined
let issueClock:number|undefined

function performCheck(){
  if(checking.value)return
  checking.value=true
  if(checkTimer)window.clearTimeout(checkTimer)
  checkTimer=window.setTimeout(()=>{
    checkGeneration.value+=1
    checking.value=false
    lastChecked.value=new Date().toLocaleTimeString([],{hour:'2-digit',minute:'2-digit',second:'2-digit'})
    if(autoCheck.value)nextCheckIn.value=checkInterval.value
  },700)
}
function restartAuto(){
  if(autoTimer)window.clearInterval(autoTimer)
  if(countdownTimer)window.clearInterval(countdownTimer)
  autoTimer=undefined
  countdownTimer=undefined
  nextCheckIn.value=null
  if(!autoCheck.value)return
  nextCheckIn.value=checkInterval.value
  autoTimer=window.setInterval(performCheck,checkInterval.value*1000)
  countdownTimer=window.setInterval(()=>{
    if(nextCheckIn.value===null)return
    nextCheckIn.value=nextCheckIn.value<=1?checkInterval.value:nextCheckIn.value-1
  },1000)
}
watch([autoCheck,checkInterval],restartAuto)

const latencyPattern=[12,18,24,31,38,46,57,69,82,96,118,146,188,245,360,540]
const acquisitionChannels=computed<ChannelRow[]>(()=>store.devices.filter(d=>d.enabled).map((d,index)=>{
  const wave=(checkGeneration.value*7+index*3)%19-9
  const base=latencyPattern[index%latencyPattern.length]+Math.floor(index/latencyPattern.length)*4
  const latencyMs=index===0&&checkGeneration.value%3===0?null:Math.max(4,base+wave)
  const state:ChannelState=latencyMs===null?'Interrupted':latencyMs>=180||((index+checkGeneration.value)%11===0)?'Degraded':'Healthy'
  return {
    object:d.device_id,
    source:'Acquisition',
    protocol:protocolOfDevice(d),
    state,
    target:d.host,
    last:state==='Interrupted'?'No recent success':String(Math.max(15,Math.round((latencyMs??0)*2.5)))+' ms ago',
    latency:latencyMs===null?'—':String(latencyMs)+' ms',
    latencyMs,
    timeouts:state==='Interrupted'?8:state==='Degraded'?((index+checkGeneration.value)%4)+1:0,
    reconnects:state==='Interrupted'?3:state==='Degraded'?((index+checkGeneration.value)%3):0,
    issue:state==='Interrupted'?'Protocol session unavailable':state==='Degraded'?(latencyMs!==null&&latencyMs>=180?'High acquisition latency':'Reconnect burst'):'—',
  }
}))

const deliveryChannels=computed<ChannelRow[]>(()=>store.sinks.filter(s=>s.type!=='file').map((s,index)=>{
  const base=s.latency_ms??18
  const latency=Math.max(3,base+((checkGeneration.value+index*2)%7)-3)
  const state=s.enabled?(s.runtime_state==='failed'?'Interrupted':s.runtime_state==='healthy'?'Healthy':'Degraded'):'Disabled'
  return {
    object:s.name,
    source:'Delivery',
    protocol:s.type==='db'?'PostgreSQL':s.type==='redis'?'Redis':'Kafka',
    state,
    target:String(s.params.bootstrap_servers||s.params.dsn||'configured target'),
    last:lastChecked.value==='Never'?(s.verification.checked_at||'Never'):lastChecked.value,
    latency:s.enabled?String(latency)+' ms':'—',
    latencyMs:s.enabled?latency:null,
    timeouts:s.name==='kafka_main'?checkGeneration.value%3:0,
    reconnects:s.name==='kafka_main'?checkGeneration.value%2:0,
    issue:s.enabled?(s.error||'—'):'Sink disabled',
  }
}))

const channelSummary=computed(()=>{
  const all=[...acquisitionChannels.value,...deliveryChannels.value]
  const interrupted=all.filter(x=>x.state==='Interrupted').length
  const timeout=all.reduce((n,x)=>n+x.timeouts,0)
  const reconnect=all.reduce((n,x)=>n+x.reconnects,0)
  const degraded=all.filter(x=>x.state==='Degraded').length
  return [
    {key:'interruptions',label:'Active Interruptions',value:interrupted,hint:'acquisition + delivery',tone:(interrupted?'danger':'normal') as Tone},
    {key:'timeouts',label:'Timeouts',value:timeout,hint:'current check snapshot',tone:(timeout?'warning':'normal') as Tone},
    {key:'reconnects',label:'Reconnects',value:reconnect,hint:'current check snapshot',tone:(reconnect?'warning':'normal') as Tone},
    {key:'degraded',label:'Degraded Channels',value:degraded,hint:'current check snapshot',tone:(degraded?'warning':'normal') as Tone},
  ]
})

const acquisitionFilter=ref<'all'|'healthy'|'degraded'|'interrupted'>('all')
const acquisitionPage=ref(1)
const acquisitionPageSize=ref(20)
const filteredAcquisition=computed(()=>{
  if(acquisitionFilter.value==='all')return acquisitionChannels.value
  const expected=acquisitionFilter.value==='healthy'?'Healthy':acquisitionFilter.value==='degraded'?'Degraded':'Interrupted'
  return acquisitionChannels.value.filter(x=>x.state===expected)
})
const pagedAcquisition=computed(()=>{
  const start=(acquisitionPage.value-1)*acquisitionPageSize.value
  return filteredAcquisition.value.slice(start,start+acquisitionPageSize.value)
})
watch([acquisitionFilter,acquisitionPageSize],()=>{acquisitionPage.value=1})

const buckets=[
  {label:'0–10',min:0,max:10},{label:'10–20',min:10,max:20},{label:'20–30',min:20,max:30},
  {label:'30–50',min:30,max:50},{label:'50–75',min:50,max:75},{label:'75–100',min:75,max:100},
  {label:'100–150',min:100,max:150},{label:'150–200',min:150,max:200},{label:'200–300',min:200,max:300},
  {label:'300–500',min:300,max:500},{label:'500+',min:500,max:Number.POSITIVE_INFINITY},
]
const latencyBuckets=computed(()=>buckets.map(b=>({
  ...b,
  rows:acquisitionChannels.value.filter(x=>x.latencyMs!==null&&x.latencyMs>=b.min&&x.latencyMs<b.max),
})))
function percentile(values:number[],q:number){
  if(!values.length)return 0
  const sorted=[...values].sort((a,b)=>a-b)
  const pos=(sorted.length-1)*q
  const lo=Math.floor(pos),hi=Math.ceil(pos)
  return lo===hi?sorted[lo]:Math.round(sorted[lo]+(sorted[hi]-sorted[lo])*(pos-lo))
}
const latencyStats=computed(()=>{
  const values=acquisitionChannels.value.flatMap(x=>x.latencyMs===null?[]:[x.latencyMs])
  return {p50:percentile(values,.5),p95:percentile(values,.95),p99:percentile(values,.99)}
})
const channelStateStats=computed(()=>[
  {name:'Healthy',value:acquisitionChannels.value.filter(x=>x.state==='Healthy').length},
  {name:'Degraded',value:acquisitionChannels.value.filter(x=>x.state==='Degraded').length},
  {name:'Interrupted',value:acquisitionChannels.value.filter(x=>x.state==='Interrupted').length},
])

const latencyChartEl=ref<HTMLElement|null>(null)
const stateChartEl=ref<HTMLElement|null>(null)
let latencyChart:echarts.ECharts|null=null
let stateChart:echarts.ECharts|null=null
let resizeObserver:ResizeObserver|null=null
function css(name:string,fallback:string){return getComputedStyle(document.documentElement).getPropertyValue(name).trim()||fallback}

function renderCharts(){
  if(activeTab.value!=='channel')return
  latencyChart?.dispose()
  stateChart?.dispose()
  resizeObserver?.disconnect()
  resizeObserver=new ResizeObserver(()=>{latencyChart?.resize();stateChart?.resize()})

  if(latencyChartEl.value){
    latencyChart=echarts.init(latencyChartEl.value)
    latencyChart.setOption({
      animation:false,
      grid:{left:42,right:16,top:18,bottom:42},
      tooltip:{trigger:'item',formatter:(p:any)=>{const b=latencyBuckets.value[p.dataIndex];return b?b.label+' ms<br/><b>'+b.rows.length+' channels</b>':''}},
      xAxis:{type:'category',data:latencyBuckets.value.map(x=>x.label),name:'Latency (ms)',nameLocation:'middle',nameGap:28,axisLabel:{fontSize:10,color:css('--app-text-muted','#98a2b3'),interval:0}},
      yAxis:{type:'value',minInterval:1,name:'Channels',axisLabel:{fontSize:10,color:css('--app-text-muted','#98a2b3')},splitLine:{lineStyle:{color:css('--app-border-soft','#eef0f3')}}},
      series:[{type:'bar',barMaxWidth:46,data:latencyBuckets.value.map(x=>x.rows.length)}],
    })
    latencyChart.on('click',(p:any)=>openLatencyBucket(p.dataIndex))
    resizeObserver.observe(latencyChartEl.value)
  }

  if(stateChartEl.value){
    stateChart=echarts.init(stateChartEl.value)
    stateChart.setOption({
      animation:false,
      tooltip:{trigger:'item',formatter:'{b}: {c} ({d}%)'},
      legend:{bottom:0,left:'center',textStyle:{fontSize:10,color:css('--app-text-secondary','#77808f')}},
      series:[{type:'pie',radius:['42%','68%'],center:['50%','43%'],label:{show:true,formatter:'{c}',fontSize:11},data:channelStateStats.value}],
    })
    stateChart.on('click',(p:any)=>openChannelState(String(p.name)))
    resizeObserver.observe(stateChartEl.value)
  }
}
watch(activeTab,tab=>{if(tab==='channel')nextTick(renderCharts)})
watch(checkGeneration,()=>nextTick(renderCharts))

const dataWindow=ref<DataWindow>('24 h')
const windowFactor=computed(()=>dataWindow.value==='1 h'?0.18:dataWindow.value==='7 d'?4.5:1)
const dataSummary=computed(()=>[
  {key:'stale',label:'Stale Tasks',value:1,hint:'current',tone:'danger' as Tone},
  {key:'missing',label:'Missing Cycles',value:Math.max(1,Math.round(13*windowFactor.value)),hint:dataWindow.value,tone:'warning' as Tone},
  {key:'reads',label:'Point Read Failures',value:Math.max(1,Math.round(6*windowFactor.value)),hint:dataWindow.value,tone:'warning' as Tone},
  {key:'dropped',label:'Dropped Points',value:0,hint:dataWindow.value,tone:'normal' as Tone},
])
const dimensions=computed(()=>[
  {dimension:'Continuity',status:'Failed',metric:'1 stale task',detail:'Longest active gap 12m 18s',description:'判断期望采集周期是否出现连续中断、长时间无新数据或采集任务停滞。',tone:'danger' as Tone},
  {dimension:'Timeliness',status:'Degraded',metric:'Jitter P95 87 ms',detail:'Worst task 8.7 × expected age',description:'判断数据是否在期望时间内到达，包括 freshness、采集延迟和 jitter。',tone:'warning' as Tone},
  {dimension:'Completeness',status:'Degraded',metric:Math.max(1,Math.round(13*windowFactor.value))+' missing cycles',detail:Math.max(1,Math.round(6*windowFactor.value))+' point reads failed',description:'判断期望采集的数据是否完整，包括 missing cycles 和 point read failures。',tone:'warning' as Tone},
  {dimension:'Validity',status:'Degraded',metric:Math.max(1,Math.round(2*windowFactor.value))+' decode errors',detail:'No timestamp/order errors',description:'判断采集数据能否被正确解析和使用，包括 decode、timestamp、ordering 等技术有效性。',tone:'warning' as Tone},
  {dimension:'Delivery Integrity',status:'Normal',metric:'0 dropped points',detail:'File sink backlog 2',description:'判断 wind-hub 已采集的数据是否完整交付到目标 Sink，包括 drop、queue backlog、write failure。',tone:'normal' as Tone},
])

const nowTick=ref(Date.now())
const issueBase=Date.now()
const issueDefs=[
  {object:'turbine-ads-all',kind:'Task',level:'Fault' as DetailLevel,dimension:'Continuity',symptom:'No fresh samples',impact:'24 devices · 120 points',startedAt:issueBase-12*60*1000-18*1000},
  {object:'turbine-modbus-all',kind:'Task',level:'Warning' as DetailLevel,dimension:'Timeliness',symptom:'Jitter above baseline',impact:'3 missing cycles',startedAt:issueBase-8*60*1000},
  {object:store.devices[2]?.device_id||'wtg-003',kind:'Device',level:'Warning' as DetailLevel,dimension:'Completeness',symptom:'3 point reads missing',impact:'3 points',startedAt:issueBase-3*60*1000-40*1000},
  {object:'file_archive',kind:'Sink',level:'Warning' as DetailLevel,dimension:'Delivery Integrity',symptom:'Queue depth increasing',impact:'2 queued batches',startedAt:issueBase-2*60*1000},
]
function formatDuration(ms:number){
  const total=Math.max(0,Math.floor(ms/1000))
  const h=Math.floor(total/3600),m=Math.floor((total%3600)/60),s=total%60
  return h?h+'h '+m+'m':m?m+'m '+s+'s':s+'s'
}
const dataIssues=computed(()=>issueDefs.map(x=>({...x,duration:formatDuration(nowTick.value-x.startedAt)})))

const affectedPage=ref(1)
const affectedPageSize=ref(10)
const pagedAffected=computed(()=>{
  if(!detail.value)return []
  const start=(affectedPage.value-1)*affectedPageSize.value
  return detail.value.affected.slice(start,start+affectedPageSize.value)
})
const affectedColumns=computed(()=>detail.value?.affected[0]?Object.keys(detail.value.affected[0]):[])
const logs=computed(()=>detail.value?Array.from({length:20},(_,i)=>({
  time:new Date(Date.now()-i*41000).toLocaleTimeString([],{hour:'2-digit',minute:'2-digit',second:'2-digit'}),
  level:i%7===0?'ERROR':i%3===0?'WARN':'INFO',
  source:detail.value?.kind==='channel'?'protocol':detail.value?.dimension==='Delivery Integrity'?'sink':'quality',
  object:detail.value?.title||'—',
  message:i%7===0?'Operation failed; retry scheduled':i%3===0?'Observed metric exceeded quality baseline':'Related operation completed',
})):[])

function levelFromTone(tone:Tone):DetailLevel{return tone==='danger'?'Fault':tone==='warning'?'Warning':'Normal'}
function showDetail(model:DetailModel){
  detail.value=model
  affectedPage.value=1
}
function closeDetail(){
  detail.value=null
}

function openChannelRow(row:ChannelRow){
  const device=store.devices.find(d=>d.device_id===row.object)
  const relatedTasks=device?store.tasks.filter(t=>t.device===device.device_id||t.device_group===device.device_group).map(t=>t.task_id).join(', '):'—'
  showDetail({
    kind:'channel',
    title:row.object,
    subtitle:row.issue==='—'?'Channel quality snapshot':row.issue,
    level:row.state==='Interrupted'?'Fault':row.state==='Degraded'?'Warning':'Normal',
    dimension:'Channel Quality',
    impact:row.source+' channel',
    duration:row.state==='Healthy'?'Current snapshot':'18m 42s',
    location:[
      ['Stage',row.source],['Protocol',row.protocol],['Target',row.target],
      ['Point Table',device?tableOfDevice(device):'—'],['Related Tasks',relatedTasks||'—'],
    ],
    evidence:[
      ['State',row.state],['Latency',row.latency],['Timeouts',String(row.timeouts)],
      ['Reconnects',String(row.reconnects)],['Last Success / Check',row.last],
    ],
    affected:[{Object:row.object,Type:row.source,Protocol:row.protocol,State:row.state,Latency:row.latency}],
    checks:row.source==='Acquisition'
      ?['Verify host reachability from the Wind Hub runtime host.','Probe the configured protocol port.','Establish the protocol session.','Read one representative configured Point.']
      :['Resolve the configured target and probe TCP connectivity.','Verify protocol session / authentication.','Check target-specific metadata and delivery queue.'],
  })
}

function openChannelSummary(m:any){
  const all=[...acquisitionChannels.value,...deliveryChannels.value]
  const affected=m.key==='interruptions'?all.filter(x=>x.state==='Interrupted')
    :m.key==='timeouts'?all.filter(x=>x.timeouts>0)
    :m.key==='reconnects'?all.filter(x=>x.reconnects>0)
    :all.filter(x=>x.state==='Degraded')
  showDetail({
    kind:'channel',title:m.label,subtitle:'Channel quality aggregate',level:levelFromTone(m.tone),
    dimension:'Channel Quality',impact:affected.length+' affected channels',duration:'Current snapshot',
    location:[['Scope','Acquisition + Delivery'],['Metric',m.label],['Last Check',lastChecked.value]],
    evidence:[['Value',String(m.value)],['Affected channels',String(affected.length)],['Snapshot',lastChecked.value]],
    affected:affected.map(x=>({Object:x.object,Type:x.source,Protocol:x.protocol,State:x.state,Latency:x.latency,Timeouts:x.timeouts,Reconnects:x.reconnects})),
    checks:['Group affected channels by protocol and network segment.','Inspect one representative channel in detail.','Use Diagnostics on a representative target after the common scope is clear.'],
  })
}

function openLatencyBucket(index:number){
  const b=latencyBuckets.value[index]
  if(!b)return
  showDetail({
    kind:'channel',title:b.label+' ms Latency Bucket',subtitle:'Acquisition latency distribution detail',
    level:b.min>=200?'Warning':'Normal',dimension:'Channel Quality',impact:b.rows.length+' channels',duration:'Current snapshot',
    location:[['Stage','Acquisition'],['Metric','Latency'],['Bucket',b.label+' ms']],
    evidence:[['Channels',String(b.rows.length)],['P50',latencyStats.value.p50+' ms'],['P95',latencyStats.value.p95+' ms'],['P99',latencyStats.value.p99+' ms']],
    affected:b.rows.map(x=>({Object:x.object,Protocol:x.protocol,State:x.state,Latency:x.latency})),
    checks:['Review high-latency channels for common network segments.','Compare timeout and reconnect counters.','Use Diagnostics on one outlier.'],
  })
}

function openChannelState(state:string){
  const rows=acquisitionChannels.value.filter(x=>x.state===state)
  showDetail({
    kind:'channel',title:state+' Acquisition Channels',subtitle:'Acquisition channel state group',
    level:state==='Interrupted'?'Fault':state==='Degraded'?'Warning':'Normal',
    dimension:'Channel Quality',impact:rows.length+' channels',duration:'Current snapshot',
    location:[['Stage','Acquisition'],['State',state],['Scope','All enabled acquisition channels']],
    evidence:[['Channels',String(rows.length)],['P50',latencyStats.value.p50+' ms'],['P95',latencyStats.value.p95+' ms']],
    affected:rows.map(x=>({Object:x.object,Protocol:x.protocol,State:x.state,Latency:x.latency,Issue:x.issue})),
    checks:['Review the affected channels by protocol and device group.','Compare state with latency distribution.','Use Diagnostics on one representative device.'],
  })
}

function affectedForDimension(dimension:string){
  if(dimension==='Continuity')return store.devices.slice(24,48).map(x=>({Object:x.device_id,Type:'Device',Stage:'Acquisition',Issue:'No fresh samples'}))
  if(dimension==='Timeliness')return store.tasks.map(x=>({Object:x.task_id,Type:'Task',Stage:'Scheduling',Issue:'Timing / jitter'}))
  if(dimension==='Completeness')return store.devices.slice(2,10).map(x=>({Object:x.device_id,Type:'Device',Stage:'Point Read',Issue:'Missing point reads'}))
  if(dimension==='Validity')return pointsOfTable('beckhoff_wtg_v1').slice(0,8).map(x=>({Object:x.point_id,Type:'Point',Stage:'Decode',Issue:'Decode mismatch'}))
  return store.sinks.map(x=>({Object:x.name,Type:'Sink',Stage:'Delivery',Issue:'Delivery integrity'}))
}
function checksForDimension(dimension:string){
  const map:Record<string,string[]>={
    Continuity:['Check the affected Task runtime state.','Compare affected devices to Acquisition Channel state.','Inspect protocol/session errors for one representative device.'],
    Timeliness:['Compare expected and actual task interval.','Review acquisition latency and task overrun counters.','If multiple protocols degrade together, inspect System Health.'],
    Completeness:['Confirm channel health first.','Inspect failed Point IDs and resolved addresses.','Read one failed Point in Diagnostics.'],
    Validity:['Inspect decode/type errors.','Compare raw payload with configured data_type.','Verify scale / offset only after raw decoding is correct.'],
    'Delivery Integrity':['Inspect Sink runtime state and queue depth.','Check target connection / write errors.','Compare enqueue rate with sink throughput.'],
  }
  return map[dimension]||[]
}
function openDimension(d:any){
  const affected=affectedForDimension(d.dimension)
  showDetail({
    kind:'data',title:d.dimension,subtitle:d.detail,level:d.status==='Failed'?'Fault':d.status==='Degraded'?'Warning':'Normal',
    dimension:d.dimension,impact:affected.length+' affected objects',duration:'Window · '+dataWindow.value,
    location:[['Dimension',d.dimension],['Window',dataWindow.value],['Stage',d.dimension==='Delivery Integrity'?'Delivery':'Acquisition / Processing']],
    evidence:[['Primary Metric',d.metric],['Secondary Detail',d.detail],['Status',d.status]],
    affected,checks:checksForDimension(d.dimension),
  })
}
function openDataSummary(m:any){
  const dimension=m.key==='stale'?'Continuity':m.key==='dropped'?'Delivery Integrity':'Completeness'
  showDetail({
    kind:'data',title:m.label,subtitle:'Data quality summary detail',level:levelFromTone(m.tone),
    dimension,impact:String(m.value)+' observed',duration:m.hint,
    location:[['Dimension',dimension],['Window',m.hint]],
    evidence:[['Metric',m.label],['Value',String(m.value)],['Window',m.hint]],
    affected:affectedForDimension(dimension),checks:checksForDimension(dimension),
  })
}
function openDataIssue(issue:any){
  const device=store.devices.find(d=>d.device_id===issue.object)
  const task=store.tasks.find(t=>t.task_id===issue.object)
  const sink=store.sinks.find(s=>s.name===issue.object)
  showDetail({
    kind:'data',title:issue.object,subtitle:issue.symptom,level:issue.level,
    dimension:issue.dimension,impact:issue.impact,duration:issue.duration,
    location:[
      ['Object Type',issue.kind],['Dimension',issue.dimension],
      ['Task',task?.task_id||'—'],['Device',device?.device_id||'—'],['Sink',sink?.name||'—'],
      ['Protocol',device?protocolOfDevice(device):(sink?.type||'—')],
    ],
    evidence:[['Symptom',issue.symptom],['Impact',issue.impact],['Duration',issue.duration],['Window',dataWindow.value]],
    affected:affectedForDimension(issue.dimension),checks:checksForDimension(issue.dimension),
  })
}

function tagType(tone:Tone){return tone==='danger'?'danger':tone==='warning'?'warning':'success'}
function stateTag(state:string){return state==='Interrupted'?'danger':state==='Degraded'?'warning':state==='Healthy'?'success':'info'}
function detailTag(level:DetailLevel){return level==='Fault'?'danger':level==='Warning'?'warning':'success'}

onMounted(()=>{
  nextTick(renderCharts)
  issueClock=window.setInterval(()=>{nowTick.value=Date.now()},1000)
})
onBeforeUnmount(()=>{
  window.removeEventListener('resize',updateViewport)
  if(autoTimer)window.clearInterval(autoTimer)
  if(countdownTimer)window.clearInterval(countdownTimer)
  if(checkTimer)window.clearTimeout(checkTimer)
  if(issueClock)window.clearInterval(issueClock)
  resizeObserver?.disconnect()
  latencyChart?.dispose()
  stateChart?.dispose()
})
</script>

<template>
  <div class="standard-page quality-page">
    <div class="quality-overview">
      <div class="head">
        <div>
          <h1>Quality</h1>
          <p>发现问题、确定影响范围并查看证据；主动测试与探索由 Diagnostics 负责。</p>
        </div>
      </div>

      <el-tabs v-model="activeTab" class="quality-tabs">
        <el-tab-pane label="Channel Quality" name="channel">
          <div class="quality-definition channel-definition">
            <div>
              <b>Channel Quality</b>
              <span>wind-hub 与外部端点之间的通信链路质量。</span>
            </div>
            <div class="quality-check-controls">
              <el-button type="primary" :loading="checking" @click="performCheck">Check</el-button>
              <label class="auto-check-control"><span>Auto Check</span><el-switch v-model="autoCheck" /></label>
              <el-select v-model="checkInterval" class="check-interval" aria-label="Check interval">
                <el-option v-for="option in checkIntervalOptions" :key="option.value" :label="option.label" :value="option.value" />
              </el-select>
              <div class="check-status">
                <span>{{ checking ? 'Checking…' : 'Last checked ' + lastChecked }}</span>
                <small v-if="autoCheck&&nextCheckIn!==null">Next check in {{ nextCheckIn }} s</small>
              </div>
            </div>
          </div>

          <div class="quality-metric-grid">
            <el-card
              v-for="m in channelSummary"
              :key="m.key"
              shadow="never"
              class="interactive-card"
              @click="openChannelSummary(m)"
            >
              <span class="metric-label">
                {{ m.label }}
                <el-tooltip v-if="m.key==='degraded'" placement="top" effect="dark" :show-after="200" :teleported="true" @click.stop>
                  <template #content>
                    <div class="metric-tooltip">
                      <b>Channel State</b>
                      <span>Healthy：通信正常。</span>
                      <span>Degraded：仍可通信，但 latency、timeout、reconnect 或 partial failure 已超过质量阈值。</span>
                      <span>Interrupted：通信链路当前不可用。</span>
                    </div>
                  </template>
                  <el-icon class="info-icon"><InfoFilled /></el-icon>
                </el-tooltip>
              </span>
              <div><b>{{ m.value }}</b><el-tag :type="tagType(m.tone)" size="small">{{ m.tone==='danger'?'Fault':m.tone==='warning'?'Attention':'Normal' }}</el-tag></div>
              <small>{{ m.hint }}</small>
            </el-card>
          </div>

          <section class="quality-section">
            <div class="section-title">
              <div><h2>Delivery Channels</h2><p>wind-hub → Kafka / PostgreSQL / Redis 等网络 Sink；点击对象查看完整证据链。</p></div>
            </div>
            <el-card shadow="never">
              <el-table :data="deliveryChannels">
                <el-table-column label="Sink" min-width="145"><template #default="{row}"><el-button link type="primary" @click="openChannelRow(row)">{{ row.object }}</el-button></template></el-table-column>
                <el-table-column prop="protocol" label="Protocol" width="110" />
                <el-table-column label="State" width="105"><template #default="{row}"><el-tag :type="stateTag(row.state)" size="small">{{ row.state }}</el-tag></template></el-table-column>
                <el-table-column v-if="!isMobile" prop="last" label="Last Check" min-width="145" />
                <el-table-column v-if="!isTablet" prop="latency" label="Latency" width="90" />
                <el-table-column v-if="!isTablet" prop="timeouts" label="Timeout" width="78" align="right" />
                <el-table-column v-if="!isTablet" prop="reconnects" label="Reconnect" width="88" align="right" />
                <el-table-column prop="issue" label="Current Issue" min-width="190" />
              </el-table>
            </el-card>
          </section>

          <section class="quality-section">
            <div class="section-title">
              <div><h2>Acquisition Channels</h2><p>Device / PLC / EMS → wind-hub。图表统计全部信道；图形和对象均可进入详情。</p></div>
            </div>

            <div class="channel-chart-grid">
              <el-card shadow="never">
                <div class="chart-card-head">
                  <div><b>Latency Distribution</b><span>采集通信 latency 的整体分布。</span></div>
                  <div class="latency-stats"><div><span>P50</span><b>{{ latencyStats.p50 }} ms</b></div><div><span>P95</span><b>{{ latencyStats.p95 }} ms</b></div><div><span>P99</span><b>{{ latencyStats.p99 }} ms</b></div></div>
                </div>
                <div ref="latencyChartEl" class="channel-chart" />
              </el-card>

              <el-card shadow="never">
                <div class="chart-card-head"><div><b>Channel State</b><span>当前采集信道状态构成。</span></div></div>
                <div ref="stateChartEl" class="channel-chart" />
              </el-card>
            </div>

            <div class="acquisition-toolbar">
              <el-segmented v-model="acquisitionFilter" :options="[{label:'All',value:'all'},{label:'Healthy',value:'healthy'},{label:'Degraded',value:'degraded'},{label:'Interrupted',value:'interrupted'}]" />
              <span>{{ filteredAcquisition.length }} channels</span>
            </div>

            <el-card shadow="never">
              <el-table :data="pagedAcquisition">
                <el-table-column label="Endpoint" min-width="145"><template #default="{row}"><el-button link type="primary" @click="openChannelRow(row)">{{ row.object }}</el-button></template></el-table-column>
                <el-table-column prop="protocol" label="Protocol" width="100" />
                <el-table-column label="State" width="105"><template #default="{row}"><el-tag :type="stateTag(row.state)" size="small">{{ row.state }}</el-tag></template></el-table-column>
                <el-table-column v-if="!isMobile" prop="last" label="Last Success" min-width="120" />
                <el-table-column v-if="!isTablet" prop="latency" label="Latency" width="90" />
                <el-table-column v-if="!isTablet" prop="timeouts" label="Timeout" width="78" align="right" />
                <el-table-column v-if="!isTablet" prop="reconnects" label="Reconnect" width="88" align="right" />
                <el-table-column prop="issue" label="Current Issue" min-width="190" />
              </el-table>
              <div class="pagination">
                <el-pagination v-model:current-page="acquisitionPage" v-model:page-size="acquisitionPageSize" :page-sizes="[20,50,100]" :total="filteredAcquisition.length" :layout="isMobile?'prev, pager, next':'total, sizes, prev, pager, next'" />
              </div>
            </el-card>
          </section>
        </el-tab-pane>

        <el-tab-pane label="Data Quality" name="data">
          <div class="quality-definition">
            <div><b>Data Quality</b><span>衡量期望数据流与实际可用数据流之间的偏差。</span></div>
            <el-segmented v-model="dataWindow" :options="['1 h','24 h','7 d']" />
          </div>

          <div class="quality-metric-grid">
            <el-card v-for="m in dataSummary" :key="m.key" shadow="never" class="interactive-card" @click="openDataSummary(m)">
              <span>{{ m.label }}</span>
              <div><b>{{ m.value }}</b><el-tag :type="tagType(m.tone)" size="small">{{ m.tone==='danger'?'Fault':m.tone==='warning'?'Attention':'Normal' }}</el-tag></div>
              <small>{{ m.hint }}</small>
            </el-card>
          </div>

          <section class="quality-section">
            <div class="section-title"><div><h2>Quality Dimensions</h2><p>点击任一维度进入页面级详情，查看问题位置、受影响对象和最近日志。</p></div></div>
            <div class="dimension-grid">
              <el-card v-for="d in dimensions" :key="d.dimension" shadow="never" class="dimension-card interactive-card" @click="openDimension(d)">
                <div class="dimension-card-head">
                  <span class="dimension-name">
                    <b>{{ d.dimension }}</b>
                    <el-tooltip placement="top" effect="dark" :show-after="200" :teleported="true" @click.stop>
                      <template #content><div class="dimension-tooltip"><b>{{ d.dimension }}</b><span>{{ d.description }}</span></div></template>
                      <el-icon class="info-icon"><InfoFilled /></el-icon>
                    </el-tooltip>
                  </span>
                  <el-tag :type="tagType(d.tone)" size="small">{{ d.status }}</el-tag>
                </div>
                <strong>{{ d.metric }}</strong>
                <span class="dimension-detail">{{ d.detail }}</span>
                <span class="dimension-window">Window · {{ dataWindow }}</span>
              </el-card>
            </div>
          </section>

          <section class="quality-section">
            <div class="section-title"><div><h2>Active Data Issues</h2><p>点击 Object 进入详情；大量对象统一分页，不使用 Drawer。</p></div></div>
            <el-card shadow="never">
              <el-table :data="dataIssues">
                <el-table-column label="Object" min-width="155"><template #default="{row}"><el-button link type="primary" @click="openDataIssue(row)">{{ row.object }}</el-button></template></el-table-column>
                <el-table-column label="Level" width="95"><template #default="{row}"><el-tag :type="row.level==='Fault'?'danger':'warning'" size="small">{{ row.level }}</el-tag></template></el-table-column>
                <el-table-column prop="dimension" label="Dimension" min-width="130" />
                <el-table-column prop="symptom" label="Symptom" min-width="180" />
                <el-table-column v-if="!isMobile" prop="impact" label="Impact" min-width="160" />
                <el-table-column prop="duration" label="Duration" min-width="100" />
              </el-table>
            </el-card>
          </section>
        </el-tab-pane>
      </el-tabs>
    </div>

    <el-drawer
      :model-value="detail!==null"
      direction="rtl"
      :size="isMobile?'100%':isTablet?'92%':'min(920px, 82vw)'"
      append-to-body
      destroy-on-close
      @update:model-value="open=>{if(!open)closeDetail()}"
    >
      <template #header>
        <div v-if="detail" class="quality-drawer-header">
          <div>
            <span>{{detail.kind==='channel'?'Channel Issue Detail':'Data Issue Detail'}}</span>
            <b>{{detail.title}}</b>
          </div>
          <el-tag :type="detailTag(detail.level)">{{detail.level}}</el-tag>
        </div>
      </template>

      <template v-if="detail">
        <p class="quality-drawer-subtitle">{{detail.subtitle}}</p>

        <div class="detail-summary-grid">
          <div><span>Dimension</span><b>{{detail.dimension}}</b></div>
          <div><span>Impact</span><b>{{detail.impact}}</b></div>
          <div><span>Duration / Window</span><b>{{detail.duration}}</b></div>
          <div><span>Recent Logs</span><b>20 related entries</b></div>
        </div>

        <section class="detail-section">
          <div class="section-title"><div><h2>Problem Location</h2><p>定位 Task / Device / Point / Sink / Protocol / Stage。</p></div></div>
          <el-descriptions :column="isMobile?1:2" border>
            <el-descriptions-item v-for="item in detail.location" :key="item[0]" :label="item[0]">{{item[1]}}</el-descriptions-item>
          </el-descriptions>
        </section>

        <section class="detail-section">
          <div class="section-title"><div><h2>Evidence</h2><p>触发当前质量结论的直接观测数据。</p></div></div>
          <el-descriptions :column="isMobile?1:2" border>
            <el-descriptions-item v-for="item in detail.evidence" :key="item[0]" :label="item[0]">{{item[1]}}</el-descriptions-item>
          </el-descriptions>
        </section>

        <section class="detail-section">
          <div class="section-title"><div><h2>Affected Objects</h2><p>{{detail.affected.length}} objects；大量对象统一分页。</p></div></div>
          <el-card shadow="never">
            <el-table :data="pagedAffected">
              <el-table-column v-for="column in affectedColumns" :key="column" :prop="column" :label="column" min-width="130"/>
            </el-table>
            <div class="pagination">
              <el-pagination
                v-model:current-page="affectedPage"
                v-model:page-size="affectedPageSize"
                :page-sizes="[10,20,50]"
                :total="detail.affected.length"
                :layout="isMobile?'prev, pager, next':'total, sizes, prev, pager, next'"
              />
            </div>
          </el-card>
        </section>

        <section class="detail-section">
          <div class="section-title"><div><h2>Recent Logs</h2><p>仅展示与当前 Issue 上下文相关的最近 20 条日志。</p></div></div>
          <el-card shadow="never">
            <el-table :data="logs">
              <el-table-column prop="time" label="Time" width="100"/>
              <el-table-column label="Level" width="90"><template #default="{row}"><el-tag :type="row.level==='ERROR'?'danger':row.level==='WARN'?'warning':'info'" size="small">{{row.level}}</el-tag></template></el-table-column>
              <el-table-column prop="source" label="Source" width="110"/>
              <el-table-column prop="object" label="Object" min-width="150"/>
              <el-table-column prop="message" label="Message" min-width="260"/>
            </el-table>
          </el-card>
        </section>

        <section class="detail-section">
          <div class="section-title"><div><h2>Suggested Investigation</h2><p>这里只给出下一步探索方向；主动网络、协议、读写测试仍在 Diagnostics 中完成。</p></div></div>
          <el-card shadow="never"><ol class="check-list"><li v-for="check in detail.checks" :key="check">{{check}}</li></ol></el-card>
        </section>
      </template>
    </el-drawer>
  </div>
</template>

<style scoped>
.quality-tabs{margin-top:var(--app-space-4)}.quality-definition{display:flex;align-items:center;justify-content:space-between;gap:var(--app-space-4);margin-bottom:var(--app-space-4);padding:var(--app-space-3) 0}.quality-definition>div:first-child{display:grid;gap:4px}.quality-definition b{font-size:var(--app-font-panel-title);font-weight:var(--app-font-weight-semibold)}.quality-definition span{color:var(--app-text-muted);font-size:var(--app-font-caption)}
.quality-check-controls{display:flex;align-items:center;justify-content:flex-end;gap:var(--app-space-3);flex-wrap:wrap}.auto-check-control{display:inline-flex;align-items:center;gap:var(--app-space-2);color:var(--app-text-secondary);font-size:var(--app-font-label);white-space:nowrap}.check-interval{width:92px}.check-status{display:grid;gap:2px;min-width:128px}.check-status span,.check-status small{color:var(--app-text-muted);font-size:var(--app-font-caption);font-variant-numeric:tabular-nums;white-space:nowrap}
.quality-metric-grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:var(--app-space-3);margin-bottom:var(--app-space-6)}.quality-metric-grid .el-card span,.quality-metric-grid .el-card small{color:var(--app-text-muted);font-size:var(--app-font-caption)}.quality-metric-grid .el-card>div>div{display:flex;align-items:center;justify-content:space-between;gap:var(--app-space-2);margin:var(--app-space-2) 0}.quality-metric-grid b{color:var(--app-text-primary);font-size:var(--app-font-metric);font-weight:var(--app-font-weight-semibold)}
.interactive-card{cursor:pointer;transition:border-color .15s ease,background .15s ease}.interactive-card:hover{border-color:var(--el-color-primary-light-5);background:var(--el-fill-color-extra-light)}.metric-label,.dimension-name{display:inline-flex!important;align-items:center;gap:5px}.metric-tooltip,.dimension-tooltip{display:grid;gap:5px;max-width:340px;line-height:1.5}.info-icon{color:var(--app-text-muted);font-size:var(--app-font-panel-title);cursor:help}
.quality-section,.detail-section{margin-bottom:var(--app-space-6)}.section-title{margin-bottom:var(--app-space-3)}.section-title h2{margin:0;font-size:var(--app-font-section-title);font-weight:var(--app-font-weight-semibold)}.section-title p{margin:4px 0 0;color:var(--app-text-muted);font-size:var(--app-font-caption)}
.channel-chart-grid{display:grid;grid-template-columns:minmax(0,2fr) minmax(280px,1fr);gap:var(--app-space-4);margin-bottom:var(--app-space-3)}.chart-card-head{display:flex;align-items:flex-start;justify-content:space-between;gap:var(--app-space-4)}.chart-card-head>div:first-child{display:grid;gap:4px}.chart-card-head>div:first-child>b{font-size:var(--app-font-panel-title)}.chart-card-head>div:first-child>span{color:var(--app-text-muted);font-size:var(--app-font-caption)}.latency-stats{display:flex;align-items:center;gap:var(--app-space-5)}.latency-stats>div{display:grid;gap:2px;text-align:right}.latency-stats span{color:var(--app-text-muted);font-size:var(--app-font-caption)}.latency-stats b{font-size:var(--app-font-body);font-weight:var(--app-font-weight-semibold);font-variant-numeric:tabular-nums}.channel-chart{height:250px;margin-top:var(--app-space-2)}
.acquisition-toolbar{display:flex;align-items:center;justify-content:space-between;gap:var(--app-space-3);margin:var(--app-space-3) 0}.acquisition-toolbar>span{color:var(--app-text-muted);font-size:var(--app-font-caption)}.pagination{display:flex;justify-content:flex-end;padding-top:var(--app-space-3)}
.dimension-grid{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:var(--app-space-3)}.dimension-card{min-width:0}.dimension-card-head{display:flex;align-items:center;justify-content:space-between;gap:var(--app-space-2)}.dimension-name b{font-size:var(--app-font-panel-title);font-weight:var(--app-font-weight-semibold)}.dimension-card strong{display:block;margin-top:var(--app-space-4);font-size:var(--app-font-panel-title);font-weight:var(--app-font-weight-semibold)}.dimension-detail{display:block;margin-top:4px;color:var(--app-text-muted);font-size:var(--app-font-caption)}.dimension-window{display:block;margin-top:var(--app-space-3);padding-top:var(--app-space-2);border-top:1px solid var(--app-border-soft);color:var(--app-text-muted);font-size:var(--app-font-caption)}
.quality-drawer-header{display:flex;align-items:center;justify-content:space-between;gap:var(--app-space-3);width:100%}.quality-drawer-header>div{display:grid;gap:var(--app-space-1)}.quality-drawer-header span{color:var(--app-text-muted);font-size:var(--app-font-caption)}.quality-drawer-header b{color:var(--app-text-primary);font-size:var(--app-font-section-title);font-weight:var(--app-font-weight-semibold)}.quality-drawer-subtitle{margin:0 0 var(--app-space-4);color:var(--app-text-secondary)}.detail-summary-grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:var(--app-space-3);margin-bottom:var(--app-space-6)}.detail-summary-grid>div{padding:var(--app-space-3);border:1px solid var(--app-border-soft);border-radius:var(--app-panel-radius);background:var(--el-bg-color)}.detail-summary-grid span{display:block;margin-bottom:5px;color:var(--app-text-muted);font-size:var(--app-font-caption)}.detail-summary-grid b{font-size:var(--app-font-body);font-weight:var(--app-font-weight-semibold)}.check-list{margin:0;padding-left:22px;display:grid;gap:var(--app-space-2);line-height:1.55;color:var(--app-text-secondary)}
@media(max-width:1199px){.dimension-grid{grid-template-columns:repeat(3,minmax(0,1fr))}}
@media(max-width:1199px){.quality-metric-grid{grid-template-columns:repeat(2,minmax(0,1fr))}.channel-definition{align-items:flex-start;flex-direction:column}.quality-check-controls{justify-content:flex-start}.channel-chart-grid{grid-template-columns:1fr}.dimension-grid{grid-template-columns:repeat(2,minmax(0,1fr))}.detail-summary-grid{grid-template-columns:repeat(2,minmax(0,1fr))}}
@media(max-width:767px){.quality-definition{align-items:flex-start;flex-direction:column}.quality-metric-grid,.dimension-grid,.detail-summary-grid{grid-template-columns:1fr}.quality-check-controls{width:100%;justify-content:flex-start}.check-status{width:100%}.chart-card-head{flex-direction:column}.latency-stats{width:100%;justify-content:space-between;gap:var(--app-space-3)}.latency-stats>div{text-align:left}.channel-chart{height:220px}.pagination{justify-content:center;overflow-x:auto}.detail-title-line{align-items:flex-start;flex-direction:column;gap:var(--app-space-2)}}
</style>
