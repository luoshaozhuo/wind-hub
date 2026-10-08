<script setup lang="ts">
import { computed, ref } from 'vue'
import { useQuery } from '@tanstack/vue-query'
import { InfoFilled } from '@element-plus/icons-vue'
import { fetchLogs } from '../api/monitoring'
import { qk } from '../api/queryKeys'
import { useQuality } from '../composables/useQuality'
import { useSystemHealth } from '../composables/useSystemHealth'
import { useViewport } from '../composables/useViewport'
import { protocolOfDevice } from '../domain/devices'
import { useConfigStore } from '../stores/config'

const { isMobile, isTablet } = useViewport()
const configStore = useConfigStore()

// Overview 页面查询每 1s 更新；全局运行态快照亦按 1s 更新。
const OVERVIEW_REFETCH_MS = 1000

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

const quality1h = useQuality(ref('1 h'), OVERVIEW_REFETCH_MS)
const quality24h = useQuality(ref('24 h'), OVERVIEW_REFETCH_MS)
const health = useSystemHealth(ref('24 h'), OVERVIEW_REFETCH_MS)
const healthRisks = health.risks

interface LogEntry {
  time: string
  level: 'ERROR' | 'WARN' | 'INFO'
  source: string
  object: string
  message: string
}
const logsQuery = useQuery({
  queryKey: qk.logs({ page: 1, pageSize: 200 }),
  queryFn: () => fetchLogs({ page: 1, pageSize: 200 }),
  refetchInterval: OVERVIEW_REFETCH_MS,
})
const logStore = computed<LogEntry[]>(() =>
  (logsQuery.data.value?.items || []).map((row) => ({
    time: row.timestamp.replace('T', ' ').replace('Z', '').slice(0, 19),
    level: (row.level === 'WARNING' ? 'WARN' : row.level) as LogEntry['level'],
    source: row.source,
    object: row.object,
    message: row.message,
  })),
)

const lastStart = computed(() => {
  const current = health.current.value
  if (!current.sampledAt || !current.uptimeSeconds) return '—'
  return new Date(new Date(current.sampledAt).getTime() - current.uptimeSeconds * 1000)
    .toISOString()
    .replace('T', ' ')
    .slice(0, 19)
})
const lastStop = '—'
const lastReload = '—'

const formatDuration = (ms: number) => {
  const total = Math.max(0, Math.floor(ms / 1000))
  const days = Math.floor(total / 86400)
  const hours = Math.floor((total % 86400) / 3600)
  const minutes = Math.floor((total % 3600) / 60)
  const seconds = total % 60
  return `${days}d ${String(hours).padStart(2, '0')}:${String(minutes).padStart(2, '0')}:${String(seconds).padStart(2, '0')}`
}

const serviceUptime = computed(() => formatDuration(health.current.value.uptimeSeconds * 1000))

const enabledDevices = computed(() => configStore.devices.filter((d) => d.enabled))
const online = computed(() => enabledDevices.value.filter((d) => d.online).length)
const offline = computed(() => enabledDevices.value.filter((d) => !d.online).length)
const disabledDevices = computed(() => configStore.devices.filter((d) => !d.enabled).length)

const runningTasks = computed(() => configStore.tasks.filter((t) => t.runtime === 'RUNNING').length)
const stoppedTasks = computed(() => configStore.tasks.filter((t) => t.runtime !== 'RUNNING').length)
const enabledSinks = computed(() => configStore.sinks.filter((s) => s.enabled).length)

const typeStats = computed(() =>
  configStore.deviceTypes
    .map((type) => {
      const devices = configStore.devices.filter((device) => {
        const model = configStore.deviceModels.find((m) => m.id === device.model)
        return model?.device_type === type.id
      })
      return {
        label: type.name,
        total: devices.length,
        online: devices.filter((d) => d.enabled && d.online).length,
      }
    })
    .filter((x) => x.total > 0),
)

const allModelStats = computed(() =>
  configStore.deviceModels
    .map((model) => {
      const devices = configStore.devices.filter((d) => d.model === model.id)
      const enabled = devices.filter((d) => d.enabled)
      return {
        label: model.id,
        total: enabled.length,
        online: enabled.filter((d) => d.online).length,
        offline: enabled.filter((d) => !d.online).length,
      }
    })
    .filter((x) => x.total > 0)
    .sort((a, b) => {
      if (a.offline > 0 !== b.offline > 0) return a.offline > 0 ? -1 : 1
      return b.total - a.total
    }),
)

const visibleModelStats = computed(() => allModelStats.value.slice(0, 4))
const hiddenModelCount = computed(() =>
  Math.max(0, allModelStats.value.length - visibleModelStats.value.length),
)

const protocolStats = computed(() => {
  const protocols = ['ads', 'modbus', 'iec104']
  return protocols
    .map((protocol) => {
      const devices = configStore.devices.filter(
        (d) => protocolOfDevice(configStore, d) === protocol,
      )
      return {
        label: protocol.toUpperCase(),
        total: devices.length,
        online: devices.filter((d) => d.enabled && d.online).length,
      }
    })
    .filter((x) => x.total > 0)
})

// Overview 所有运行指标从统一后端事实源派生：
// useQuality / useSystemHealth / logs query / configStore 运行态字段。
const overviewQuality = quality1h.view
const riskQuality24h = quality24h.view
const channelSummaryValue = (key: string) =>
  overviewQuality.value.channelSummary.find((x) => x.key === key)?.value ?? 0
const dataMetricValue = (key: string) =>
  overviewQuality.value.dataMetrics.find((x) => x.key === key)?.value ?? 0

const acquisition = computed(() => {
  const channels = quality1h.view.value.acquisition
  const enabled = channels.filter((row) => row.state !== 'Disabled')
  const healthy = enabled.filter((row) => row.state === 'Healthy').length
  const availability = enabled.length ? (healthy / enabled.length) * 100 : 100
  const latencies = enabled
    .map((row) => Number.parseFloat(row.latency))
    .filter((value) => Number.isFinite(value))
    .sort((a, b) => a - b)
  const avg = latencies.length
    ? latencies.reduce((sum, value) => sum + value, 0) / latencies.length
    : 0
  const p95 = latencies.length
    ? latencies[Math.min(latencies.length - 1, Math.ceil(latencies.length * 0.95) - 1)]
    : 0
  const timelinessDimension = overviewQuality.value.dimensions.find(
    (row) => row.key === 'timeliness',
  )
  const overrunText = timelinessDimension?.metric || ''
  const overrunMatch = overrunText.match(/^(\d+)/)
  return {
    availability,
    avgLatencyMs: avg,
    p95LatencyMs: p95,
    timeouts: channelSummaryValue('timeouts'),
    reconnects: channelSummaryValue('reconnects'),
    overruns: Number(overrunMatch?.[1] || 0),
  }
})

const host = health.current

const timeliness = computed(() => {
  const running = configStore.tasks.filter((t) => t.runtime === 'RUNNING')
  const stale = running.filter((t) =>
    overviewQuality.value.issues.some(
      (i) => i.kind === 'Task' && i.object === t.task_id && i.dimension === 'Continuity',
    ),
  )
  return {
    totalTasks: running.length,
    freshTasks: running.length - stale.length,
    delayedTasks: 0,
    staleTasks: stale.length,
    worstTask: stale[0]?.task_id || '—',
    worstAgeRatio: null as number | null,
  }
})

const communication = computed(() => ({
  disconnected: channelSummaryValue('interrupted'),
  timeout1m: acquisition.value.timeouts,
  reconnect1m: acquisition.value.reconnects,
  overrun1m: acquisition.value.overruns,
}))

const sinkToneOf = (state: string): Tone =>
  state === 'healthy'
    ? 'normal'
    : state === 'warning'
      ? 'warning'
      : state === 'failed'
        ? 'danger'
        : 'muted'
const sinkRuntime = computed(() =>
  configStore.sinks.map((s) => ({
    name: s.name,
    type: s.type,
    state: s.enabled ? s.runtime_state.toUpperCase() : 'DISABLED',
    tone: (s.enabled ? sinkToneOf(s.runtime_state) : 'muted') as Tone,
  })),
)
const sinkCardTone = computed<Tone>(() =>
  configStore.sinks.some((s) => s.enabled && s.runtime_state === 'failed')
    ? 'danger'
    : configStore.sinks.some((s) => s.enabled && s.runtime_state === 'warning')
      ? 'warning'
      : 'normal',
)

// 对象当前是否仍处于故障场景：决定 Recent Events 的 ACTIVE / RECOVERED。
function isActiveObject(object: string) {
  const device = configStore.devices.find((d) => d.device_id === object)
  if (device) return !device.enabled || !device.online
  const sink = configStore.sinks.find((s) => s.name === object)
  if (sink) return sink.enabled && sink.runtime_state === 'failed'
  return false
}

const recentEvents = computed<RecentEvent[]>(() =>
  logStore.value
    .filter((e) => e.level !== 'INFO')
    .slice(0, 4)
    .map((e) => ({
      time: e.time.slice(11),
      level: e.level as 'ERROR' | 'WARN',
      object: e.object,
      event: e.message,
      state: isActiveObject(e.object) ? ('ACTIVE' as const) : ('RECOVERED' as const),
    })),
)

const activeAlerts = computed<ActiveAlert[]>(() =>
  overviewQuality.value.issues.map((issue) => ({
    level: issue.level === 'Fault' ? ('ERROR' as const) : ('WARN' as const),
    object: issue.object,
    event: `${issue.issue} · ${issue.dimension}`,
    since: logStore.value.find((e) => e.object === issue.object)?.time || '—',
    duration: issue.duration,
  })),
)
const ACTIVE_ALERT_DISPLAY_LIMIT = 5
const visibleActiveAlerts = computed(() => activeAlerts.value.slice(0, ACTIVE_ALERT_DISPLAY_LIMIT))
const hiddenActiveAlertCount = computed(() =>
  Math.max(0, activeAlerts.value.length - ACTIVE_ALERT_DISPLAY_LIMIT),
)

const acquisitionTone = computed<Tone>(() =>
  acquisition.value.availability >= 99.9
    ? 'normal'
    : acquisition.value.availability >= 99
      ? 'warning'
      : 'danger',
)
const deviceTone = computed<Tone>(() =>
  offline.value === 0 ? 'normal' : offline.value <= 2 ? 'warning' : 'danger',
)
const taskTone = computed<Tone>(() => (stoppedTasks.value === 0 ? 'normal' : 'warning'))

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
            <span class="ov-status-pill normal"><i></i>RUNNING</span>
          </div>
          <div class="hero-value normal">{{ serviceUptime }}</div>
          <div class="hero-caption">Service uptime</div>
          <div class="kv-list">
            <div>
              <span>Last start</span><b class="value info">{{ lastStart }}</b>
            </div>
            <div>
              <span>Last stop</span><b class="value muted">{{ lastStop }}</b>
            </div>
            <div>
              <span>Last reload</span><b class="value info">{{ lastReload }}</b>
            </div>
          </div>
        </article>

        <article class="industrial-card devices-card">
          <div class="card-top">
            <span class="card-label">Devices</span>
            <span class="ov-status-pill" :class="deviceTone"
              ><i></i>{{ offline === 0 ? 'HEALTHY' : 'ATTENTION' }}</span
            >
          </div>

          <div class="split-main">
            <div>
              <div class="hero-value info">{{ online }} / {{ enabledDevices.length }}</div>
              <div class="hero-caption">Online / Enabled</div>
            </div>
            <div class="mini-stack">
              <span
                ><b class="value danger">{{ offline }}</b> Offline</span
              >
              <span
                ><b class="value muted">{{ disabledDevices }}</b> Disabled</span
              >
            </div>
          </div>

          <div class="subsection-caption">Device types</div>
          <div class="compact-bars">
            <div v-for="item in typeStats.slice(0, 3)" :key="item.label">
              <span class="ellipsis" :title="item.label">{{ item.label }}</span>
              <b class="value" :class="statTone(item.online, item.total)"
                >{{ item.online }} / {{ item.total }}</b
              >
            </div>
          </div>

          <div class="subsection-caption model-caption">Model status</div>
          <div class="compact-bars model-list">
            <div v-for="item in visibleModelStats" :key="item.label">
              <span class="model-name" :title="item.label">{{ item.label }}</span>
              <b class="value" :class="statTone(item.online, item.total)"
                >{{ item.online }} / {{ item.total }}</b
              >
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
              <el-tooltip
                placement="bottom-start"
                effect="dark"
                :show-after="200"
                :hide-after="100"
                :teleported="true"
              >
                <template #content>
                  <div class="metric-tooltip">
                    <div><b>Current availability</b>：当前健康采集通道数 / 当前启用采集通道数</div>
                    <div><b>Timeout</b>：最近 1 分钟协议读超时次数</div>
                    <div><b>Overrun</b>：单次采集执行时间超过任务采样周期的次数</div>
                  </div>
                </template>
                <el-icon class="info-icon" aria-label="Acquisition 指标定义"
                  ><InfoFilled
                /></el-icon>
              </el-tooltip>
            </span>
            <span class="ov-status-pill" :class="acquisitionTone"
              ><i></i>{{ acquisition.availability >= 99.9 ? 'HEALTHY' : 'DEGRADED' }}</span
            >
          </div>
          <div class="hero-value" :class="acquisitionTone">
            {{ acquisition.availability.toFixed(2) }}%
          </div>
          <div class="hero-caption">Current availability</div>
          <div class="four-metrics">
            <div>
              <span>P95 latency</span
              ><b class="value info">{{ acquisition.p95LatencyMs.toFixed(0) }} ms</b>
            </div>
            <div>
              <span>Avg latency</span><b class="value info">{{ acquisition.avgLatencyMs }} ms</b>
            </div>
            <div>
              <span>Timeout / 1 h</span><b class="value warning">{{ acquisition.timeouts }}</b>
            </div>
            <div>
              <span>Overrun / 1 h</span><b class="value warning">{{ acquisition.overruns }}</b>
            </div>
          </div>
        </article>

        <article class="industrial-card">
          <div class="card-top">
            <span class="card-label-with-info">
              <span class="card-label">Tasks</span>
              <el-tooltip
                placement="bottom-start"
                effect="dark"
                :show-after="200"
                :hide-after="100"
                :teleported="true"
              >
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
            <span class="ov-status-pill" :class="taskTone"
              ><i></i>{{ stoppedTasks === 0 ? 'HEALTHY' : 'PARTIAL' }}</span
            >
          </div>
          <div class="hero-value info">{{ runningTasks }} / {{ configStore.tasks.length }}</div>
          <div class="hero-caption">Running / Total</div>
          <div class="four-metrics">
            <div>
              <span>Running</span><b class="value normal">{{ runningTasks }}</b>
            </div>
            <div>
              <span>Stopped</span><b class="value muted">{{ stoppedTasks }}</b>
            </div>
            <div>
              <span>Timeout / 1m</span><b class="value warning">{{ acquisition.timeouts }}</b>
            </div>
            <div>
              <span>Overrun / 1m</span><b class="value warning">{{ acquisition.overruns }}</b>
            </div>
          </div>
        </article>

        <article class="industrial-card">
          <div class="card-top">
            <span class="card-label">Sinks</span>
            <span class="ov-status-pill" :class="sinkCardTone"
              ><i></i>{{ sinkCardTone === 'normal' ? 'AVAILABLE' : 'ATTENTION' }}</span
            >
          </div>
          <div class="hero-value info">{{ enabledSinks }} / {{ configStore.sinks.length }}</div>
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
            <span class="ov-status-pill normal"><i></i>HEALTHY</span>
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
          <div class="card-top">
            <span class="card-label">Channel Quality</span
            ><span
              class="ov-status-pill"
              :class="channelSummaryValue('interrupted') ? 'danger' : 'normal'"
              ><i></i>{{ channelSummaryValue('interrupted') }} INTERRUPTED</span
            >
          </div>
          <div class="risk-summary-main">
            {{ riskQuality24h.channelSummary.find((x) => x.key === 'timeouts')?.value ?? 0 }}
            timeouts ·
            {{ riskQuality24h.channelSummary.find((x) => x.key === 'reconnects')?.value ?? 0 }}
            reconnects / 24 h
          </div>
        </article>
        <article class="industrial-card risk-summary-card">
          <div class="card-top">
            <span class="card-label">Data Quality</span
            ><span class="ov-status-pill" :class="dataMetricValue('stale') ? 'danger' : 'normal'"
              ><i></i>{{ dataMetricValue('stale') ? 'DEGRADED' : 'HEALTHY' }}</span
            >
          </div>
          <div class="risk-summary-main">
            {{ dataMetricValue('stale') }} stale task · {{ dataMetricValue('missing') }} missing
            cycles
          </div>
        </article>
        <article class="industrial-card risk-summary-card">
          <div class="card-top">
            <span class="card-label">System Health</span
            ><span class="ov-status-pill danger"><i></i>CAPACITY RISK</span>
          </div>
          <div class="risk-summary-main">
            {{ healthRisks[1]?.summary ?? '—' }} · {{ healthRisks[0]?.summary ?? '—' }}
          </div>
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
              <b class="value" :class="statTone(item.online, item.total)"
                >{{ item.online }} / {{ item.total }}</b
              >
            </div>
          </div>
        </article>

        <article class="industrial-card compact-card">
          <div class="card-label">Configuration</div>
          <div class="kv-list tight">
            <div>
              <span>Config set</span
              ><b class="value info">{{ configStore.systemInfo.configSet }}</b>
            </div>
            <div>
              <span>Point tables</span
              ><b class="value info">{{ configStore.pointTables.length }}</b>
            </div>
            <div>
              <span>Point groups</span
              ><b class="value info">{{ configStore.pointGroups.length }}</b>
            </div>
            <div>
              <span>Units</span
              ><b class="value info">{{ Object.keys(configStore.units).length }}</b>
            </div>
          </div>
        </article>

        <article class="industrial-card compact-card">
          <div class="card-label-with-info">
            <span class="card-label">Data Timeliness</span>
            <el-tooltip
              placement="bottom-start"
              effect="dark"
              :show-after="200"
              :hide-after="100"
              :teleported="true"
            >
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
              <el-icon class="info-icon" aria-label="Data Timeliness 指标定义"
                ><InfoFilled
              /></el-icon>
            </el-tooltip>
          </div>
          <div class="distribution-list">
            <div>
              <span>Fresh</span
              ><b class="value normal">{{ timeliness.freshTasks }} / {{ timeliness.totalTasks }}</b>
            </div>
            <div>
              <span>Delayed</span
              ><b class="value warning"
                >{{ timeliness.delayedTasks }} / {{ timeliness.totalTasks }}</b
              >
            </div>
            <div>
              <span>Stale</span
              ><b class="value danger">{{ timeliness.staleTasks }} / {{ timeliness.totalTasks }}</b>
            </div>
            <div>
              <span>Worst task</span
              ><b class="value danger" :title="timeliness.worstTask">{{ timeliness.worstTask }}</b>
            </div>
          </div>
        </article>

        <article class="industrial-card compact-card">
          <div class="card-label-with-info">
            <span class="card-label">Communication</span>
            <el-tooltip
              placement="bottom-start"
              effect="dark"
              :show-after="200"
              :hide-after="100"
              :teleported="true"
            >
              <template #content>
                <div class="metric-tooltip">
                  <div><b>Disconnected</b>：当前通信链路不可用的设备数</div>
                  <div><b>Timeout / 1m</b>：最近 1 分钟协议读超时次数</div>
                  <div><b>Reconnect / 1m</b>：最近 1 分钟发生的重连次数</div>
                  <div><b>Overrun / 1m</b>：最近 1 分钟采集执行时间超过任务采样周期的次数</div>
                </div>
              </template>
              <el-icon class="info-icon" aria-label="Communication 指标定义"
                ><InfoFilled
              /></el-icon>
            </el-tooltip>
          </div>
          <div class="distribution-list">
            <div>
              <span>Disconnected</span><b class="value danger">{{ communication.disconnected }}</b>
            </div>
            <div>
              <span>Timeout / 1m</span><b class="value warning">{{ communication.timeout1m }}</b>
            </div>
            <div>
              <span>Reconnect / 1m</span><b class="value info">{{ communication.reconnect1m }}</b>
            </div>
            <div>
              <span>Overrun / 1m</span><b class="value warning">{{ communication.overrun1m }}</b>
            </div>
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
            <el-table-column v-if="!isTablet" label="Time" width="92">
              <template #default="s"
                ><span class="mono-cell info">{{ s.row.time }}</span></template
              >
            </el-table-column>
            <el-table-column label="Level" width="82">
              <template #default="s">
                <span class="level-chip" :class="s.row.level === 'ERROR' ? 'danger' : 'warning'">{{
                  s.row.level
                }}</span>
              </template>
            </el-table-column>
            <el-table-column prop="object" label="Object" min-width="145">
              <template #default="s"
                ><span class="mono-cell object-cell">{{ s.row.object }}</span></template
              >
            </el-table-column>
            <el-table-column prop="event" label="Event" min-width="230">
              <template #default="s"
                ><span class="event-cell">{{ s.row.event }}</span></template
              >
            </el-table-column>
            <el-table-column label="State" width="96">
              <template #default="s">
                <span
                  class="state-text"
                  :class="
                    s.row.state === 'ACTIVE'
                      ? s.row.level === 'ERROR'
                        ? 'danger'
                        : 'warning'
                      : 'normal'
                  "
                  >{{ s.row.state }}</span
                >
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
              <span v-if="hiddenActiveAlertCount" class="muted"
                >showing {{ visibleActiveAlerts.length }}</span
              >
              <span class="count-badge danger">{{ activeAlerts.length }}</span>
            </div>
          </div>

          <el-table :data="visibleActiveAlerts" size="small">
            <el-table-column label="Level" width="82">
              <template #default="s">
                <span class="level-chip" :class="s.row.level === 'ERROR' ? 'danger' : 'warning'">{{
                  s.row.level
                }}</span>
              </template>
            </el-table-column>
            <el-table-column prop="object" label="Object" min-width="145">
              <template #default="s"
                ><span class="mono-cell object-cell">{{ s.row.object }}</span></template
              >
            </el-table-column>
            <el-table-column prop="event" label="Issue" min-width="230">
              <template #default="s"
                ><span class="event-cell">{{ s.row.event }}</span></template
              >
            </el-table-column>
            <el-table-column v-if="!isTablet" prop="since" label="Since" width="164">
              <template #default="s"
                ><span class="mono-cell">{{ s.row.since }}</span></template
              >
            </el-table-column>
            <el-table-column v-if="!isMobile" label="Duration" width="100">
              <template #default="s"
                ><span class="mono-cell" :class="s.row.level === 'ERROR' ? 'danger' : 'warning'">{{
                  s.row.duration
                }}</span></template
              >
            </el-table-column>
          </el-table>
        </article>
      </div>
    </section>
  </div>
</template>

<style scoped>
.overview-page {
  color: var(--app-text-primary);
}
.overview-head {
  align-items: flex-end;
}
.color-legend {
  display: flex;
  flex-wrap: wrap;
  justify-content: flex-end;
  gap: var(--app-space-3);
  color: var(--app-text-secondary);
  font-size: var(--app-font-body);
}
.color-legend span {
  display: inline-flex;
  align-items: center;
  gap: var(--app-space-1);
}
.dot {
  width: var(--app-status-dot-size);
  height: var(--app-status-dot-size);
  border-radius: 50%;
  display: inline-block;
  background: var(--app-status-disabled);
}
.dot.info {
  background: var(--app-status-info);
}
.dot.normal {
  background: var(--app-status-healthy);
}
.dot.warning {
  background: var(--app-status-warning);
}
.dot.danger {
  background: var(--app-status-fault);
}
.dot.muted {
  background: var(--app-status-disabled);
}
.section-block {
  margin-top: var(--app-space-6);
}
.section-title {
  display: flex;
  align-items: flex-end;
  justify-content: space-between;
  margin-bottom: var(--app-space-3);
  padding: 0 var(--app-space-1);
}
.section-title h2 {
  margin: var(--app-space-1) 0 0;
  font-size: var(--app-font-section-title);
  font-weight: var(--app-font-weight-bold);
  letter-spacing: 0.01em;
}
.eyebrow {
  display: block;
  color: var(--app-text-muted);
  font-size: var(--app-font-caption);
  font-weight: var(--app-font-weight-bold);
  letter-spacing: 0.14em;
}
.primary-grid {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: var(--app-space-3);
}
.risk-summary-grid {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: var(--app-space-3);
}
.risk-summary-main {
  margin: var(--app-space-3) 0;
  color: var(--app-text-regular);
  font-size: var(--app-font-body);
  font-weight: var(--app-font-weight-semibold);
}
.coverage-grid {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: var(--app-space-3);
}
.industrial-card {
  position: relative;
  min-width: 0;
  padding: var(--app-space-4);
  background: var(--app-bg-surface);
  border: 1px solid var(--app-border);
  border-radius: var(--app-dialog-radius);
  box-shadow: var(--app-elevation-soft);
  overflow: hidden;
}
.industrial-card::before {
  content: '';
  position: absolute;
  top: -1px;
  left: var(--app-card-padding);
  width: var(--app-accent-width);
  height: var(--app-accent-height);
  background: var(--app-text-secondary);
}
.runtime-card::before,
.industrial-card:has(.ov-status-pill.normal)::before {
  background: var(--app-status-healthy);
}
.industrial-card:has(.ov-status-pill.warning)::before {
  background: var(--app-status-warning);
}
.industrial-card:has(.ov-status-pill.danger)::before {
  background: var(--app-status-fault);
}
.card-top {
  min-height: var(--app-space-6);
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--app-space-3);
}
.card-label {
  color: var(--app-text-secondary);
  font-size: var(--app-font-body);
  font-weight: var(--app-font-weight-bold);
  letter-spacing: 0.04em;
  text-transform: uppercase;
}
.card-label-with-info {
  display: inline-flex;
  align-items: center;
  gap: var(--app-space-1);
  min-width: 0;
}
.info-icon {
  flex: 0 0 auto;
  color: var(--app-text-muted);
  font-size: var(--app-font-panel-title);
  cursor: help;
  transition: color 0.15s ease;
}
.info-icon:hover {
  color: var(--app-text-secondary);
}
.metric-tooltip {
  max-width: var(--app-tooltip-max-width);
  display: grid;
  gap: var(--app-space-2);
  font-size: var(--app-font-label);
  line-height: 1.5;
}
.metric-tooltip b {
  font-weight: var(--app-font-weight-bold);
}
.ov-status-pill {
  display: inline-flex;
  align-items: center;
  gap: var(--app-space-2);
  padding: var(--app-space-1) var(--app-space-2);
  border: 1px solid currentColor;
  border-radius: var(--app-control-radius);
  font-size: var(--app-font-caption);
  font-weight: var(--app-font-weight-bold);
  letter-spacing: 0.05em;
  background: var(--app-bg-surface);
}
.ov-status-pill i {
  width: var(--app-status-dot-size);
  height: var(--app-status-dot-size);
  border-radius: 50%;
  background: currentColor;
}
.hero-value {
  margin-top: var(--app-space-3);
  font-family: 'SFMono-Regular', Consolas, monospace;
  font-size: var(--app-font-metric-lg);
  line-height: 1.15;
  font-weight: var(--app-font-weight-bold);
  letter-spacing: -0.02em;
  font-variant-numeric: tabular-nums;
}
.hero-caption {
  margin-top: var(--app-space-1);
  color: var(--app-text-secondary);
  font-size: var(--app-font-body);
}
.split-main {
  display: flex;
  align-items: flex-end;
  justify-content: space-between;
  gap: var(--app-space-4);
}
.mini-stack {
  display: grid;
  gap: var(--app-space-1);
  color: var(--app-text-secondary);
  font-size: var(--app-font-label);
  text-align: right;
}
.value,
.mono-cell {
  font-family: 'SFMono-Regular', Consolas, monospace;
  font-variant-numeric: tabular-nums;
}
.info {
  color: var(--app-status-info) !important;
}
.normal {
  color: var(--app-status-healthy) !important;
}
.warning {
  color: var(--app-status-warning) !important;
}
.danger {
  color: var(--app-status-fault) !important;
}
.muted {
  color: var(--app-status-disabled) !important;
}
.kv-list {
  margin-top: var(--app-space-3);
  border-top: 1px solid var(--app-border-soft);
}
.kv-list > div,
.compact-bars > div,
.distribution-list > div,
.sink-list > div,
.health-check {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--app-space-3);
  min-height: var(--app-row-min-height);
  color: var(--app-text-secondary);
  font-size: var(--app-font-label);
  border-bottom: 1px solid var(--app-border-soft);
}
.kv-list > div:last-child,
.compact-bars > div:last-child,
.distribution-list > div:last-child,
.sink-list > div:last-child {
  border-bottom: 0;
}
.kv-list .value,
.compact-bars .value,
.distribution-list .value,
.health-check .value {
  color: var(--app-text-primary);
  font-size: var(--app-font-label);
  font-weight: var(--app-font-weight-bold);
}
.kv-list.tight {
  margin-top: var(--app-space-2);
}
.subsection-caption {
  margin-top: var(--app-space-3);
  color: var(--app-text-muted);
  font-size: var(--app-font-caption);
  font-weight: var(--app-font-weight-bold);
  letter-spacing: 0.1em;
  text-transform: uppercase;
}
.model-caption {
  margin-top: var(--app-space-2);
}
.compact-bars {
  margin-top: var(--app-space-1);
  border-top: 1px solid var(--app-border-soft);
}
.ellipsis,
.model-name {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.model-name {
  max-width: 72%;
}
.more-row {
  color: var(--app-text-muted) !important;
}
.four-metrics {
  display: grid;
  grid-template-columns: repeat(2, 1fr);
  gap: 0;
  margin-top: var(--app-space-3);
  border-top: 1px solid var(--app-border-soft);
  border-left: 1px solid var(--app-border-soft);
}
.four-metrics > div {
  min-width: 0;
  padding: var(--app-space-2) var(--app-space-2);
  border-right: 1px solid var(--app-border-soft);
  border-bottom: 1px solid var(--app-border-soft);
}
.four-metrics span,
.resource-grid span {
  display: block;
  color: var(--app-text-secondary);
  font-size: var(--app-font-caption);
}
.four-metrics b {
  display: block;
  margin-top: var(--app-space-1);
  font-size: var(--app-font-panel-title);
}
.sink-list {
  margin-top: var(--app-space-3);
  border-top: 1px solid var(--app-border-soft);
}
.sink-name {
  min-width: 0;
  overflow: hidden;
  color: var(--app-text-primary);
  text-overflow: ellipsis;
  white-space: nowrap;
}
.sink-type {
  margin-left: auto;
  color: var(--app-text-muted);
  font-size: var(--app-font-caption);
}
.sink-list b {
  min-width: max-content;
  text-align: right;
  font-size: var(--app-font-caption);
}
.resource-grid {
  display: grid;
  gap: var(--app-space-3);
  margin-top: var(--app-space-3);
}
.resource-grid > div {
  display: grid;
  grid-template-columns: max-content max-content minmax(0, 1fr);
  align-items: center;
  gap: var(--app-space-2);
}
.resource-grid b {
  text-align: right;
  font-size: var(--app-font-body);
}
.meter {
  height: var(--app-meter-height);
  overflow: hidden;
  background: var(--app-border-soft);
  border-radius: 2px;
}
.meter i {
  display: block;
  height: 100%;
  background: var(--app-status-info);
}
.meter.normal i {
  background: var(--app-status-healthy);
}
.health-check {
  margin-top: var(--app-space-3);
  padding-top: var(--app-space-1);
  border-top: 1px solid var(--app-border-soft);
  border-bottom: 0;
}
.compact-card {
  min-height: 10rem;
}
.distribution-list {
  margin-top: var(--app-space-2);
  border-top: 1px solid var(--app-border-soft);
}
.alarm-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: var(--app-space-3);
}
.table-card {
  padding: 0 var(--app-space-3) var(--app-space-3);
}
.table-card::before {
  left: var(--app-space-3);
  background: var(--app-status-warning);
}
.active-alert-card::before {
  background: var(--app-status-fault);
}
.alert-count-wrap {
  display: flex;
  align-items: center;
  gap: var(--app-space-2);
}
.alert-count-wrap .muted {
  font-size: var(--app-font-caption);
}
.table-head {
  min-height: var(--app-header-height);
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--app-space-3);
}
.table-head h3 {
  margin: 0;
  font-size: var(--app-font-section-title);
}
.table-head p {
  margin: var(--app-space-1) 0 0;
  color: var(--app-text-muted);
  font-size: var(--app-font-caption);
}
.count-badge {
  min-width: var(--app-control-height);
  padding: var(--app-space-1) var(--app-space-2);
  border: 1px solid currentColor;
  border-radius: var(--app-control-radius);
  text-align: center;
  font-family: 'SFMono-Regular', Consolas, monospace;
  font-size: var(--app-font-label);
  font-weight: var(--app-font-weight-bold);
}
.level-chip {
  display: inline-block;
  min-width: calc(var(--app-control-height) + var(--app-space-4));
  padding: var(--app-space-1) var(--app-space-1);
  border: 1px solid currentColor;
  border-radius: var(--app-control-radius);
  text-align: center;
  font-size: var(--app-font-caption);
  font-weight: var(--app-font-weight-bold);
  line-height: 1.35;
}
.state-text {
  font-family: 'SFMono-Regular', Consolas, monospace;
  font-size: var(--app-font-caption);
  font-weight: var(--app-font-weight-bold);
  letter-spacing: 0.03em;
}
.mono-cell {
  font-size: var(--app-font-label);
  font-weight: var(--app-font-weight-semibold);
}
.object-cell {
  color: var(--app-text-regular);
}
.event-cell {
  color: var(--app-text-regular);
  font-family: var(--app-font-family);
  font-size: var(--app-font-body);
  font-weight: var(--app-font-weight-regular);
}
@media (max-width: 1199px) {
  .primary-grid {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
  .risk-summary-grid {
    grid-template-columns: 1fr;
  }
  .coverage-grid {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
  .alarm-grid {
    grid-template-columns: 1fr;
  }
  .overview-head,
  .section-title {
    align-items: flex-start;
  }
  .color-legend {
    display: none;
  }
  .primary-grid,
  .coverage-grid {
    grid-template-columns: 1fr;
  }
}
</style>
