<script setup lang="ts">
import * as echarts from 'echarts'
import { InfoFilled } from '@element-plus/icons-vue'
import { computed, nextTick, onBeforeUnmount, reactive, ref, watch } from 'vue'
import { useViewport } from '../composables/useViewport'
import {
  acquisitionChannels,
  deliveryChannels,
  qualityChannelMetricDetail,
  qualityDimensionDetail,
  qualityMetricDetail,
  qualityWindowData,
  type CommunicationEvent,
  type QualityProblem,
  type QualityWindow,
} from '../api/quality'
import { LATENCY, runQualityCheck, sleep } from '../api/service'
import { baseAxisLabel, baseAxisLine, baseChartOption, baseSplitLine } from '../utils/chartTheme'
import { nowText } from '../utils/format'
import { statusTagType } from '../utils/status'

type WindowRange=QualityWindow
type DrawerKind='channel-metric'|'event'|'data-metric'|'dimension'
type DrawerTable='tasks'|'devices'|'errors'|'problems'

interface QualityDrawer {
  kind:DrawerKind
  title:string
  subtitle:string
  distribution:Array<{name:string;value:number}>
  tasks:Array<Record<string,string|number>>
  devices:Array<Record<string,string|number>>
  errors:Array<Record<string,string|number>>
  problems:QualityProblem[]
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

const acquisitionPage=ref(1)
const acquisitionPageSize=ref(20)
const eventPage=ref(1)
const eventPageSize=ref(50)

const drawerOpen=ref(false)
const drawer=ref<QualityDrawer|null>(null)
const drawerPages=reactive<Record<DrawerTable,number>>({tasks:1,devices:1,errors:1,problems:1})
const drawerPageSizes=reactive<Record<DrawerTable,number>>({tasks:10,devices:10,errors:10,problems:10})
const chartEl=ref<HTMLElement|null>(null)
let chart:echarts.ECharts|null=null

// Auto Check（§16）：backend service 推进 qualityCheckTick → qualityWindowData 全量重算。
async function runCheck(){
  if(checking.value)return
  checking.value=true
  try{
    await sleep(LATENCY.qualityCheck)
    runQualityCheck()
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

// 通道当前状态（窗口无关）：由 backend Quality API 从 deviceScenarios / sink 状态派生。
const acquisitionChannelRows=computed(()=>acquisitionChannels())
const acquisitionChannelCount=computed(()=>acquisitionChannelRows.value.length)
const pagedAcquisitionChannels=computed(()=>{
  const start=(acquisitionPage.value-1)*acquisitionPageSize.value
  return acquisitionChannelRows.value.slice(start,start+acquisitionPageSize.value)
})
const deliveryChannelRows=computed(()=>deliveryChannels())

// 统一窗口数据源（§11–§15）：切窗口时 summary / events / metrics / dimensions / issues 全部联动。
const channelWindowData=computed(()=>qualityWindowData(channelWindow.value))
const dataWindowData=computed(()=>qualityWindowData(dataWindow.value))
const channelSummary=computed(()=>channelWindowData.value.channelSummary)
const communicationEvents=computed(()=>channelWindowData.value.communicationEvents)
const dataMetrics=computed(()=>dataWindowData.value.dataMetrics)
const dimensionRows=computed(()=>dataWindowData.value.dimensions)
const activeIssues=computed(()=>dataWindowData.value.issues)

const pagedCommunicationEvents=computed(()=>{
  const start=(eventPage.value-1)*eventPageSize.value
  return communicationEvents.value.slice(start,start+eventPageSize.value)
})

watch(channelWindow,()=>{
  acquisitionPage.value=1
  eventPage.value=1
})
watch(acquisitionPageSize,()=>{acquisitionPage.value=1})
watch(eventPageSize,()=>{eventPage.value=1})

function resetDrawerPages(){
  for(const key of Object.keys(drawerPages) as DrawerTable[])drawerPages[key]=1
}
function openChannelMetric(metric:(typeof channelSummary.value)[number]){
  const detail=qualityChannelMetricDetail(metric.key,channelWindow.value)
  resetDrawerPages()
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
  resetDrawerPages()
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

function openMetric(metric:(typeof dataMetrics.value)[number]){
  const detail=qualityMetricDetail(metric.key,dataWindow.value)
  resetDrawerPages()
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

function openDimension(row:(typeof dimensionRows.value)[number]){
  const detail=qualityDimensionDetail(row.key,dataWindow.value)
  resetDrawerPages()
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

function pageRows<T>(rows:T[],table:DrawerTable){
  const start=(drawerPages[table]-1)*drawerPageSizes[table]
  return rows.slice(start,start+drawerPageSizes[table])
}
const pagedDrawerTasks=computed(()=>pageRows(drawer.value?.tasks||[],'tasks'))
const pagedDrawerDevices=computed(()=>pageRows(drawer.value?.devices||[],'devices'))
const pagedDrawerErrors=computed(()=>pageRows(drawer.value?.errors||[],'errors'))
const pagedDrawerProblems=computed(()=>pageRows(drawer.value?.problems||[],'problems'))

function renderChart(){
  chart?.dispose()
  if(!chartEl.value||!drawer.value?.distribution.length)return
  chart=echarts.init(chartEl.value)
  chart.setOption({
    ...baseChartOption(),
    grid:{left:44,right:18,top:20,bottom:42},
    xAxis:{type:'category',data:drawer.value.distribution.map(x=>x.name),axisLine:baseAxisLine(),axisLabel:baseAxisLabel()},
    yAxis:{type:'value',minInterval:1,axisLabel:baseAxisLabel(),splitLine:baseSplitLine()},
    series:[{type:'bar',barMaxWidth:54,data:drawer.value.distribution.map(x=>x.value)}],
  })
}
watch(drawerOpen,open=>{
  if(open&&drawer.value?.distribution.length)nextTick(renderChart)
  if(!open){chart?.dispose();chart=null}
})
function stateType(state:string){
  return statusTagType(state)
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
        <div class="channel-window-bar">
          <el-segmented v-model="channelWindow" :options="['1 h','24 h','7 d']"/>
        </div>

        <div class="metric-grid clickable-metrics">
          <el-card v-for="metric in channelSummary" :key="metric.key" shadow="never" @click="openChannelMetric(metric)">
            <span>{{metric.label}}</span><b>{{metric.value}}</b><small>{{channelWindow}}</small>
          </el-card>
        </div>

        <section class="quality-section">
          <div class="section-head acquisition-head">
            <div>
              <h2>Acquisition Channels</h2>
              <p>设备采集通道当前状态；详情通过 Communication Events 查看。</p>
            </div>
            <div class="channel-check-panel">
              <div class="channel-check-actions">
                <div class="auto-check-group">
                  <span class="auto-check-label">Auto Check</span>
                  <el-switch v-model="autoCheck"/>
                  <el-select v-model="checkInterval" :disabled="!autoCheck" aria-label="Auto check interval">
                    <el-option :value="10" label="10 s"/>
                    <el-option :value="30" label="30 s"/>
                    <el-option :value="60" label="1 min"/>
                  </el-select>
                </div>
                <el-button type="primary" :loading="checking" @click="runCheck">Check</el-button>
              </div>
              <span class="last-check">Last check: {{lastChecked}}</span>
            </div>
          </div>

          <el-card shadow="never">
            <el-table :data="pagedAcquisitionChannels">
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
            <div v-if="acquisitionChannelCount>acquisitionPageSize" class="pagination">
              <el-pagination
                v-model:current-page="acquisitionPage"
                v-model:page-size="acquisitionPageSize"
                :page-sizes="[20,50,100]"
                :total="acquisitionChannelCount"
                :layout="isMobile ? 'prev, pager, next' : 'total, sizes, prev, pager, next'"
              />
            </div>
          </el-card>
        </section>

        <section class="quality-section">
          <div class="section-head">
            <div><h2>Delivery Channels</h2><p>Sink 交付通道当前状态。</p></div>
          </div>
          <el-card shadow="never">
            <el-table :data="deliveryChannelRows">
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
          <div class="section-head">
            <div><h2>Communication Events</h2><p>连接中断、恢复、超时和退化事件。点击事件查看时间与错误信息。</p></div>
          </div>
          <el-card shadow="never">
            <el-table :data="pagedCommunicationEvents" @row-click="openEvent" class="clickable-table">
              <el-table-column prop="time" label="Time" min-width="165"/>
              <el-table-column prop="object" label="Object" min-width="130"/>
              <el-table-column v-if="!isMobile" prop="protocol" label="Protocol" width="100"/>
              <el-table-column prop="event" label="Event" min-width="150"/>
              <el-table-column label="State" width="105"><template #default="{row}"><el-tag :type="stateType(row.state)">{{row.state}}</el-tag></template></el-table-column>
              <el-table-column prop="error" label="Error / Evidence" min-width="240" show-overflow-tooltip/>
              <el-table-column v-if="!isTablet" prop="duration" label="Duration" width="100"/>
            </el-table>
            <div v-if="communicationEvents.length>eventPageSize" class="pagination">
              <el-pagination
                v-model:current-page="eventPage"
                v-model:page-size="eventPageSize"
                :page-sizes="[50,100,200]"
                :total="communicationEvents.length"
                :layout="isMobile ? 'prev, pager, next' : 'total, sizes, prev, pager, next'"
              />
            </div>
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
                  <span class="dimension-name app-panel-title">{{row.dimension}}</span>
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
              <span class="dimension-metric">{{row.metric}}</span>
              <span class="dimension-detail">{{row.detail}}</span>
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

    <el-drawer v-model="drawerOpen" :title="drawer?.title||'Quality Detail'" :size="isMobile?'100%':'min(var(--app-drawer-width-md), 86vw)'" append-to-body>
      <template v-if="drawer">
        <p class="drawer-subtitle">{{drawer.subtitle}}</p>

        <section v-if="drawer.distribution.length" class="drawer-section">
          <h3>Distribution</h3>
          <div ref="chartEl" class="distribution-chart"></div>
        </section>

        <section v-if="drawer.kind==='dimension'" class="drawer-section">
          <h3>Problem Objects</h3>
          <el-table :data="pagedDrawerProblems" empty-text="No degraded objects">
            <el-table-column prop="object" label="Object" min-width="160"/>
            <el-table-column prop="kind" label="Type" width="90"/>
            <el-table-column prop="metric" label="Metric" min-width="130"/>
            <el-table-column v-if="!isMobile" prop="expected" label="Expected" min-width="130"/>
            <el-table-column label="State" width="100"><template #default="{row}"><el-tag :type="stateType(row.state)">{{row.state}}</el-tag></template></el-table-column>
            <el-table-column prop="error" label="Error" min-width="180" show-overflow-tooltip/>
          </el-table>
          <div v-if="drawer.problems.length>drawerPageSizes.problems" class="pagination">
            <el-pagination v-model:current-page="drawerPages.problems" v-model:page-size="drawerPageSizes.problems" :page-sizes="[10,20,50]" :total="drawer.problems.length" :layout="isMobile ? 'prev, pager, next' : 'total, sizes, prev, pager, next'"/>
          </div>
        </section>

        <section v-if="drawer.tasks.length" class="drawer-section">
          <h3>Affected Tasks</h3>
          <el-table :data="pagedDrawerTasks"><el-table-column v-for="key in Object.keys(drawer.tasks[0]||{})" :key="key" :prop="key" :label="key" min-width="130"/></el-table>
          <div v-if="drawer.tasks.length>drawerPageSizes.tasks" class="pagination">
            <el-pagination v-model:current-page="drawerPages.tasks" v-model:page-size="drawerPageSizes.tasks" :page-sizes="[10,20,50]" :total="drawer.tasks.length" :layout="isMobile ? 'prev, pager, next' : 'total, sizes, prev, pager, next'"/>
          </div>
        </section>

        <section v-if="drawer.devices.length" class="drawer-section">
          <h3>Affected Devices / Objects</h3>
          <el-table :data="pagedDrawerDevices"><el-table-column v-for="key in Object.keys(drawer.devices[0]||{})" :key="key" :prop="key" :label="key" min-width="130"/></el-table>
          <div v-if="drawer.devices.length>drawerPageSizes.devices" class="pagination">
            <el-pagination v-model:current-page="drawerPages.devices" v-model:page-size="drawerPageSizes.devices" :page-sizes="[10,20,50]" :total="drawer.devices.length" :layout="isMobile ? 'prev, pager, next' : 'total, sizes, prev, pager, next'"/>
          </div>
        </section>

        <section v-if="drawer.errors.length" class="drawer-section">
          <h3>Errors / Evidence</h3>
          <el-table :data="pagedDrawerErrors"><el-table-column v-for="key in Object.keys(drawer.errors[0]||{})" :key="key" :prop="key" :label="key" min-width="150"/></el-table>
          <div v-if="drawer.errors.length>drawerPageSizes.errors" class="pagination">
            <el-pagination v-model:current-page="drawerPages.errors" v-model:page-size="drawerPageSizes.errors" :page-sizes="[10,20,50]" :total="drawer.errors.length" :layout="isMobile ? 'prev, pager, next' : 'total, sizes, prev, pager, next'"/>
          </div>
        </section>
      </template>
    </el-drawer>
  </div>
</template>

<style scoped>
.channel-window-bar{display:flex;align-items:center;margin-bottom:var(--app-space-4)}
.quality-toolbar{display:flex;align-items:center;justify-content:space-between;gap:var(--app-space-4);margin-bottom:var(--app-space-4)}
.quality-toolbar h2{margin:0;font-size:var(--app-font-section-title)}.quality-toolbar p{margin:var(--app-space-1) 0 0;color:var(--app-text-muted);font-size:var(--app-font-body)}
.channel-check-panel{display:grid;justify-items:end;gap:var(--app-space-1);flex-shrink:0}
.channel-check-actions{display:flex;align-items:center;justify-content:flex-end;gap:var(--app-space-3);white-space:nowrap}
.auto-check-group{display:flex;align-items:center;gap:var(--app-space-2);white-space:nowrap;flex-shrink:0}
.auto-check-label{font-size:var(--app-font-body);line-height:var(--app-line-height-body);font-weight:var(--app-font-weight-regular);color:var(--app-text-primary);white-space:nowrap}
.last-check{color:var(--app-text-muted);font-size:var(--app-font-label);line-height:var(--app-line-height-body);white-space:nowrap}
.metric-grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:var(--app-space-3);margin-bottom:var(--app-space-6)}
.metric-grid :deep(.el-card__body){display:grid;gap:var(--app-space-1)}
.metric-grid span{color:var(--app-text-secondary);font-size:var(--app-font-label)}.metric-grid b{font-size:var(--app-font-metric);font-weight:var(--app-font-weight-semibold)}.metric-grid small{color:var(--app-text-muted)}
.clickable-metrics :deep(.el-card),.dimension-card,.clickable-table :deep(.el-table__row){cursor:pointer}
.quality-section{margin-top:var(--app-space-6)}.section-head{display:flex;align-items:flex-start;justify-content:space-between;gap:var(--app-space-4);margin-bottom:var(--app-space-3)}
.section-head h2,.drawer-section h3{margin:0;font-size:var(--app-font-section-title)}.section-head p,.drawer-subtitle{margin:var(--app-space-1) 0 0;color:var(--app-text-muted);font-size:var(--app-font-body)}
.dimension-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:var(--app-space-3)}.dimension-card :deep(.el-card__body){display:grid;gap:var(--app-space-2)}
.dimension-head{display:flex;align-items:center;justify-content:space-between;gap:var(--app-space-2)}.dimension-title{display:flex;align-items:center;gap:var(--app-space-1)}.info-icon{color:var(--app-text-muted);cursor:help}
.dimension-name{display:inline-flex;align-items:center}
.dimension-metric{color:var(--app-text-primary);font-size:var(--app-font-body);line-height:var(--app-line-height-compact);font-weight:var(--app-font-weight-semibold)}
.dimension-detail{color:var(--app-text-secondary);font-size:var(--app-font-body);line-height:var(--app-line-height-body);font-weight:var(--app-font-weight-regular)}
.dimension-help{max-width:var(--app-tooltip-max-width);display:grid;gap:var(--app-space-2);line-height:var(--app-line-height-body)}
.drawer-section{margin-top:var(--app-space-6)}.distribution-chart{height:var(--app-chart-height-md)}
@media(max-width:1199px){.metric-grid{grid-template-columns:repeat(2,minmax(0,1fr))}.dimension-grid{grid-template-columns:repeat(2,minmax(0,1fr))}.acquisition-head{align-items:stretch;flex-direction:column}.channel-check-panel{justify-items:start}.channel-check-actions{justify-content:flex-start}}
@media(max-width:767px){.quality-toolbar{align-items:flex-start;flex-direction:column}.channel-check-panel{width:100%}.channel-check-actions{width:100%;align-items:flex-start;flex-wrap:wrap}.auto-check-group{flex-wrap:nowrap}.metric-grid,.dimension-grid{grid-template-columns:1fr}.distribution-chart{height:var(--app-chart-height-sm)}}
</style>
