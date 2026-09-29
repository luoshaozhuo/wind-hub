<script setup lang="ts">
import { computed, onBeforeUnmount, ref } from 'vue'
import { protocolOfDevice, store } from '../mock/data'

const emit = defineEmits<{ (e:'navigate', page:'Debug'): void }>()
const viewportWidth = ref(window.innerWidth)
const isMobile = computed(() => viewportWidth.value < 768)
const isTablet = computed(() => viewportWidth.value < 1200)
function updateViewport(){ viewportWidth.value = window.innerWidth }
window.addEventListener('resize', updateViewport)
onBeforeUnmount(()=>window.removeEventListener('resize', updateViewport))

type IssueSeverity = 'error' | 'warning'
type QualityIssue = {
  severity: IssueSeverity
  object: string
  kind: 'Task' | 'Device'
  scope: string
  symptom: string
  since: string
  duration: string
  last_success: string
  impact: string
  probable_cause: string
}

function protocolLabel(index:number){
  const device=store.devices.filter(d=>d.enabled)[index] || store.devices[index]
  return device ? protocolOfDevice(device) : 'unknown'
}

const activeIssues = computed<QualityIssue[]>(() => {
  const runningTasks = store.tasks.filter(t => t.runtime === 'RUNNING')
  const devices = store.devices.filter(d => d.enabled)
  const fallbackDevice = devices[0]?.device_id || 'wtg-001'
  const secondDevice = devices[1]?.device_id || 'wtg-006'
  return [
    {
      severity:'error',
      object: runningTasks[1]?.task_id || 'turbine-ads-all',
      kind:'Task',
      scope:'24 devices',
      symptom:'Collection interrupted',
      since:'21:37:12',
      duration:'12m 18s',
      last_success:'12m 18s ago',
      impact:'24 devices · 120 points',
      probable_cause:'Protocol session / route',
    },
    {
      severity:'warning',
      object: runningTasks[0]?.task_id || 'turbine-modbus-all',
      kind:'Task',
      scope:'24 devices',
      symptom:'Jitter P95 above baseline',
      since:'21:44:08',
      duration:'5m 22s',
      last_success:'80 ms ago',
      impact:'3 missing ticks',
      probable_cause:'Polling overrun / device latency',
    },
    {
      severity:'warning',
      object:fallbackDevice,
      kind:'Device',
      scope:protocolLabel(0),
      symptom:'Repeated read timeout',
      since:'21:46:41',
      duration:'2m 49s',
      last_success:'1.8 s ago',
      impact:'3 points',
      probable_cause:'Network / remote endpoint',
    },
    {
      severity:'warning',
      object:secondDevice,
      kind:'Device',
      scope:protocolLabel(1),
      symptom:'Reconnect burst',
      since:'21:47:20',
      duration:'2m 10s',
      last_success:'340 ms ago',
      impact:'4 reconnects / 5 min',
      probable_cause:'Unstable protocol session',
    },
  ]
})

const situation = computed(() => {
  const issues = activeIssues.value
  return [
    { label:'Active interruptions', value:issues.filter(x=>x.symptom.includes('interrupted')).length, hint:'currently blocking acquisition', tone:'danger' as const },
    { label:'Degraded tasks', value:new Set(issues.filter(x=>x.kind==='Task').map(x=>x.object)).size, hint:'need attention now', tone:'warning' as const },
    { label:'Affected devices', value:Math.max(2, issues.filter(x=>x.kind==='Device').length + 4), hint:'direct or group impact', tone:'warning' as const },
    { label:'Timeouts', value:13, hint:'last 24 h', tone:'neutral' as const },
  ]
})

const taskExceptions = computed(() => store.tasks
  .filter((t,i)=>t.runtime==='RUNNING' && i<3)
  .map((t,i)=>({
    task:t.task_id,
    expected:t.interval===null?'Subscription':`${Number(t.interval).toFixed(3)} s`,
    actual:t.interval===null?'—':`${(Number(t.interval)+0.002+i*0.003).toFixed(3)} s`,
    jitter:`${36+i*15} ms`,
    missing:3+i*6,
    interruptions:1+(i%2),
    last:`${80+i*120} ms ago`,
    state:i===1?'Interrupted':'Degraded',
  })))

const clusters = computed(() => [
  { name:'Read timeout', count:3, scope:'2 devices', sample:activeIssues.value.filter(x=>x.kind==='Device').map(x=>x.object).slice(0,2).join(', ') || '—', cause:'Network / endpoint response' },
  { name:'Protocol reconnect', count:4, scope:'1 device', sample:activeIssues.value.find(x=>x.symptom.includes('Reconnect'))?.object || '—', cause:'Session instability' },
  { name:'Timing / jitter', count:1, scope:'1 task', sample:activeIssues.value.find(x=>x.symptom.includes('Jitter'))?.object || '—', cause:'Polling overrun' },
])

const recentTrend = [
  { label:'Interruptions', now:2, previous:0, direction:'up' },
  { label:'Read timeout', now:13, previous:5, direction:'up' },
  { label:'Reconnect', now:7, previous:6, direction:'flat' },
]

function diagnose(){ emit('navigate','Debug') }
</script>

<template>
  <div class="standard-page quality-page">
    <div class="head">
      <div><h1>Quality</h1><p>采集质量态势：先发现异常，再进入 Diagnostics 定位原因</p></div>
    </div>

    <section class="quality-section">
      <div class="section-title"><div><h2>Current Situation</h2><p>只突出当前需要行动的异常，不用状态色装饰普通统计。</p></div></div>
      <div class="situation-grid">
        <el-card v-for="item in situation" :key="item.label" shadow="never" class="situation-card">
          <span>{{item.label}}</span>
          <div class="situation-value">
            <b>{{item.value}}</b>
            <el-tag v-if="item.tone!=='neutral'" :type="item.tone==='danger'?'danger':'warning'" size="small">{{item.tone==='danger'?'Fault':'Attention'}}</el-tag>
          </div>
          <small>{{item.hint}}</small>
        </el-card>
      </div>
    </section>

    <section class="quality-section">
      <div class="section-title">
        <div><h2>Active Issues</h2><p>当前真正影响采集的对象，按严重程度和持续时间处理。</p></div>
        <el-button @click="diagnose">Open Diagnostics</el-button>
      </div>
      <el-card shadow="never">
        <el-table :data="activeIssues" row-key="object">
          <el-table-column label="Object" min-width="155">
            <template #default="{row}"><div class="object-cell"><b>{{row.object}}</b><span>{{row.kind}} · {{row.scope}}</span></div></template>
          </el-table-column>
          <el-table-column label="Status" width="100">
            <template #default="{row}"><el-tag :type="row.severity==='error'?'danger':'warning'" size="small">{{row.severity==='error'?'Fault':'Warning'}}</el-tag></template>
          </el-table-column>
          <el-table-column prop="symptom" label="Symptom" min-width="180"/>
          <el-table-column v-if="!isMobile" prop="duration" label="Duration" width="105"/>
          <el-table-column v-if="!isTablet" prop="last_success" label="Last Success" min-width="125"/>
          <el-table-column v-if="!isTablet" prop="impact" label="Impact" min-width="155"/>
          <el-table-column prop="probable_cause" label="Probable Cause" min-width="190"/>
          <el-table-column label="Operation" width="100">
            <template #default><el-button link type="primary" @click="diagnose">Diagnose</el-button></template>
          </el-table-column>
        </el-table>
      </el-card>
    </section>

    <div class="quality-two-col">
      <section class="quality-section">
        <div class="section-title"><div><h2>Task Exceptions</h2><p>只显示节拍、中断或缺失异常的运行任务。</p></div></div>
        <el-card shadow="never">
          <el-table :data="taskExceptions" size="small">
            <el-table-column prop="task" label="Task" min-width="160"/>
            <el-table-column label="State" width="105"><template #default="{row}"><el-tag :type="row.state==='Interrupted'?'danger':'warning'" size="small">{{row.state}}</el-tag></template></el-table-column>
            <el-table-column v-if="!isMobile" prop="actual" label="Actual" width="95"/>
            <el-table-column v-if="!isTablet" prop="jitter" label="Jitter P95" width="95"/>
            <el-table-column prop="missing" label="Missing" width="80" align="right"/>
            <el-table-column v-if="!isMobile" prop="interruptions" label="Interruptions" width="95" align="right"/>
            <el-table-column prop="last" label="Last Success" min-width="110"/>
          </el-table>
        </el-card>
      </section>

      <section class="quality-section">
        <div class="section-title"><div><h2>Problem Clusters</h2><p>按症状聚类，判断是单点故障还是同类问题。</p></div></div>
        <el-card shadow="never">
          <div class="cluster-list">
            <div v-for="c in clusters" :key="c.name" class="cluster-row">
              <div><b>{{c.name}}</b><span>{{c.scope}} · {{c.sample}}</span></div>
              <div class="cluster-meta"><strong>{{c.count}}</strong><span>{{c.cause}}</span></div>
            </div>
          </div>
        </el-card>
      </section>
    </div>

    <section class="quality-section">
      <div class="section-title"><div><h2>Recent Change</h2><p>最近 24 h 与前一窗口对比，用于判断质量是否恶化。</p></div></div>
      <el-card shadow="never">
        <div class="change-grid">
          <div v-for="x in recentTrend" :key="x.label" class="change-item">
            <span>{{x.label}}</span>
            <b>{{x.now}}</b>
            <small>previous {{x.previous}} · {{x.direction==='up'?'increased':x.direction==='flat'?'stable':'decreased'}}</small>
          </div>
        </div>
      </el-card>
    </section>
  </div>
</template>

<style scoped>
.quality-section{margin-bottom:var(--app-space-5)}.section-title{display:flex;align-items:flex-end;justify-content:space-between;gap:var(--app-space-3);margin-bottom:var(--app-space-3)}.section-title h2{margin:0;font-size:var(--app-font-section-title);font-weight:var(--app-font-weight-semibold)}.section-title p{margin:4px 0 0;color:var(--app-text-muted);font-size:var(--app-font-caption)}
.situation-grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:var(--app-space-3)}.situation-card span,.situation-card small{color:var(--app-text-muted);font-size:var(--app-font-caption)}.situation-value{display:flex;align-items:center;justify-content:space-between;gap:var(--app-space-2);margin:var(--app-space-2) 0}.situation-value b{font-size:var(--app-font-metric);font-weight:var(--app-font-weight-semibold);color:var(--app-text-primary)}
.object-cell{display:grid;gap:2px}.object-cell span{color:var(--app-text-muted);font-size:var(--app-font-caption)}
.quality-two-col{display:grid;grid-template-columns:minmax(0,1.2fr) minmax(320px,.8fr);gap:var(--app-space-4)}.cluster-list{display:grid}.cluster-row{display:flex;align-items:center;justify-content:space-between;gap:var(--app-space-4);padding:var(--app-space-3) 0;border-bottom:1px solid var(--app-border-soft)}.cluster-row:last-child{border-bottom:0}.cluster-row>div:first-child,.cluster-meta{display:grid;gap:2px}.cluster-row span,.cluster-meta span{color:var(--app-text-muted);font-size:var(--app-font-caption)}.cluster-meta{text-align:right}.cluster-meta strong{font-size:var(--app-font-panel-title)}
.change-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:var(--app-space-4)}.change-item{display:grid;gap:4px}.change-item span,.change-item small{color:var(--app-text-muted);font-size:var(--app-font-caption)}.change-item b{font-size:var(--app-font-panel-title);font-weight:var(--app-font-weight-semibold)}
@media(max-width:1199px){.situation-grid{grid-template-columns:repeat(2,minmax(0,1fr))}.quality-two-col{grid-template-columns:1fr}}
@media(max-width:767px){.situation-grid,.change-grid{grid-template-columns:1fr}.section-title{align-items:flex-start;flex-direction:column}}
</style>
