// Mock Quality：由 device scenarios / task runtime / sink runtime 动态派生，
// 支持 1h / 24h / 7d 窗口与 Auto Check（tick 驱动确定性变化 + Logs）。
import type { components } from '../api/generated/schema'
import { mockState, sinkRuntimeOf, taskPhase } from './state'
import { deviceRuntimeState } from './scenarios'
import { appendLog } from './logs'
import { epochSecNow } from './values'
import { targetsOfTask } from './service'
import { modelOf } from './data'

type QualityResponse = components['schemas']['QualityResponse']
type Channel = components['schemas']['QualityChannelResponse']
type Metric = components['schemas']['QualityMetricResponse']
type Dimension = components['schemas']['QualityDimensionResponse']
type Issue = components['schemas']['QualityIssueResponse']
type Event = components['schemas']['CommunicationEventResponse']

const WINDOW_FACTOR: Record<string, number> = { '1h': 1, '24h': 6, '7d': 28 }

function acquisitionChannels(factor: number): Channel[] {
  const channels: Channel[] = []
  for (const d of mockState.devices) {
    if (!d.enabled) continue
    const runtime = deviceRuntimeState(d.device_id, d.enabled)
    const protocol = modelOf(d)?.protocol ?? ''
    const offline = runtime.network === 'unreachable'
    const protocolDown = !!runtime.protocolError
    const failing = runtime.failingPointIndexes.length
    const timeouts = offline ? 4 * factor : runtime.degradation === 'latency' ? factor : 0
    const missing = runtime.degradation === 'missing' ? 3 * factor : 0
    const state =
      offline || protocolDown ? 'failed' : failing || runtime.degradation ? 'warning' : 'ok'
    channels.push({
      object: d.device_id,
      source: 'device',
      protocol,
      target: d.host,
      state,
      timeouts,
      reconnects: offline ? 2 * factor : 0,
      latency_ms:
        runtime.degradation === 'latency' ? 420 : offline ? 0 : 8 + (d.device_id.length % 5) * 3,
      issue:
        state === 'ok'
          ? null
          : offline
            ? 'host unreachable'
            : protocolDown
              ? runtime.protocolError!.message
              : failing
                ? `${failing} point(s) failing`
                : runtime.degradation === 'latency'
                  ? 'read latency above threshold'
                  : `${missing} cycle(s) missing`,
    })
  }
  return channels
}

function deliveryChannels(factor: number): Channel[] {
  return mockState.sinks
    .filter((s) => s.enabled)
    .map((s) => {
      const runtime = sinkRuntimeOf(s.name)
      const state = runtime.error ? 'failed' : 'ok'
      return {
        object: s.name,
        source: 'sink',
        protocol: s.type,
        target:
          s.type === 'kafka'
            ? String(s.connection.bootstrap_servers ?? '')
            : s.type === 'db'
              ? String(s.connection.dsn ?? '').replace(/:\/\/[^@/]+@/, '://***@')
              : String(s.connection.path ?? ''),
        state,
        timeouts: runtime.error ? factor : 0,
        reconnects: runtime.error ? 2 * factor : 0,
        latency_ms: runtime.latency_ms || null,
        issue: runtime.error || null,
      }
    })
}

function iso(sec: number): string {
  return new Date(sec * 1000).toISOString()
}

export function qualityForWindow(window: string): QualityResponse {
  const factor = WINDOW_FACTOR[window] ?? 1
  const tick = mockState.qualityCheckTick
  const acquisition = acquisitionChannels(factor)
  const delivery = deliveryChannels(factor)

  const failedAcq = acquisition.filter((c) => c.state === 'failed').length
  const warnAcq = acquisition.filter((c) => c.state === 'warning').length
  const failedDel = delivery.filter((c) => c.state === 'failed').length
  const enabledDevices = mockState.devices.filter((d) => d.enabled).length
  const runningTasks = mockState.tasks.filter((t) => taskPhase(t.task_id) === 'running')

  const expectedPoints = runningTasks.reduce((n, t) => n + targetsOfTask(t).length, 0) * 60 * factor
  const droppedPoints =
    mockState.sinks.reduce((n, s) => n + sinkRuntimeOf(s.name).dropped_points, 0) +
    failedAcq * 12 * factor +
    warnAcq * 2 * factor
  const collectedPoints = Math.max(0, expectedPoints - droppedPoints - tick * 0 + (tick % 3))

  const continuity = enabledDevices ? (enabledDevices - failedAcq) / enabledDevices : 1
  const timeliness =
    1 -
    acquisition.filter((c) => (c.latency_ms ?? 0) > 300).length / Math.max(1, acquisition.length)
  const completeness = expectedPoints ? collectedPoints / expectedPoints : 1
  const deliveryIntegrity = delivery.length ? (delivery.length - failedDel) / delivery.length : 1

  const pct = (v: number) => Math.round(v * 1000) / 10
  const statusOf = (v: number, warn = 98) => (v >= 99.9 ? 'ok' : v >= warn ? 'warning' : 'critical')

  const dimensions: Dimension[] = [
    {
      key: 'continuity',
      dimension: 'Continuity',
      metric: `${pct(continuity)}%`,
      status: statusOf(continuity * 100),
      detail: `${enabledDevices - failedAcq}/${enabledDevices} enabled devices reachable`,
    },
    {
      key: 'timeliness',
      dimension: 'Timeliness',
      metric: `${pct(timeliness)}%`,
      status: statusOf(timeliness * 100),
      detail: 'read latency within threshold',
    },
    {
      key: 'completeness',
      dimension: 'Completeness',
      metric: `${pct(completeness)}%`,
      status: statusOf(completeness * 100, 99),
      detail: `${collectedPoints}/${expectedPoints} expected points collected`,
    },
    {
      key: 'delivery',
      dimension: 'Delivery Integrity',
      metric: `${pct(deliveryIntegrity)}%`,
      status: statusOf(deliveryIntegrity * 100),
      detail: `${delivery.length - failedDel}/${delivery.length} enabled sinks healthy`,
    },
  ]

  const channel_summary: Metric[] = [
    {
      key: 'acquisition_ok',
      label: 'Acquisition OK',
      value: acquisition.length - failedAcq - warnAcq,
      status: 'ok',
      hint: 'channels without issues',
    },
    {
      key: 'acquisition_warning',
      label: 'Acquisition Warning',
      value: warnAcq,
      status: warnAcq ? 'warning' : 'ok',
      hint: 'partial point failure / latency / missing cycles',
    },
    {
      key: 'acquisition_failed',
      label: 'Acquisition Failed',
      value: failedAcq,
      status: failedAcq ? 'critical' : 'ok',
      hint: 'unreachable / protocol failure',
    },
    {
      key: 'delivery_failed',
      label: 'Delivery Failed',
      value: failedDel,
      status: failedDel ? 'critical' : 'ok',
      hint: 'sinks with failing writes',
    },
  ]

  const data_metrics: Metric[] = [
    {
      key: 'expected_points',
      label: 'Expected Points',
      value: expectedPoints,
      status: 'ok',
      hint: 'window expectation from running tasks',
    },
    {
      key: 'collected_points',
      label: 'Collected Points',
      value: collectedPoints,
      status: 'ok',
      hint: 'points passing acquisition',
    },
    {
      key: 'stale_points',
      label: 'Stale Points',
      value: failedAcq * 12 * factor,
      status: failedAcq ? 'warning' : 'ok',
      hint: 'points without fresh samples',
    },
    {
      key: 'dropped_points',
      label: 'Dropped Points',
      value: droppedPoints,
      status: droppedPoints ? 'warning' : 'ok',
      hint: 'dropped before delivery',
    },
  ]

  const issues: Issue[] = []
  for (const c of acquisition) {
    if (c.state === 'ok') continue
    issues.push({
      kind: c.source,
      dimension:
        c.state === 'failed'
          ? 'continuity'
          : c.issue?.includes('latency')
            ? 'timeliness'
            : 'completeness',
      level: c.state === 'failed' ? 'critical' : 'warning',
      object: c.object,
      issue: c.issue ?? 'issue',
      error: c.issue ?? null,
      duration_seconds: 300 * factor,
    })
  }
  for (const c of delivery) {
    if (c.state === 'ok') continue
    issues.push({
      kind: 'sink',
      dimension: 'delivery',
      level: 'critical',
      object: c.object,
      issue: c.issue ?? 'sink failure',
      error: c.issue ?? null,
      duration_seconds: 600 * factor,
    })
  }

  const events: Event[] = []
  const now = epochSecNow()
  let cursor = now - 3600 * factor
  for (const c of acquisition.filter((x) => x.state === 'failed')) {
    events.push({
      timestamp: iso(cursor),
      object: c.object,
      event: 'disconnect',
      state: 'failed',
      evidence: c.issue ?? '',
    })
    events.push({
      timestamp: iso(cursor + 120),
      object: c.object,
      event: 'reconnect failed',
      state: 'failed',
      evidence: 'retry backoff 8 s',
    })
    cursor += 600
  }

  return {
    window,
    sampled_from: iso(now - 3600 * factor),
    sampled_to: iso(now),
    acquisition_channels: acquisition,
    delivery_channels: delivery,
    channel_summary,
    data_metrics,
    dimensions,
    issues,
    events,
  }
}

/** Auto Check：tick 驱动、追加日志，返回当前窗口结果（确定性，无随机）。 */
export function runQualityCheck(window: string): QualityResponse {
  mockState.qualityCheckTick += 1
  appendLog('INFO', 'quality', window, `auto check #${mockState.qualityCheckTick} completed`)
  return qualityForWindow(window)
}
