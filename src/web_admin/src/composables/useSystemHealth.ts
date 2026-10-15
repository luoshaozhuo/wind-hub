// System Health 查询 + wire → 展示模型映射（OverviewPage 与 SystemHealthPage 共用）。
import { computed, type Ref } from 'vue'
import { useQuery } from '@tanstack/vue-query'
import { qk } from '../api/queryKeys'
import { fetchSystemHealth, type HealthRangeParam, type SystemHealthDto } from '../api/monitoring'

export type HealthRange = '1 h' | '24 h' | '7 d' | '30 d'

export interface HealthSeries {
  axis: string[]
  memoryHost: number[]
  memoryRss: number[]
  cpuHost: number[]
  cpuProcess: number[]
  cpuTemp: number[]
  diskFree: number[]
  diskForecast: Array<number | null>
  stressStart: number
  forecastStart: number
}

export type HealthTagType = 'success' | 'warning' | 'danger' | 'info'

export interface HealthRisk {
  name: string
  state: string
  type: HealthTagType
  summary: string
  detail: string
}

function toApiRange(range: HealthRange): HealthRangeParam {
  return range.replace(' ', '') as HealthRangeParam
}

function nums(values: Array<number | null>): number[] {
  return values.map((v) => v ?? 0)
}

/** current 为后端自由键值表（number | string | null），只消费数值项。 */
function num(value: number | string | null | undefined): number | null {
  return typeof value === 'number' ? value : null
}

function healthTagType(state: string): HealthTagType {
  if (state === 'Fault') return 'danger'
  if (state === 'Warning') return 'warning'
  if (state === 'Healthy') return 'success'
  return 'info'
}

function emptySeries(): HealthSeries {
  return {
    axis: [],
    memoryHost: [],
    memoryRss: [],
    cpuHost: [],
    cpuProcess: [],
    cpuTemp: [],
    diskFree: [],
    diskForecast: [],
    stressStart: 0,
    forecastStart: 0,
  }
}

export function useSystemHealth(range: Ref<HealthRange>, refetchIntervalMs?: number) {
  const query = useQuery({
    queryKey: computed(() => qk.systemHealth(range.value)),
    queryFn: () => fetchSystemHealth(toApiRange(range.value)),
    refetchInterval: refetchIntervalMs,
  })
  const data = computed<SystemHealthDto | null>(() => query.data.value || null)

  const series = computed<HealthSeries>(() => {
    const d = data.value
    if (!d) return emptySeries()
    return {
      axis: d.series.timestamps.map((x) => x.replace('T', ' ').slice(5, 16)),
      memoryHost: nums(d.series.memory_host_gb),
      memoryRss: nums(d.series.memory_rss_gb),
      cpuHost: nums(d.series.cpu_host_pct),
      cpuProcess: nums(d.series.cpu_process_pct),
      cpuTemp: nums(d.series.cpu_temp_c),
      diskFree: nums(d.series.disk_free_gb),
      diskForecast: d.series.disk_free_gb.map(() => null),
      stressStart: 0,
      forecastStart: d.series.timestamps.length,
    }
  })

  const risks = computed<HealthRisk[]>(() =>
    (data.value?.risks || []).map((r) => ({
      name: r.name,
      state: r.state,
      type: healthTagType(r.state),
      summary: r.summary,
      detail: r.detail,
    })),
  )

  const details = computed<Array<{ group: string; items: Array<[string, string]> }>>(() => {
    const d = data.value
    if (!d) return []
    const c = d.current
    const [used, total, rss, cpuHost, cpuProcess, cpuTemp] = [
      num(c.memory_used_gb),
      num(c.memory_total_gb),
      num(c.process_rss_gb),
      num(c.cpu_host_pct),
      num(c.cpu_process_pct),
      num(c.cpu_temp_c),
    ]
    return [
      {
        group: 'Memory',
        items: [
          ['Host used', used == null ? '—' : used.toFixed(2) + ' GB'],
          ['Host total', total == null ? '—' : total.toFixed(2) + ' GB'],
          ['wind-hub RSS', rss == null ? '—' : rss.toFixed(2) + ' GB'],
        ],
      },
      {
        group: 'CPU / Thermal',
        items: [
          ['Host CPU', cpuHost == null ? '—' : cpuHost.toFixed(1) + '%'],
          ['wind-hub CPU', cpuProcess == null ? '—' : cpuProcess.toFixed(1) + '%'],
          ['CPU temp', cpuTemp == null ? '—' : cpuTemp.toFixed(1) + '°C'],
          ['Uptime', Math.round(d.uptime_seconds) + ' s'],
        ],
      },
    ]
  })

  const mounts = computed(() =>
    (data.value?.mounts || []).map((m) => ({
      mount: m.mount,
      used: m.used_gb.toFixed(1) + ' GB / ' + m.total_gb.toFixed(1) + ' GB',
      free: m.free_gb.toFixed(1) + ' GB',
      usage: m.usage_pct.toFixed(1) + '%',
      growth: m.growth_24h_gb == null ? '—' : m.growth_24h_gb.toFixed(2) + ' GB / 24 h',
      estimated: m.estimated_full_days == null ? '—' : m.estimated_full_days.toFixed(1) + ' days',
    })),
  )

  const current = computed(() => {
    const d = data.value
    const c = d?.current
    const [memUsed, memTotal, diskFreeGb, diskTotalGb, cpuHost] = [
      num(c?.memory_used_gb),
      num(c?.memory_total_gb),
      num(c?.disk_free_gb),
      num(c?.disk_total_gb),
      num(c?.cpu_host_pct),
    ]
    const memory = memTotal && memUsed ? (memUsed / memTotal) * 100 : 0
    const diskFree = diskTotalGb && diskFreeGb ? (diskFreeGb / diskTotalGb) * 100 : 0
    return {
      cpu: cpuHost ?? 0,
      memory,
      diskFree,
      healthCheck: d ? 'live' : '—',
      uptimeSeconds: d?.uptime_seconds ?? 0,
      sampledAt: d?.series.timestamps[d.series.timestamps.length - 1] || '',
    }
  })

  return { query, data, series, risks, details, mounts, current }
}
