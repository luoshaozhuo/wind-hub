// Quality 查询 + wire → 展示模型映射（QualityPage 使用；指标/维度明细为纯函数）。
import { computed, type Ref } from 'vue'
import { useQuery } from '@tanstack/vue-query'
import { qk } from '../api/queryKeys'
import { fetchQuality, type QualityDto, type QualityWindowParam } from '../api/monitoring'
import type { components } from '../api/generated/schema'

export type QualityWindow = '1 h' | '24 h' | '7 d'

type ChannelDto = components['schemas']['QualityChannelResponse']

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

export interface QualityView {
  channelSummary: Array<{ key: string; label: string; value: number; tone: string }>
  communicationEvents: CommunicationEvent[]
  dataMetrics: Array<{ key: string; label: string; value: number; hint: string; tone: string }>
  dimensions: components['schemas']['QualityDimensionResponse'][]
  issues: Array<{
    level: 'Fault' | 'Warning'
    object: string
    kind: 'Task' | 'Device' | 'Point' | 'Sink'
    dimension: string
    issue: string
    duration: string
    error: string
  }>
  acquisition: ChannelRow[]
  delivery: ChannelRow[]
}

export function toApiWindow(window: QualityWindow): QualityWindowParam {
  return window === '1 h' ? '1h' : window === '24 h' ? '24h' : '7d'
}

function channelRow(row: ChannelDto): ChannelRow {
  return {
    object: String(row.object),
    source: row.source as ChannelRow['source'],
    protocol: String(row.protocol),
    state: row.state as ChannelRow['state'],
    target: String(row.target),
    last: '—',
    latency: row.latency_ms == null ? '—' : Math.round(row.latency_ms) + ' ms',
    timeouts: Number(row.timeouts || 0),
    reconnects: Number(row.reconnects || 0),
    issue: String(row.issue || '—'),
  }
}

export function mapQualityView(row: QualityDto): QualityView {
  return {
    channelSummary: row.channel_summary.map((x) => ({
      key: x.key,
      label: x.label,
      value: Number(x.value),
      tone: x.status,
    })),
    communicationEvents: row.events.map((x, i) => ({
      id: i,
      time: String(x.timestamp).replace('T', ' ').slice(0, 19),
      object: x.object,
      protocol: '—',
      event: x.event,
      state: x.state as CommunicationEvent['state'],
      error: x.evidence,
      duration: '—',
      target: '—',
    })),
    dataMetrics: row.data_metrics.map((x) => ({
      key: x.key,
      label: x.label,
      value: Number(x.value),
      hint: x.hint,
      tone: x.status,
    })),
    dimensions: row.dimensions,
    issues: row.issues.map((x) => ({
      ...x,
      level: x.level as 'Fault' | 'Warning',
      kind: x.kind as 'Task' | 'Device' | 'Point' | 'Sink',
      duration: x.duration_seconds == null ? '—' : Math.round(x.duration_seconds) + ' s',
      error: x.error || '—',
    })),
    acquisition: row.acquisition_channels.map(channelRow),
    delivery: row.delivery_channels.map(channelRow),
  }
}

const EMPTY_VIEW: QualityView = {
  channelSummary: [],
  communicationEvents: [],
  dataMetrics: [],
  dimensions: [],
  issues: [],
  acquisition: [],
  delivery: [],
}

export function useQuality(window: Ref<QualityWindow>, refetchIntervalMs?: number) {
  const query = useQuery({
    queryKey: computed(() => qk.quality(window.value)),
    queryFn: () => fetchQuality(toApiWindow(window.value)),
    refetchInterval: refetchIntervalMs,
  })
  const view = computed<QualityView>(() =>
    query.data.value ? mapQualityView(query.data.value) : EMPTY_VIEW,
  )
  return { query, view }
}

// -- 明细纯函数（drawer 数据源） ----------------------------------------------

export function qualityMetricDetail(key: string, view: QualityView) {
  const issues = view.issues.filter((i) =>
    key === 'dropped' ? i.dimension.includes('Delivery') : true,
  )
  return {
    tasks: issues
      .filter((i) => i.kind === 'Task')
      .map((i) => ({ Task: i.object, State: i.level, Error: i.error })),
    devices: issues
      .filter((i) => i.kind === 'Device' || i.kind === 'Point')
      .map((i) => ({ Device: i.object, State: i.level, Error: i.error })),
    errors: issues.map((i) => ({ Object: i.object, Error: i.error })),
  }
}

export function qualityDimensionDetail(key: string, view: QualityView) {
  const row = view.dimensions.find((x) => x.key === key)
  const issues = view.issues.filter((i) =>
    i.dimension.toLowerCase().startsWith((row?.dimension || key).toLowerCase().split(' ')[0]),
  )
  return {
    distribution: [{ name: row?.status || 'Normal', value: 1 }],
    problems: issues.map((i) => ({
      object: i.object,
      kind: i.kind,
      metric: i.issue,
      expected: 'See threshold',
      state: i.level,
      error: i.error,
    })) as QualityProblem[],
  }
}

export function qualityChannelMetricDetail(_key: string, view: QualityView) {
  const rows = [...view.acquisition, ...view.delivery]
  return {
    distribution: rows.reduce<Array<{ name: string; value: number }>>((acc, row) => {
      const hit = acc.find((x) => x.name === row.state)
      if (hit) hit.value++
      else acc.push({ name: row.state, value: 1 })
      return acc
    }, []),
    devices: rows.map((r) => ({
      Device: r.object,
      Protocol: r.protocol,
      State: r.state,
      Error: r.issue,
    })),
    errors: view.communicationEvents.map((e) => ({
      Time: e.time,
      Object: e.object,
      Event: e.event,
      Error: e.error,
    })),
  }
}
