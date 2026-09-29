<script setup lang="ts">
import { computed, onBeforeUnmount, ref } from 'vue'
import { protocolOfDevice, store } from '../mock/data'

const emit = defineEmits<{ (e:'navigate', page:'Debug'): void }>()
const activeTab = ref<'channel'|'data'>('channel')
const viewportWidth = ref(window.innerWidth)
const isMobile = computed(()=>viewportWidth.value<768)
const isTablet = computed(()=>viewportWidth.value<1200)
function updateViewport(){viewportWidth.value=window.innerWidth}
window.addEventListener('resize',updateViewport)
onBeforeUnmount(()=>window.removeEventListener('resize',updateViewport))

type Tone='normal'|'warning'|'danger'

function enabledDevice(index:number){
  return store.devices.filter(d=>d.enabled)[index] || store.devices[index]
}
function deviceProtocol(index:number){
  const d=enabledDevice(index)
  return d?protocolOfDevice(d):'unknown'
}

const channelSummary=[
  {label:'Active interruptions',value:2,hint:'acquisition + delivery',tone:'danger' as Tone},
  {label:'Timeouts',value:13,hint:'last 24 h',tone:'warning' as Tone},
  {label:'Reconnects',value:7,hint:'last 24 h',tone:'warning' as Tone},
  {label:'Degraded endpoints',value:3,hint:'need attention',tone:'warning' as Tone},
]

const acquisitionChannels=computed(()=>[
  {object:enabledDevice(0)?.device_id||'wtg-001',protocol:deviceProtocol(0),state:'Interrupted',last:'12m 18s ago',latency:'—',timeouts:8,reconnects:3,issue:'Protocol session unavailable'},
  {object:enabledDevice(1)?.device_id||'wtg-006',protocol:deviceProtocol(1),state:'Degraded',last:'340 ms ago',latency:'84 ms',timeouts:4,reconnects:4,issue:'Reconnect burst'},
  {object:enabledDevice(2)?.device_id||'wtg-025',protocol:deviceProtocol(2),state:'Degraded',last:'1.8 s ago',latency:'126 ms',timeouts:3,reconnects:0,issue:'Repeated read timeout'},
])

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
    issue:s.enabled?(s.error||'Session / endpoint check required'):'Sink disabled',
  })))

const dataSummary=[
  {label:'Stale tasks',value:1,hint:'current',tone:'danger' as Tone},
  {label:'Missing cycles',value:13,hint:'last 24 h',tone:'warning' as Tone},
  {label:'Point read failures',value:6,hint:'last 24 h',tone:'warning' as Tone},
  {label:'Dropped points',value:0,hint:'last 24 h',tone:'normal' as Tone},
]

const dataDimensions=[
  {dimension:'Continuity',status:'Failed',metric:'1 stale task',detail:'Longest active gap 12m 18s',tone:'danger' as Tone},
  {dimension:'Timeliness',status:'Degraded',metric:'Jitter P95 87 ms',detail:'Worst task 8.7 × expected age',tone:'warning' as Tone},
  {dimension:'Completeness',status:'Degraded',metric:'13 missing cycles',detail:'6 point reads failed',tone:'warning' as Tone},
  {dimension:'Validity',status:'Degraded',metric:'2 decode errors',detail:'No timestamp/order errors',tone:'warning' as Tone},
  {dimension:'Delivery Integrity',status:'Normal',metric:'0 dropped points',detail:'File sink backlog 2',tone:'normal' as Tone},
]

const dataIssues=computed(()=>[
  {object:'turbine-ads-all',level:'Fault',dimension:'Continuity',symptom:'No fresh samples',impact:'24 devices · 120 points',last:'12m 18s ago'},
  {object:'turbine-modbus-all',level:'Warning',dimension:'Timeliness',symptom:'Jitter above baseline',impact:'3 missing cycles',last:'80 ms ago'},
  {object:enabledDevice(2)?.device_id||'wtg-025',level:'Warning',dimension:'Completeness',symptom:'3 point reads missing',impact:'3 points',last:'1.8 s ago'},
  {object:'file_archive',level:'Warning',dimension:'Delivery Integrity',symptom:'Queue depth increasing',impact:'2 queued batches',last:'4 ms ago'},
])

function tagType(tone:Tone){return tone==='danger'?'danger':tone==='warning'?'warning':'success'}
function diagnose(){emit('navigate','Debug')}
</script>

<template>
  <div class="standard-page quality-page">
    <div class="head">
      <div><h1>Quality</h1><p>区分通信链路质量与最终数据交付质量，异常可进入 Diagnostics 继续定位。</p></div>
    </div>

    <el-tabs v-model="activeTab" class="quality-tabs">
      <el-tab-pane label="Channel Quality" name="channel">
        <div class="quality-definition">
          <div><b>Channel Quality</b><span>wind-hub 与外部端点之间的通信链路质量，包括设备采集侧和网络 Sink 交付侧。</span></div>
          <el-button @click="diagnose">Open Diagnostics</el-button>
        </div>

        <div class="quality-metric-grid">
          <el-card v-for="m in channelSummary" :key="m.label" shadow="never">
            <span>{{m.label}}</span>
            <div><b>{{m.value}}</b><el-tag :type="tagType(m.tone)" size="small">{{m.tone==='danger'?'Fault':m.tone==='warning'?'Attention':'Normal'}}</el-tag></div>
            <small>{{m.hint}}</small>
          </el-card>
        </div>

        <section class="quality-section">
          <div class="section-title"><div><h2>Acquisition Channels</h2><p>Device / PLC / EMS → wind-hub。只展示当前异常信道。</p></div></div>
          <el-card shadow="never">
            <el-table :data="acquisitionChannels">
              <el-table-column prop="object" label="Endpoint" min-width="145"/>
              <el-table-column prop="protocol" label="Protocol" width="100"/>
              <el-table-column label="State" width="105"><template #default="{row}"><el-tag :type="row.state==='Interrupted'?'danger':'warning'" size="small">{{row.state}}</el-tag></template></el-table-column>
              <el-table-column v-if="!isMobile" prop="last" label="Last Success" min-width="120"/>
              <el-table-column v-if="!isTablet" prop="latency" label="Latency" width="90"/>
              <el-table-column v-if="!isTablet" prop="timeouts" label="Timeout" width="78" align="right"/>
              <el-table-column v-if="!isTablet" prop="reconnects" label="Reconnect" width="88" align="right"/>
              <el-table-column prop="issue" label="Current Issue" min-width="190"/>
              <el-table-column label="Operation" width="100"><template #default><el-button link type="primary" @click="diagnose">Diagnose</el-button></template></el-table-column>
            </el-table>
          </el-card>
        </section>

        <section class="quality-section">
          <div class="section-title"><div><h2>Delivery Channels</h2><p>wind-hub → Kafka / PostgreSQL / Redis 等网络 Sink；File 不计入网络信道。</p></div></div>
          <el-card shadow="never">
            <el-table :data="deliveryChannels" empty-text="No network Sinks configured">
              <el-table-column prop="object" label="Sink" min-width="145"/>
              <el-table-column prop="protocol" label="Protocol" width="110"/>
              <el-table-column label="State" width="105"><template #default="{row}"><el-tag :type="row.state==='Interrupted'?'danger':row.state==='Degraded'?'warning':'info'" size="small">{{row.state}}</el-tag></template></el-table-column>
              <el-table-column v-if="!isMobile" prop="last" label="Last Check" min-width="145"/>
              <el-table-column v-if="!isTablet" prop="latency" label="Latency" width="90"/>
              <el-table-column v-if="!isTablet" prop="timeouts" label="Timeout" width="78" align="right"/>
              <el-table-column v-if="!isTablet" prop="reconnects" label="Reconnect" width="88" align="right"/>
              <el-table-column prop="issue" label="Current Issue" min-width="190"/>
            </el-table>
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
          <div class="section-title"><div><h2>Quality Dimensions</h2><p>连续性、时效性、完整性、技术有效性与最终交付完整性。</p></div></div>
          <el-card shadow="never">
            <div class="dimension-list">
              <div v-for="d in dataDimensions" :key="d.dimension" class="dimension-row">
                <div><b>{{d.dimension}}</b><span>{{d.detail}}</span></div>
                <strong>{{d.metric}}</strong>
                <el-tag :type="tagType(d.tone)" size="small">{{d.status}}</el-tag>
              </div>
            </div>
          </el-card>
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
.quality-tabs{margin-top:var(--app-space-4)}.quality-definition{display:flex;align-items:center;justify-content:space-between;gap:var(--app-space-4);margin-bottom:var(--app-space-4);padding:var(--app-space-3) 0}.quality-definition>div{display:grid;gap:4px}.quality-definition b{font-size:var(--app-font-panel-title);font-weight:var(--app-font-weight-semibold)}.quality-definition span{color:var(--app-text-muted);font-size:var(--app-font-caption)}
.quality-metric-grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:var(--app-space-3);margin-bottom:var(--app-space-6)}.quality-metric-grid .el-card span,.quality-metric-grid .el-card small{color:var(--app-text-muted);font-size:var(--app-font-caption)}.quality-metric-grid .el-card>div>div{display:flex;align-items:center;justify-content:space-between;gap:var(--app-space-2);margin:var(--app-space-2) 0}.quality-metric-grid b{color:var(--app-text-primary);font-size:var(--app-font-metric);font-weight:var(--app-font-weight-semibold)}
.quality-section{margin-bottom:var(--app-space-6)}.section-title{margin-bottom:var(--app-space-3)}.section-title h2{margin:0;font-size:var(--app-font-section-title);font-weight:var(--app-font-weight-semibold)}.section-title p{margin:4px 0 0;color:var(--app-text-muted);font-size:var(--app-font-caption)}
.dimension-list{display:grid}.dimension-row{display:grid;grid-template-columns:minmax(0,1fr) minmax(160px,.45fr) 90px;align-items:center;gap:var(--app-space-4);padding:var(--app-space-3) 0;border-bottom:1px solid var(--app-border-soft)}.dimension-row:last-child{border-bottom:0}.dimension-row>div{display:grid;gap:2px}.dimension-row span{color:var(--app-text-muted);font-size:var(--app-font-caption)}.dimension-row strong{font-size:var(--app-font-body);font-weight:var(--app-font-weight-semibold);text-align:right}
@media(max-width:1199px){.quality-metric-grid{grid-template-columns:repeat(2,minmax(0,1fr))}}
@media(max-width:767px){.quality-definition{align-items:flex-start;flex-direction:column}.quality-metric-grid{grid-template-columns:1fr}.dimension-row{grid-template-columns:1fr auto}.dimension-row strong{text-align:left}.dimension-row .el-tag{grid-column:2;grid-row:1/3}}
</style>
