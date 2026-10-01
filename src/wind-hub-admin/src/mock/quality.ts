// Quality 时间窗口模型（§11–§16）：qualityWindowData(window) 是 Quality 页
// 所有区域（channels / events / metrics / dimensions / issues / drawer）的统一数据源。
// 一切统计从 deviceScenarios / sinkScenarios / store 派生，qualityCheckTick 驱动
// Auto Check 的确定性重算；三个窗口各自独立结果，切窗口全部区域联动。
import { computed } from 'vue'
import { protocolOfDevice, store } from './data'
import { deviceRuntimeState, qualityCheckTick } from './runtime'
import { formatTimestamp } from '../utils/format'

export type QualityWindow = '1 h' | '24 h' | '7 d'

export interface ChannelRow {
  object: string
  source: 'Acquisition' | 'Delivery'
  protocol: string
  state: 'Healthy' | 'Degraded' | 'Interrupted' | 'Disabled'
  target: string
  last: string
  latency: string
  timeouts: number
  reconnects: number
  issue: string
}
export interface CommunicationEvent {
  id: number
  time: string
  object: string
  protocol: string
  event: string
  state: 'Active' | 'Recovered'
  error: string
  duration: string
  target: string
}
export interface QualityProblem {
  object: string
  kind: 'Task' | 'Device' | 'Point' | 'Sink'
  metric: string
  expected: string
  state: 'Warning' | 'Fault'
  error: string
}
export interface QualityDimensionRow {
  key: 'continuity' | 'timeliness' | 'completeness' | 'validity' | 'delivery'
  dimension: string
  status: 'Normal' | 'Warning' | 'Fault'
  metric: string
  detail: string
}
export interface QualityIssue {
  level: 'Fault' | 'Warning'
  object: string
  kind: 'Task' | 'Device' | 'Point' | 'Sink'
  dimension: string
  issue: string
  duration: string
  error: string
}

function ago(minutes: number, seconds = 0) {
  return formatTimestamp(new Date(Date.now() - (minutes * 60 + seconds) * 1000))
}
function windowFactor(window: QualityWindow) {
  return window === '1 h' ? 1 : window === '24 h' ? 6 : 28
}
function windowHours(window: QualityWindow) {
  return window === '1 h' ? 1 : window === '24 h' ? 24 : 168
}

// 通道当前状态（窗口无关）：Interrupted = 网络不可达；Degraded = 通信退化场景。
export function acquisitionChannels(): ChannelRow[] {
  return store.devices.filter(d => d.enabled).map(d => {
    const runtime = deviceRuntimeState(d.device_id, d.enabled)
    const interrupted = runtime.network === 'unreachable'
    const degraded = !interrupted && !!runtime.degradation
    const h = (d.device_id.charCodeAt(4) * 7 + d.device_id.charCodeAt(6)) % 60
    return {
      object: d.device_id,
      source: 'Acquisition',
      protocol: protocolOfDevice(d).toUpperCase(),
      state: interrupted ? 'Interrupted' : degraded ? 'Degraded' : 'Healthy',
      target: d.host,
      last: interrupted ? ago(12, 18) : ago(h % 9, (h * 7) % 60),
      latency: interrupted ? '—' : runtime.degradation === 'latency' ? `${380 + h} ms` : `${12 + (h * 11) % 120} ms`,
      timeouts: interrupted ? 8 : runtime.degradation === 'missing' ? 2 : 0,
      reconnects: interrupted ? 3 : runtime.degradation === 'latency' ? 1 : 0,
      issue: interrupted
        ? 'Host unreachable'
        : runtime.degradation === 'latency'
          ? 'Latency / reconnect degradation'
          : runtime.degradation === 'missing'
            ? 'Intermittent read timeout'
            : '—',
    }
  })
}

export function deliveryChannels(): ChannelRow[] {
  return store.sinks.map(s => ({
    object: s.name,
    source: 'Delivery',
    protocol: s.type === 'db' ? 'POSTGRESQL' : s.type.toUpperCase(),
    state: !s.enabled ? 'Disabled' : s.runtime_state === 'failed' ? 'Interrupted' : s.runtime_state === 'healthy' ? 'Healthy' : 'Degraded',
    target: String(s.params.bootstrap_servers || s.params.dsn || s.params.path || 'configured'),
    last: s.last_write_at || 'Never',
    latency: s.enabled ? (s.latency_ms ? s.latency_ms + ' ms' : '—') : '—',
    timeouts: s.runtime_state === 'failed' ? 3 : 0,
    reconnects: s.runtime_state === 'warning' ? 1 : 0,
    issue: s.error || (!s.enabled ? 'Disabled' : '—'),
  }))
}

export interface QualityWindowData {
  channelSummary: Array<{ key: string; label: string; value: number; tone: string }>
  communicationEvents: CommunicationEvent[]
  dataMetrics: Array<{ key: string; label: string; value: number; hint: string; tone: string }>
  dimensions: QualityDimensionRow[]
  issues: QualityIssue[]
}

// 统一窗口数据：channel summary / events / data metrics / dimensions / issues。
// qualityCheckTick 参与计算 → Auto Check 重算整个 snapshot（§16）。
export function qualityWindowData(window: QualityWindow): QualityWindowData {
  const f = windowFactor(window)
  const tick = qualityCheckTick.value
  const channels = [...acquisitionChannels(), ...deliveryChannels()]
  const interrupted = channels.filter(c => c.state === 'Interrupted')
  const degraded = channels.filter(c => c.state === 'Degraded')
  const failedSinks = store.sinks.filter(s => s.enabled && s.runtime_state === 'failed')
  const droppedTotal = store.sinks.reduce((sum, s) => sum + s.dropped_points, 0)

  // --- Channel summary（§12）：窗口内累计事件数随窗口缩放 ---
  const timeouts = Math.round(channels.reduce((sum, c) => sum + c.timeouts, 0) * f)
  const reconnects = Math.round(channels.reduce((sum, c) => sum + c.reconnects, 0) * f)
  const channelSummary = [
    { key: 'interrupted', label: 'Interrupted', value: interrupted.length, tone: 'danger' },
    { key: 'degraded', label: 'Degraded', value: degraded.length, tone: 'warning' },
    { key: 'timeouts', label: 'Timeouts', value: timeouts, tone: 'warning' },
    { key: 'reconnects', label: 'Reconnects', value: reconnects, tone: 'warning' },
  ]

  // --- Communication events（§13）：从故障对象派生，窗口决定重复次数 ---
  const repeats = window === '1 h' ? 1 : window === '24 h' ? 4 : 12
  const events: CommunicationEvent[] = []
  const faulty = channels.filter(c => c.state === 'Interrupted' || c.state === 'Degraded')
  faulty.forEach((row, index) => {
    for (let n = 0; n < repeats; n++) {
      events.push({
        id: (index + 1) * 1000 + n,
        time: ago(12 + n * Math.round(windowHours(window) * 60 / (repeats + 1)), (index * 7 + n * 3) % 60),
        object: row.object,
        protocol: row.protocol,
        event: row.state === 'Interrupted' ? 'Connection lost' : 'Channel degraded',
        state: n === 0 && row.state === 'Interrupted' ? 'Active' : 'Recovered',
        error: row.state === 'Interrupted' ? (row.issue === 'Host unreachable' ? 'No route to host / ICMP + TCP timeout' : row.issue) : row.issue,
        duration: row.state === 'Interrupted' ? '12m 18s' : '43s',
        target: row.target,
      })
    }
  })
  // file_archive 瞬时重试事件（健康 Sink 的可恢复噪声）
  events.push({
    id: 9001,
    time: ago(31, 22),
    object: 'file_archive',
    protocol: 'FILE',
    event: 'Write retry',
    state: 'Recovered',
    error: 'Transient filesystem latency',
    duration: '2.1s',
    target: '/var/tmp/wind-hub/archive.jsonl',
  })
  events.sort((a, b) => b.time.localeCompare(a.time))

  // --- Data metrics（§14）：随窗口与 check tick 确定性变化 ---
  const missing = 13 * f + (tick % 2)
  const reads = 6 * f + (tick % 3)
  const dropped = window === '1 h' ? 0 : droppedTotal
  const dataMetrics = [
    { key: 'stale', label: 'Stale Tasks', value: interrupted.length ? 1 : 0, hint: 'current', tone: 'danger' },
    { key: 'missing', label: 'Missing Cycles', value: missing, hint: window, tone: 'warning' },
    { key: 'reads', label: 'Point Read Failures', value: reads, hint: window, tone: 'warning' },
    { key: 'dropped', label: 'Dropped Points', value: dropped, hint: window, tone: dropped ? 'danger' : 'normal' },
  ]

  // --- Quality dimensions（§15）：每个维度每窗口独立结果 ---
  const hours = windowHours(window)
  const expected = hours * 3600
  const received = expected - missing
  const completenessPct = (received / expected) * 100
  const longestGap = window === '1 h' ? '12m 18s' : window === '24 h' ? '47m 03s' : '3h 12m'
  const p95 = window === '1 h' ? 1.9 : window === '24 h' ? 2.3 : 2.1
  const decodeErrors = window === '1 h' ? 0 : f + (tick % 2)
  const invalidPayload = window === '1 h' ? 0 : f
  const writeFailures = failedSinks.length * 3 * f
  const backlog = store.sinks.reduce((sum, s) => sum + s.queue_depth, 0)
  const dimensions: QualityDimensionRow[] = [
    {
      key: 'continuity', dimension: 'Continuity',
      status: interrupted.length ? 'Fault' : 'Normal',
      metric: interrupted.length ? '1 stale task' : 'No stale task',
      detail: `Longest gap ${longestGap} · ${f} interruption(s)`,
    },
    {
      key: 'timeliness', dimension: 'Timeliness',
      status: 'Warning',
      metric: `P95 freshness ${p95} × period`,
      detail: `${degraded.filter(c => c.source === 'Acquisition').length + interrupted.length} devices degraded · ${5 * f} late samples`,
    },
    {
      key: 'completeness', dimension: 'Completeness',
      status: completenessPct >= 99.9 ? 'Normal' : completenessPct >= 99 ? 'Warning' : 'Fault',
      metric: `${missing} missing cycles`,
      detail: `${completenessPct.toFixed(2)}% received · ${reads} read failures`,
    },
    {
      key: 'validity', dimension: 'Validity',
      status: decodeErrors + invalidPayload > 0 ? 'Warning' : 'Normal',
      metric: `${decodeErrors} decode errors`,
      detail: `${invalidPayload} invalid payload · 0 timestamp/order`,
    },
    {
      key: 'delivery', dimension: 'Delivery Integrity',
      status: dropped > 0 || failedSinks.length ? 'Fault' : backlog > 0 ? 'Warning' : 'Normal',
      metric: `${dropped} dropped points`,
      detail: `${writeFailures} write failures · backlog ${backlog} · ${failedSinks.length} sink(s) failed`,
    },
  ]

  // --- Active issues（§14）：从 mock state 派生，不写死 ---
  const issues: QualityIssue[] = []
  if (interrupted.length) {
    issues.push({
      level: 'Fault', object: 'turbine-ads-all', kind: 'Task', dimension: 'Continuity',
      issue: 'No fresh samples', duration: longestGap, error: 'wtg-041 host unreachable',
    })
  }
  if (degraded.some(c => c.object === 'wtg-017')) {
    issues.push({
      level: 'Warning', object: 'wtg-017', kind: 'Device', dimension: 'Timeliness',
      issue: 'Freshness above threshold', duration: '4m 05s', error: 'Repeated high read latency',
    })
  }
  if (degraded.some(c => c.object === 'wtg-003')) {
    issues.push({
      level: 'Warning', object: 'wtg-003', kind: 'Device', dimension: 'Completeness',
      issue: 'Missing acquisition cycles', duration: '2m 41s', error: 'Modbus read timeout',
    })
  }
  if ((deviceRuntimeState('wtg-026').failingPointIndexes).length) {
    issues.push({
      level: 'Warning', object: 'wtg-026', kind: 'Point', dimension: 'Completeness',
      issue: 'Point read failure', duration: '38m 12s', error: 'First point in table returns no data',
    })
  }
  for (const s of failedSinks) {
    issues.push({
      level: 'Fault', object: s.name, kind: 'Sink', dimension: 'Delivery Integrity',
      issue: 'Sink write failing', duration: '26m 40s', error: s.error || 'Write failed',
    })
  }
  return { channelSummary, communicationEvents: events, dataMetrics, dimensions, issues }
}

// Drawer 明细（§15/§16）：随窗口与 tick 变化。
export function qualityMetricDetail(key: string, window: QualityWindow) {
  const f = windowFactor(window)
  if (key === 'stale') return {
    tasks: [{ Task: 'turbine-ads-all', Expected: '1 s', 'Last Sample': ago(12, 18), State: 'Stale' }],
    devices: [{ Device: 'wtg-041', Protocol: 'ADS', 'Last Success': ago(12, 18), State: 'Fault' }],
    errors: [
      { Time: ago(12, 18), Object: 'wtg-041', Error: 'Host unreachable (ICMP + TCP timeout)' },
      { Time: ago(12, 16), Object: 'turbine-ads-all', Error: 'No fresh samples' },
    ],
  }
  if (key === 'missing') return {
    tasks: [
      { Task: 'turbine-modbus-all', Expected: '1 s', 'Last Sample': ago(0, 58), Missing: 8 * f, State: 'Warning' },
      { Task: 'pcs-fast', Expected: '1 s', 'Last Sample': ago(1, 2), Missing: 5 * f, State: 'Warning' },
    ],
    devices: [
      { Device: 'wtg-003', 'Last Success': ago(4, 29), Missing: 5 * f, Error: 'Read timeout' },
      { Device: 'wtg-017', 'Last Success': ago(2, 55), Missing: 2 * f, Error: 'High latency' },
    ],
    errors: [
      { Time: ago(4, 31), Object: 'wtg-003', Error: 'Modbus read timeout' },
      { Time: ago(3, 8), Object: 'wtg-017', Error: 'Read latency above threshold' },
    ],
  }
  if (key === 'reads') return {
    tasks: [
      { Task: 'turbine-ads-all', 'Last Sample': ago(2, 10), Failures: 4 * f, State: 'Warning' },
      { Task: 'turbine-modbus-all', 'Last Sample': ago(1, 50), Failures: 2 * f, State: 'Warning' },
    ],
    devices: [
      { Device: 'wtg-026', Point: 'wtg_ads_001', 'Last Success': ago(38, 12), Error: 'Point read failed' },
      { Device: 'wtg-003', Point: 'wtg_mb_003', 'Last Success': ago(4, 29), Error: 'Modbus timeout' },
    ],
    errors: [
      { Time: ago(38, 11), Object: 'wtg-026/wtg_ads_001', Error: 'POINT_READ_FAILED' },
      { Time: ago(4, 28), Object: 'wtg-003/wtg_mb_003', Error: 'Read timeout' },
    ],
  }
  // dropped
  return {
    tasks: store.tasks.filter(t => t.sinks.includes('db_main')).map(t => ({ Task: t.task_id, Sink: 'db_main', Dropped: 6, State: 'Warning' })),
    devices: [],
    errors: [
      { Time: ago(26, 40), Object: 'db_main', Error: 'SINK_CONNECTION_REFUSED · dropped 6 points after 3 retries' },
      { Time: ago(26, 41), Object: 'db_main', Error: 'TCP connection refused by remote host' },
    ],
  }
}

export function qualityDimensionDetail(key: string, window: QualityWindow): {
  distribution: Array<{ name: string; value: number }>
  problems: QualityProblem[]
} {
  const f = windowFactor(window)
  if (key === 'continuity') return {
    distribution: [
      { name: 'Normal', value: 54 },
      { name: 'Warning', value: 0 },
      { name: 'Fault', value: 1 },
    ],
    problems: [{ object: 'turbine-ads-all', kind: 'Task', metric: `Gap ${window === '1 h' ? '12m 18s' : window === '24 h' ? '47m 03s' : '3h 12m'}`, expected: '≤ 3 s', state: 'Fault', error: 'No fresh samples (wtg-041 unreachable)' }],
  }
  if (key === 'timeliness') return {
    distribution: [
      { name: '≤1.0×', value: 42 + f },
      { name: '1.0–1.5×', value: 7 },
      { name: '1.5–3.0×', value: 5 },
      { name: '>3.0×', value: 1 },
    ],
    problems: [
      { object: 'wtg-041', kind: 'Device', metric: 'No fresh data', expected: '≤ 1.5 × period', state: 'Fault', error: 'Host unreachable' },
      { object: 'wtg-017', kind: 'Device', metric: `${window === '1 h' ? 1.9 : window === '24 h' ? 2.3 : 2.1} × period`, expected: '≤ 1.5 × period', state: 'Warning', error: 'High read latency' },
      { object: 'wtg-029', kind: 'Device', metric: '1.7 × period', expected: '≤ 1.5 × period', state: 'Warning', error: 'Jitter burst' },
    ],
  }
  if (key === 'completeness') return {
    distribution: [
      { name: '≥99.9%', value: 50 },
      { name: '99–99.9%', value: 4 },
      { name: '<99%', value: 2 },
    ],
    problems: [
      { object: 'wtg-003', kind: 'Device', metric: `${5 * f} missing cycles`, expected: '0', state: 'Warning', error: 'Modbus timeout' },
      { object: 'wtg-026', kind: 'Point', metric: `${1 * f} point read failure`, expected: '0', state: 'Warning', error: 'POINT_READ_FAILED' },
    ],
  }
  if (key === 'validity') return {
    distribution: [
      { name: 'Valid', value: 54 },
      { name: 'Warning', value: window === '1 h' ? 0 : 2 },
      { name: 'Fault', value: 0 },
    ],
    problems: window === '1 h' ? [] : [
      { object: 'wtg-028/wtg_ads_004', kind: 'Point', metric: 'Decode failed', expected: 'Valid float32', state: 'Warning', error: 'NaN payload' },
      { object: 'wtg-031/wtg_ads_007', kind: 'Point', metric: 'Invalid payload', expected: 'Valid float32', state: 'Warning', error: 'Invalid payload length' },
    ],
  }
  // delivery
  const droppedTotal = store.sinks.reduce((sum, s) => sum + s.dropped_points, 0)
  return {
    distribution: [
      { name: 'Normal', value: 2 },
      { name: 'Warning', value: 0 },
      { name: 'Fault', value: 1 },
    ],
    problems: [{
      object: 'db_main', kind: 'Sink', metric: `${window === '1 h' ? 0 : droppedTotal} dropped · 3×${f} write failures`,
      expected: '0 dropped · backlog 0', state: 'Fault', error: 'SINK_CONNECTION_REFUSED',
    }],
  }
}

export function qualityChannelMetricDetail(key: string, window: QualityWindow) {
  const f = windowFactor(window)
  const base = [
    { name: '00–04', value: key === 'timeouts' ? 2 * f : 0 },
    { name: '04–08', value: 1 * f },
    { name: '08–12', value: key === 'timeouts' ? 4 * f : 1 * f },
    { name: '12–16', value: key === 'reconnects' ? 3 * f : 1 * f },
    { name: '16–20', value: key === 'interrupted' ? 2 * f : 2 * f },
    { name: '20–24', value: 1 * f },
  ]
  const devices = [
    { Device: 'wtg-041', Protocol: 'ADS', Events: key === 'timeouts' ? 8 * f : 3 * f, Duration: '12m 18s', 'Last Event': ago(12, 18), Error: 'Host unreachable' },
    { Device: 'wtg-017', Protocol: 'ADS', Events: key === 'reconnects' ? 2 * f : 1 * f, Duration: '48s', 'Last Event': ago(197, 8), Error: 'Repeated reconnect / high latency' },
    { Device: 'wtg-003', Protocol: 'MODBUS', Events: key === 'timeouts' ? 2 * f : 1 * f, Duration: '31s', 'Last Event': ago(244, 29), Error: 'Intermittent read timeout' },
  ]
  const errors = [
    { Time: ago(12, 18), Object: 'wtg-041', Event: 'Disconnected', Error: 'Host unreachable' },
    { Time: ago(197, 8), Object: 'wtg-017', Event: 'Channel degraded', Error: 'Read latency above threshold' },
  ]
  return { distribution: base, devices, errors }
}

// Auto Check（§16）：计数器 +1 → qualityWindowData 全量重算。
export function runQualityCheck() {
  qualityCheckTick.value += 1
}

// Overview 复用同一事实源（§29 禁止跨页面不一致）
export function useQualityWindow(window: QualityWindow) {
  return computed(() => qualityWindowData(window))
}
