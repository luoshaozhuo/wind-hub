<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { InfoFilled } from '@element-plus/icons-vue'
import { protocolOfDevice, store } from '../mock/data'

const emit = defineEmits<{ (e:'navigate', page:'Quality'|'SystemHealth'): void }>()

type Tone = 'normal' | 'info' | 'warning' | 'danger' | 'muted'

interface RecentEvent {
  time: string
  level: 'ERROR' | 'WARN'
  object: string
  event: string
  state: 'ACTIVE' | 'RECOVERED'
}

interface ActiveAlert {
  level: 'ERROR' | 'WARN'
  object: string
  event: string
  since: string
  duration: string
}

const now = ref(Date.now())
let timer: number | undefined

onMounted(() => {
  timer = window.setInterval(() => {
    now.value = Date.now()
  }, 1000)
})

onBeforeUnmount(() => {
  if (timer !== undefined) window.clearInterval(timer)
})

/*
 * Service uptime = Wind Hub 服务从最近一次成功启动到当前的连续运行时间。
 * 与主机 uptime 区分。
 */
const serviceStartedAt = Date.now() - (((18 * 24 + 7) * 60 + 42) * 60 + 16) * 1000
const lastStart = '2026-09-09 11:36:12'
const lastStop = '2026-09-09 11:34:08'
const lastReload = '2026-09-27 17:42:31'

const formatDuration = (ms: number) => {
  const total = Math.max(0, Math.floor(ms / 1000))
  const days = Math.floor(total / 86400)
  const hours = Math.floor((total % 86400) / 3600)
  const minutes = Math.floor((total % 3600) / 60)
  const seconds = total % 60
  return `${days}d ${String(hours).padStart(2, '0')}:${String(minutes).padStart(2, '0')}:${String(seconds).padStart(2, '0')}`
}

const serviceUptime = computed(() => formatDuration(now.value - serviceStartedAt))

const enabledDevices = computed(() => store.devices.filter(d => d.enabled))
const online = computed(() => enabledDevices.value.filter(d => d.online).length)
const offline = computed(() => enabledDevices.value.filter(d => !d.online).length)
const disabledDevices = computed(() => store.devices.filter(d => !d.enabled).length)

const runningTasks = computed(() => store.tasks.filter(t => t.runtime === 'RUNNING').length)
const stoppedTasks = computed(() => store.tasks.filter(t => t.runtime !== 'RUNNING').length)
const enabledSinks = computed(() => store.sinks.filter(s => s.enabled).length)

const typeStats = computed(() =>
  store.deviceTypes
    .map(type => {
      const devices = store.devices.filter(device => {
        const model = store.deviceModels.find(m => m.id === device.model)
        return model?.device_type === type.id
      })
      return {
        label: type.name,
        total: devices.length,
        online: devices.filter(d => d.enabled && d.online).length,
      }
    })
    .filter(x => x.total > 0),
)

const allModelStats = computed(() =>
  store.deviceModels
    .map(model => {
      const devices = store.devices.filter(d => d.model === model.id)
      const enabled = devices.filter(d => d.enabled)
      return {
        label: model.id,
        total: enabled.length,
        online: enabled.filter(d => d.online).length,
        offline: enabled.filter(d => !d.online).length,
      }
    })
    .filter(x => x.total > 0)
    .sort((a, b) => {
      if ((a.offline > 0) !== (b.offline > 0)) return a.offline > 0 ? -1 : 1
      return b.total - a.total
    }),
)

const visibleModelStats = computed(() => allModelStats.value.slice(0, 4))
const hiddenModelCount = computed(() => Math.max(0, allModelStats.value.length - visibleModelStats.value.length))

const protocolStats = computed(() => {
  const protocols = ['ads', 'modbus', 'iec104']
  return protocols
    .map(protocol => {
      const devices = store.devices.filter(d => protocolOfDevice(d) === protocol)
      return {
        label: protocol.toUpperCase(),
        total: devices.length,
        online: devices.filter(d => d.enabled && d.online).length,
      }
    })
    .filter(x => x.total > 0)
})

const acquisition = {
  success1m: 99.92,
  success5m: 99.97,
  avgLatencyMs: 86,
  p95LatencyMs: 141,
  timeout1m: 2,
  overrun1m: 1,
}

const host = {
  cpu: 18.4,
  memory: 41.7,
  diskFree: 68.2,
  healthCheck: '3 s ago',
}

const timeliness = {
  totalTasks: 2,
  freshTasks: 1,
  delayedTasks: 0,
  staleTasks: 1,
  worstTask: 'turbine-ads-all',
  worstAgeRatio: 8.7,
}

const communication = {
  disconnected: 1,
  timeout1m: 2,
  reconnect1m: 1,
  overrun1m: 1,
}

const sinkRuntime = [
  { name: 'file_archive', type: 'file', state: 'HEALTHY', tone: 'normal' as Tone },
  { name: 'kafka_main', type: 'kafka', state: 'DISABLED', tone: 'muted' as Tone },
  { name: 'db_main', type: 'db', state: 'DISABLED', tone: 'muted' as Tone },
]

const recentEvents: RecentEvent[] = [
  { time: '19:00:48', level: 'ERROR', object: 'wtg-041', event: 'ADS disconnected', state: 'ACTIVE' },
  { time: '19:00:33', level: 'WARN', object: 'turbine-ads-all', event: 'Interval overrun 42 ms', state: 'ACTIVE' },
  { time: '19:00:21', level: 'WARN', object: 'wtg-003', event: 'Modbus read timeout', state: 'RECOVERED' },
  { time: '19:00:06', level: 'ERROR', object: 'file_archive', event: 'Sink write retry', state: 'RECOVERED' },
]

const activeAlerts: ActiveAlert[] = [
  { level: 'ERROR', object: 'wtg-041', event: 'ADS disconnected', since: '2026-09-27 18:46:12', duration: '13m 48s' },
  { level: 'WARN', object: 'turbine-ads-all', event: 'Repeated interval overrun', since: '2026-09-27 18:53:41', duration: '6m 19s' },
]
const ACTIVE_ALERT_DISPLAY_LIMIT = 5
const visibleActiveAlerts = computed(() => activeAlerts.slice(0, ACTIVE_ALERT_DISPLAY_LIMIT))
const hiddenActiveAlertCount = computed(() => Math.max(0, activeAlerts.length - ACTIVE_ALERT_DISPLAY_LIMIT))

const acquisitionTone = computed<Tone>(() => acquisition.success1m >= 99.9 ? 'normal' : acquisition.success1m >= 99 ? 'warning' : 'danger')
const deviceTone = computed<Tone>(() => offline.value === 0 ? 'normal' : offline.value <= 2 ? 'warning' : 'danger')
const taskTone = computed<Tone>(() => stoppedTasks.value === 0 ? 'normal' : 'warning')

const statTone = (onlineCount: number, total: number): Tone => {
  if (total <= 0) return 'muted'
  if (onlineCount === total) return 'normal'
  if (onlineCount === 0) return 'danger'
  return 'warning'
}
</script>

<template>
  <div class="overview-page">
    <div class="head overview-head">
      <div>
        <h1>Overview</h1>
        <p>Wind Hub 运行状态、接入规模与采集健康总览</p>
      </div>
      <div class="color-legend" aria-label="状态颜色含义">
        <span><i class="dot info"></i>运行数据</span>
        <span><i class="dot normal"></i>正常</span>
        <span><i class="dot warning"></i>告警</span>
        <span><i class="dot danger"></i>故障</span>
        <span><i class="dot muted"></i>停用</span>
      </div>
    </div>

    <section class="section-block">
      <div class="section-title">
        <div>
          <span class="eyebrow">SYSTEM STATUS</span>
          <h2>系统运行状态</h2>
        </div>
      </div>

      <div class="primary-grid">
        <article class="industrial-card runtime-card">
          <div class="card-top">
            <span class="card-label">Runtime</span>
            <span class="status-pill normal"><i></i>RUNNING</span>
          </div>
          <div class="hero-value normal">{{ serviceUptime }}</div>
          <div class="hero-caption">Service uptime</div>
          <div class="kv-list">
            <div><span>Last start</span><b class="value info">{{ lastStart }}</b></div>
            <div><span>Last stop</span><b class="value muted">{{ lastStop }}</b></div>
            <div><span>Last reload</span><b class="value info">{{ lastReload }}</b></div>
          </div>
        </article>

        <article class="industrial-card devices-card">
          <div class="card-top">
            <span class="card-label">Devices</span>
            <span class="status-pill" :class="deviceTone"><i></i>{{ offline === 0 ? 'HEALTHY' : 'ATTENTION' }}</span>
          </div>

          <div class="split-main">
            <div>
              <div class="hero-value info">{{ online }} / {{ enabledDevices.length }}</div>
              <div class="hero-caption">Online / Enabled</div>
            </div>
            <div class="mini-stack">
              <span><b class="value danger">{{ offline }}</b> Offline</span>
              <span><b class="value muted">{{ disabledDevices }}</b> Disabled</span>
            </div>
          </div>

          <div class="subsection-caption">Device types</div>
          <div class="compact-bars">
            <div v-for="item in typeStats.slice(0, 3)" :key="item.label">
              <span class="ellipsis" :title="item.label">{{ item.label }}</span>
              <b class="value" :class="statTone(item.online, item.total)">{{ item.online }} / {{ item.total }}</b>
            </div>
          </div>

          <div class="subsection-caption model-caption">Model status</div>
          <div class="compact-bars model-list">
            <div v-for="item in visibleModelStats" :key="item.label">
              <span class="model-name" :title="item.label">{{ item.label }}</span>
              <b class="value" :class="statTone(item.online, item.total)">{{ item.online }} / {{ item.total }}</b>
            </div>
            <div v-if="hiddenModelCount > 0" class="more-row">
              <span>More models</span>
              <b class="value muted">+{{ hiddenModelCount }}</b>
            </div>
          </div>
        </article>

        <article class="industrial-card">
          <div class="card-top">
            <span class="card-label-with-info">
              <span class="card-label">Acquisition</span>
              <el-tooltip placement="bottom-start" effect="dark" :show-after="200" :hide-after="100" :teleported="true">
                <template #content>
                  <div class="metric-tooltip">
                    <div><b>Success / 1 min</b>：最近 1 分钟成功采集次数 / 计划采集次数</div>
                    <div><b>Timeout</b>：最近 1 分钟协议读超时次数</div>
                    <div><b>Overrun</b>：单次采集执行时间超过任务采样周期的次数</div>
                  </div>
                </template>
                <el-icon class="info-icon" aria-label="Acquisition 指标定义"><InfoFilled /></el-icon>
              </el-tooltip>
            </span>
            <span class="status-pill" :class="acquisitionTone"><i></i>{{ acquisition.success1m >= 99.9 ? 'HEALTHY' : 'DEGRADED' }}</span>
          </div>
          <div class="hero-value" :class="acquisitionTone">{{ acquisition.success1m.toFixed(2) }}%</div>
          <div class="hero-caption">Success / 1 min</div>
          <div class="four-metrics">
            <div><span>5 min</span><b class="value normal">{{ acquisition.success5m.toFixed(2) }}%</b></div>
            <div><span>Avg latency</span><b class="value info">{{ acquisition.avgLatencyMs }} ms</b></div>
            <div><span>Timeout</span><b class="value warning">{{ acquisition.timeout1m }}</b></div>
            <div><span>Overrun</span><b class="value warning">{{ acquisition.overrun1m }}</b></div>
          </div>
        </article>

        <article class="industrial-card">
          <div class="card-top">
            <span class="card-label-with-info">
              <span class="card-label">Tasks</span>
              <el-tooltip placement="bottom-start" effect="dark" :show-after="200" :hide-after="100" :teleported="true">
                <template #content>
                  <div class="metric-tooltip">
                    <div><b>Running / Total</b>：当前运行任务数 / 已配置任务总数</div>
                    <div><b>Stopped</b>：当前未运行任务数</div>
                    <div><b>Timeout / 1m</b>：最近 1 分钟协议读超时次数</div>
                    <div><b>Overrun / 1m</b>：最近 1 分钟采集执行时间超过任务采样周期的次数</div>
                  </div>
                </template>
                <el-icon class="info-icon" aria-label="Tasks 指标定义"><InfoFilled /></el-icon>
              </el-tooltip>
            </span>
            <span class="status-pill" :class="taskTone"><i></i>{{ stoppedTasks === 0 ? 'HEALTHY' : 'PARTIAL' }}</span>
          </div>
          <div class="hero-value info">{{ runningTasks }} / {{ store.tasks.length }}</div>
          <div class="hero-caption">Running / Total</div>
          <div class="four-metrics">
            <div><span>Running</span><b class="value normal">{{ runningTasks }}</b></div>
            <div><span>Stopped</span><b class="value muted">{{ stoppedTasks }}</b></div>
            <div><span>Timeout / 1m</span><b class="value warning">{{ acquisition.timeout1m }}</b></div>
            <div><span>Overrun / 1m</span><b class="value warning">{{ acquisition.overrun1m }}</b></div>
          </div>
        </article>

        <article class="industrial-card">
          <div class="card-top">
            <span class="card-label">Sinks</span>
            <span class="status-pill normal"><i></i>AVAILABLE</span>
          </div>
          <div class="hero-value info">{{ enabledSinks }} / {{ store.sinks.length }}</div>
          <div class="hero-caption">Enabled / Configured</div>
          <div class="sink-list">
            <div v-for="sink in sinkRuntime" :key="sink.name">
              <span class="sink-name" :title="sink.name">{{ sink.name }}</span>
              <span class="sink-type">{{ sink.type }}</span>
              <b class="value" :class="sink.tone">{{ sink.state }}</b>
            </div>
          </div>
        </article>

        <article class="industrial-card">
          <div class="card-top">
            <span class="card-label">Host / Process</span>
            <span class="status-pill normal"><i></i>HEALTHY</span>
          </div>
          <div class="resource-grid">
            <div>
              <span>CPU</span>
              <b class="value info">{{ host.cpu }}%</b>
              <div class="meter"><i :style="{ width: `${host.cpu}%` }"></i></div>
            </div>
            <div>
              <span>Memory</span>
              <b class="value info">{{ host.memory }}%</b>
              <div class="meter"><i :style="{ width: `${host.memory}%` }"></i></div>
            </div>
            <div>
              <span>Disk free</span>
              <b class="value normal">{{ host.diskFree }}%</b>
              <div class="meter normal"><i :style="{ width: `${host.diskFree}%` }"></i></div>
            </div>
          </div>
          <div class="health-check">
            <span>Last health check</span>
            <b class="value normal">{{ host.healthCheck }}</b>
          </div>
        </article>
      </div>
    </section>

    <section class="section-block">
      <div class="section-title">
        <div>
          <span class="eyebrow">OPERATIONAL RISKS</span>
          <h2>关键风险</h2>
        </div>
      </div>
      <div class="risk-summary-grid">
        <article class="industrial-card risk-summary-card">
          <div class="card-top"><span class="card-label">Channel Quality</span><span class="status-pill danger"><i></i>2 INTERRUPTED</span></div>
          <div class="risk-summary-main">13 timeouts · 7 reconnects / 24 h</div>
          <el-button link type="primary" @click="emit('navigate','Quality')">Open Quality</el-button>
        </article>
        <article class="industrial-card risk-summary-card">
          <div class="card-top"><span class="card-label">Data Quality</span><span class="status-pill warning"><i></i>DEGRADED</span></div>
          <div class="risk-summary-main">1 stale task · 13 missing cycles</div>
          <el-button link type="primary" @click="emit('navigate','Quality')">Open Quality</el-button>
        </article>
        <article class="industrial-card risk-summary-card">
          <div class="card-top"><span class="card-label">System Health</span><span class="status-pill danger"><i></i>CAPACITY RISK</span></div>
          <div class="risk-summary-main">Disk ~2.7 days · RSS continuous growth</div>
          <el-button link type="primary" @click="emit('navigate','SystemHealth')">Open System Health</el-button>
        </article>
      </div>
    </section>

    <section class="section-block">
      <div class="section-title">
        <div>
          <span class="eyebrow">SYSTEM COVERAGE</span>
          <h2>接入覆盖与采集链路</h2>
        </div>
      </div>

      <div class="coverage-grid">
        <article class="industrial-card compact-card">
          <div class="card-label">Protocols</div>
          <div class="distribution-list">
            <div v-for="item in protocolStats" :key="item.label">
              <span>{{ item.label }}</span>
              <b class="value" :class="statTone(item.online, item.total)">{{ item.online }} / {{ item.total }}</b>
            </div>
          </div>
        </article>

        <article class="industrial-card compact-card">
          <div class="card-label">Configuration</div>
          <div class="kv-list tight">
            <div><span>Config set</span><b class="value info">{{ store.systemInfo.configSet }}</b></div>
            <div><span>Point tables</span><b class="value info">{{ store.pointTables.length }}</b></div>
            <div><span>Point groups</span><b class="value info">{{ store.pointGroups.length }}</b></div>
            <div><span>Units</span><b class="value info">{{ Object.keys(store.units).length }}</b></div>
          </div>
        </article>

        <article class="industrial-card compact-card">
          <div class="card-label-with-info">
            <span class="card-label">Data Timeliness</span>
            <el-tooltip placement="bottom-start" effect="dark" :show-after="200" :hide-after="100" :teleported="true">
              <template #content>
                <div class="metric-tooltip">
                  <div>按当前运行的采集任务统计，每个任务计一次。</div>
                  <div>任务时效取该任务覆盖启用设备中的最差数据年龄。</div>
                  <div><b>Fresh</b>：最差数据年龄 ≤ 1.5 × 任务采样周期</div>
                  <div><b>Delayed</b>：1.5 × 周期 &lt; 最差数据年龄 ≤ 3 × 周期</div>
                  <div><b>Stale</b>：最差数据年龄 &gt; 3 × 任务采样周期</div>
                  <div>停止或未运行的任务不参与统计。</div>
                </div>
              </template>
              <el-icon class="info-icon" aria-label="Data Timeliness 指标定义"><InfoFilled /></el-icon>
            </el-tooltip>
          </div>
          <div class="distribution-list">
            <div><span>Fresh</span><b class="value normal">{{ timeliness.freshTasks }} / {{ timeliness.totalTasks }}</b></div>
            <div><span>Delayed</span><b class="value warning">{{ timeliness.delayedTasks }} / {{ timeliness.totalTasks }}</b></div>
            <div><span>Stale</span><b class="value danger">{{ timeliness.staleTasks }} / {{ timeliness.totalTasks }}</b></div>
            <div><span>Worst task</span><b class="value danger" :title="timeliness.worstTask">{{ timeliness.worstTask }} · {{ timeliness.worstAgeRatio.toFixed(1) }} ×</b></div>
          </div>
        </article>

        <article class="industrial-card compact-card">
          <div class="card-label-with-info">
            <span class="card-label">Communication</span>
            <el-tooltip placement="bottom-start" effect="dark" :show-after="200" :hide-after="100" :teleported="true">
              <template #content>
                <div class="metric-tooltip">
                  <div><b>Disconnected</b>：当前通信链路不可用的设备数</div>
                  <div><b>Timeout / 1m</b>：最近 1 分钟协议读超时次数</div>
                  <div><b>Reconnect / 1m</b>：最近 1 分钟发生的重连次数</div>
                  <div><b>Overrun / 1m</b>：最近 1 分钟采集执行时间超过任务采样周期的次数</div>
                </div>
              </template>
              <el-icon class="info-icon" aria-label="Communication 指标定义"><InfoFilled /></el-icon>
            </el-tooltip>
          </div>
          <div class="distribution-list">
            <div><span>Disconnected</span><b class="value danger">{{ communication.disconnected }}</b></div>
            <div><span>Timeout / 1m</span><b class="value warning">{{ communication.timeout1m }}</b></div>
            <div><span>Reconnect / 1m</span><b class="value info">{{ communication.reconnect1m }}</b></div>
            <div><span>Overrun / 1m</span><b class="value warning">{{ communication.overrun1m }}</b></div>
          </div>
        </article>
      </div>
    </section>

    <section class="section-block alarm-section">
      <div class="section-title">
        <div>
          <span class="eyebrow">EVENTS & ALERTS</span>
          <h2>事件与活动告警</h2>
        </div>
      </div>

      <div class="alarm-grid">
        <article class="industrial-card table-card">
          <div class="table-head">
            <div>
              <h3>最近 1 分钟</h3>
              <p>ERROR / WARN</p>
            </div>
            <span class="count-badge info">{{ recentEvents.length }}</span>
          </div>

          <el-table :data="recentEvents" size="small">
            <el-table-column label="Time" width="92">
              <template #default="s"><span class="mono-cell info">{{ s.row.time }}</span></template>
            </el-table-column>
            <el-table-column label="Level" width="82">
              <template #default="s">
                <span class="level-chip" :class="s.row.level === 'ERROR' ? 'danger' : 'warning'">{{ s.row.level }}</span>
              </template>
            </el-table-column>
            <el-table-column prop="object" label="Object" min-width="145">
              <template #default="s"><span class="mono-cell object-cell">{{ s.row.object }}</span></template>
            </el-table-column>
            <el-table-column prop="event" label="Event" min-width="230">
              <template #default="s"><span class="event-cell">{{ s.row.event }}</span></template>
            </el-table-column>
            <el-table-column label="State" width="96">
              <template #default="s">
                <span class="state-text" :class="s.row.state === 'ACTIVE' ? (s.row.level === 'ERROR' ? 'danger' : 'warning') : 'normal'">{{ s.row.state }}</span>
              </template>
            </el-table-column>
          </el-table>
        </article>

        <article class="industrial-card table-card active-alert-card">
          <div class="table-head">
            <div>
              <h3>活动告警</h3>
              <p>Unresolved / active</p>
            </div>
            <div class="alert-count-wrap">
              <span v-if="hiddenActiveAlertCount" class="muted">showing {{ visibleActiveAlerts.length }}</span>
              <span class="count-badge danger">{{ activeAlerts.length }}</span>
            </div>
          </div>

          <el-table :data="visibleActiveAlerts" size="small">
            <el-table-column label="Level" width="82">
              <template #default="s">
                <span class="level-chip" :class="s.row.level === 'ERROR' ? 'danger' : 'warning'">{{ s.row.level }}</span>
              </template>
            </el-table-column>
            <el-table-column prop="object" label="Object" min-width="145">
              <template #default="s"><span class="mono-cell object-cell">{{ s.row.object }}</span></template>
            </el-table-column>
            <el-table-column prop="event" label="Issue" min-width="230">
              <template #default="s"><span class="event-cell">{{ s.row.event }}</span></template>
            </el-table-column>
            <el-table-column prop="since" label="Since" width="164">
              <template #default="s"><span class="mono-cell">{{ s.row.since }}</span></template>
            </el-table-column>
            <el-table-column label="Duration" width="100">
              <template #default="s"><span class="mono-cell" :class="s.row.level === 'ERROR' ? 'danger' : 'warning'">{{ s.row.duration }}</span></template>
            </el-table-column>
          </el-table>
        </article>
      </div>
    </section>
  </div>
</template>

<style scoped>
.overview-page{--c-text:#182230;--c-label:#697586;--c-faint:#98a2b3;--c-line:#dfe4ea;--c-line-soft:#edf0f3;--c-panel:#fff;--c-info:#2563a6;--c-normal:#1f7a4d;--c-warning:#b26a00;--c-danger:#c23a3a;--c-muted:#7b8492;color:var(--c-text)}
.overview-head{align-items:flex-end}.color-legend{display:flex;flex-wrap:wrap;justify-content:flex-end;gap:12px;color:var(--c-label);font-size:var(--font-size-body)}.color-legend span{display:inline-flex;align-items:center;gap:5px}.dot{width:7px;height:7px;border-radius:50%;display:inline-block;background:var(--c-muted)}.dot.info{background:var(--c-info)}.dot.normal{background:var(--c-normal)}.dot.warning{background:var(--c-warning)}.dot.danger{background:var(--c-danger)}.dot.muted{background:var(--c-muted)}
.section-block{margin-top:22px}.section-title{display:flex;align-items:flex-end;justify-content:space-between;margin-bottom:10px;padding:0 2px}.section-title h2{margin:2px 0 0;font-size:var(--font-size-section-lg);font-weight:var(--font-weight-bold);letter-spacing:.01em}.eyebrow{display:block;color:var(--c-faint);font-size:var(--font-size-caption);font-weight:var(--font-weight-bold);letter-spacing:.14em}
.primary-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px}.risk-summary-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px}.risk-summary-main{margin:var(--app-space-3) 0;color:var(--app-text-regular);font-size:var(--app-font-body);font-weight:var(--app-font-weight-semibold)}.coverage-grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px}.industrial-card{position:relative;min-width:0;padding:16px;background:var(--c-panel);border:1px solid var(--c-line);border-radius:8px;box-shadow:0 1px 2px rgba(16,24,40,.03);overflow:hidden}.industrial-card::before{content:'';position:absolute;top:-1px;left:16px;width:34px;height:2px;background:#718096}.runtime-card::before,.industrial-card:has(.status-pill.normal)::before{background:var(--c-normal)}.industrial-card:has(.status-pill.warning)::before{background:var(--c-warning)}.industrial-card:has(.status-pill.danger)::before{background:var(--c-danger)}
.card-top{min-height:24px;display:flex;align-items:center;justify-content:space-between;gap:12px}.card-label{color:var(--c-label);font-size:var(--font-size-body);font-weight:var(--font-weight-bold);letter-spacing:.04em;text-transform:uppercase}.card-label-with-info{display:inline-flex;align-items:center;gap:5px;min-width:0}.info-icon{flex:0 0 auto;color:var(--c-faint);font-size:var(--font-size-subsection);cursor:help;transition:color .15s ease}.info-icon:hover{color:var(--c-label)}.metric-tooltip{max-width:340px;display:grid;gap:6px;font-size:var(--font-size-label);line-height:1.5}.metric-tooltip b{font-weight:var(--font-weight-bold)}.status-pill{display:inline-flex;align-items:center;gap:6px;padding:3px 7px;border:1px solid currentColor;border-radius:4px;font-size:var(--font-size-caption);font-weight:var(--font-weight-bold);letter-spacing:.05em;background:#fff}.status-pill i{width:6px;height:6px;border-radius:50%;background:currentColor}
.hero-value{margin-top:12px;font-family:"SFMono-Regular",Consolas,monospace;font-size:var(--font-size-overview-metric);line-height:1.15;font-weight:var(--font-weight-bold);letter-spacing:-.02em;font-variant-numeric:tabular-nums}.hero-caption{margin-top:4px;color:var(--c-label);font-size:var(--font-size-body)}.split-main{display:flex;align-items:flex-end;justify-content:space-between;gap:16px}.mini-stack{display:grid;gap:4px;color:var(--c-label);font-size:var(--font-size-label);text-align:right}
.value,.mono-cell{font-family:"SFMono-Regular",Consolas,monospace;font-variant-numeric:tabular-nums}.info{color:var(--c-info)!important}.normal{color:var(--c-normal)!important}.warning{color:var(--c-warning)!important}.danger{color:var(--c-danger)!important}.muted{color:var(--c-muted)!important}
.kv-list{margin-top:14px;border-top:1px solid var(--c-line-soft)}.kv-list>div,.compact-bars>div,.distribution-list>div,.sink-list>div,.health-check{display:flex;align-items:center;justify-content:space-between;gap:10px;min-height:28px;color:var(--c-label);font-size:var(--font-size-label);border-bottom:1px solid var(--c-line-soft)}.kv-list>div:last-child,.compact-bars>div:last-child,.distribution-list>div:last-child,.sink-list>div:last-child{border-bottom:0}.kv-list .value,.compact-bars .value,.distribution-list .value,.health-check .value{color:var(--c-text);font-size:var(--font-size-label);font-weight:var(--font-weight-bold)}.kv-list.tight{margin-top:9px}
.subsection-caption{margin-top:12px;color:var(--c-faint);font-size:var(--font-size-micro);font-weight:var(--font-weight-bold);letter-spacing:.1em;text-transform:uppercase}.model-caption{margin-top:9px}.compact-bars{margin-top:4px;border-top:1px solid var(--c-line-soft)}.ellipsis,.model-name{min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.model-name{max-width:72%}.more-row{color:var(--c-faint)!important}
.four-metrics{display:grid;grid-template-columns:repeat(2,1fr);gap:0;margin-top:13px;border-top:1px solid var(--c-line-soft);border-left:1px solid var(--c-line-soft)}.four-metrics>div{min-width:0;padding:8px 9px;border-right:1px solid var(--c-line-soft);border-bottom:1px solid var(--c-line-soft)}.four-metrics span,.resource-grid span{display:block;color:var(--c-label);font-size:var(--font-size-caption)}.four-metrics b{display:block;margin-top:3px;font-size:var(--font-size-subsection)}
.sink-list{margin-top:10px;border-top:1px solid var(--c-line-soft)}.sink-name{min-width:0;overflow:hidden;color:var(--c-text);text-overflow:ellipsis;white-space:nowrap}.sink-type{margin-left:auto;color:var(--c-faint);font-size:var(--font-size-caption)}.sink-list b{min-width:64px;text-align:right;font-size:var(--font-size-caption)}
.resource-grid{display:grid;gap:11px;margin-top:13px}.resource-grid>div{display:grid;grid-template-columns:72px 70px 1fr;align-items:center;gap:8px}.resource-grid b{text-align:right;font-size:var(--font-size-body)}.meter{height:5px;overflow:hidden;background:#edf1f5;border-radius:2px}.meter i{display:block;height:100%;background:var(--c-info)}.meter.normal i{background:var(--c-normal)}.health-check{margin-top:13px;padding-top:4px;border-top:1px solid var(--c-line-soft);border-bottom:0}.compact-card{min-height:158px}.distribution-list{margin-top:9px;border-top:1px solid var(--c-line-soft)}
.alarm-grid{display:grid;grid-template-columns:1fr 1fr;gap:12px}.table-card{padding:0 14px 12px}.table-card::before{left:14px;background:var(--c-warning)}.active-alert-card::before{background:var(--c-danger)}.alert-count-wrap{display:flex;align-items:center;gap:8px}.alert-count-wrap .muted{font-size:var(--font-size-caption)}
.table-head{min-height:64px;display:flex;align-items:center;justify-content:space-between;gap:12px}.table-head h3{margin:0;font-size:var(--font-size-section)}.table-head p{margin:3px 0 0;color:var(--c-faint);font-size:var(--font-size-caption)}.count-badge{min-width:28px;padding:3px 7px;border:1px solid currentColor;border-radius:4px;text-align:center;font-family:"SFMono-Regular",Consolas,monospace;font-size:var(--font-size-label);font-weight:var(--font-weight-bold)}.level-chip{display:inline-block;min-width:50px;padding:2px 5px;border:1px solid currentColor;border-radius:3px;text-align:center;font-size:var(--font-size-micro);font-weight:var(--font-weight-bold);line-height:1.35}.state-text{font-family:"SFMono-Regular",Consolas,monospace;font-size:var(--font-size-micro);font-weight:var(--font-weight-bold);letter-spacing:.03em}.mono-cell{font-size:var(--font-size-label);font-weight:var(--font-weight-semibold)}.object-cell{color:#344054}.event-cell{color:#344054;font-family:Inter,ui-sans-serif,system-ui;font-size:var(--font-size-body);font-weight:var(--font-weight-regular)}
:deep(.el-table){--el-table-border-color:var(--c-line-soft);--el-table-header-bg-color:#f7f9fb;--el-table-row-hover-bg-color:#f8fafc;color:var(--c-text);font-size:var(--font-size-body)}:deep(.el-table th.el-table__cell){height:34px;color:var(--c-label);font-size:var(--font-size-caption);font-weight:var(--font-weight-bold);letter-spacing:.02em}:deep(.el-table td.el-table__cell){padding:7px 0}:deep(.el-table .cell){line-height:1.3}
@media(max-width:1450px){.primary-grid{grid-template-columns:repeat(2,minmax(0,1fr))}.risk-summary-grid{grid-template-columns:1fr}.coverage-grid{grid-template-columns:repeat(2,minmax(0,1fr))}.alarm-grid{grid-template-columns:1fr}}@media(max-width:900px){.overview-head,.section-title{align-items:flex-start}.color-legend{display:none}.primary-grid,.coverage-grid{grid-template-columns:1fr}}
</style>
